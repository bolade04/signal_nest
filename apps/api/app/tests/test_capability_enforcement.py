"""P6-CAP-1 / P6-UI-006: scheduling and RSS enforce through the capability resolver.

Before P6-CAP-1 only feedback consumed :func:`app.capabilities.resolver.resolve_capability`.
Scheduling (route gate, tick seeding, the tick itself) and RSS (connector selection) read
their raw global flags, so neither honoured a workspace override nor the safety ceiling —
while the operator console, which *does* resolve, reported "Enabled · Workspace override"
for a workspace whose schedule mutations still answered 503 (P6-UI-006).

These tests pin every enforcement point to the resolver's per-workspace decision and pin
the three views of that decision together — the enforcement outcome, the customer
reflection (``GET /workspaces/{id}/scheduling-capability``) and the operator effective
read (``GET /internal/system/capabilities/effective``) — across the precedence matrix:

* global off + override on, global on + override off, both directions of the dark default;
* the safety ceiling (rule 1) outranking an enable override and an enabled global flag;
* sibling-workspace and cross-organization isolation, and a tenant-mismatched override;
* non-members (403) and a view-only member (reads the reflection, cannot mutate);
* a resolver failure is re-raised, never an enable, and writes nothing.

All override rows are synthetic test intent on synthetic workspaces; the shipped global
flags are only toggled through ``monkeypatch`` on the cached settings object and restored.
No connector performs a network fetch (the RSS connector's default provider is the bundled
sandbox feed, and the pipeline test stops before fetching).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

import app.capabilities.resolver as resolver_mod
import app.scouting_requests.schedules as schedules_mod
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.connectors.rss import RssNewsConnector
from app.core.config import get_settings
from app.core.enums import Role
from app.db.models import Base
from app.db.session import get_db
from app.jobs.models import Job
from app.jobs.status import JobType
from app.main import app
from app.organizations.models import OrganizationMember, User
from app.scouting_requests.connectors import FixtureConnector, get_connector
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.tests._auth2_support import live_bearer

from .test_feedback_capability_gate import _seed_market

API = get_settings().api_prefix
SCHED = Capability.SCOUT_SCHEDULING
RSS = Capability.CONNECTOR_RSS
_T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)

_OPERATOR = "operator-cap"
_VIEWER = "viewer-cap"


class _Harness:
    def __init__(self, client: TestClient, make, markets) -> None:
        self.client = client
        self.make = make
        self.markets = markets
        self.operator_auth = {"Authorization": f"Bearer {live_bearer(_OPERATOR)}"}
        self.viewer_auth = {"Authorization": f"Bearer {live_bearer(_VIEWER)}"}

    # -- tenant handles ----------------------------------------------------
    def m(self, key: str):
        return self.markets[key]

    def _sched_url(self, key: str, suffix: str = "") -> str:
        mk = self.m(key)
        return f"{API}/workspaces/{mk.ws_id}/scout-requests/sr-{key}/schedule{suffix}"

    # -- customer surface --------------------------------------------------
    def create(self, key: str, *, auth=None):
        return self.client.post(
            self._sched_url(key), json={"interval": "daily"},
            headers=auth or self.m(key).owner_auth,
        )

    def pause(self, key: str):
        return self.client.post(self._sched_url(key, "/pause"), headers=self.m(key).owner_auth)

    def resume(self, key: str):
        return self.client.post(self._sched_url(key, "/resume"), headers=self.m(key).owner_auth)

    def reflect(self, key: str, *, auth=None):
        return self.client.get(
            f"{API}/workspaces/{self.m(key).ws_id}/scheduling-capability",
            headers=auth or self.m(key).owner_auth,
        )

    # -- operator surface --------------------------------------------------
    def operator_effective(self, key: str, capability: Capability) -> dict:
        mk = self.m(key)
        r = self.client.get(
            f"{API}/internal/system/capabilities/effective",
            params={
                "organization_id": mk.org_id,
                "workspace_id": mk.ws_id,
                "capability": capability.value,
            },
            headers=self.operator_auth,
        )
        assert r.status_code == 200, r.text
        (item,) = r.json()["items"]
        return item

    # -- state -------------------------------------------------------------
    def set_override(
        self, key: str, capability: Capability, *, enabled: bool,
        organization_id: str | None = None,
    ) -> None:
        mk = self.m(key)
        with self.make() as s:
            s.add(WorkspaceCapabilityOverride(
                organization_id=organization_id or mk.org_id,
                workspace_id=mk.ws_id,
                capability=capability.value,
                enabled=enabled,
            ))
            s.commit()

    def schedule_count(self) -> int:
        with self.make() as s:
            return int(s.scalar(select(func.count()).select_from(ScoutSchedule)) or 0)


@pytest.fixture
def h(tmp_path_factory, monkeypatch):
    tmp = tmp_path_factory.mktemp("capability_enforcement")
    engine = create_engine(
        f"sqlite:///{tmp/'cap.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with make() as s:
        markets = {
            # "a" and "b" share an organization; "other" is a different organization.
            "a": _seed_market(s, "a", org_id="org-shared"),
            "b": _seed_market(s, "b", org_id="org-shared", create_org=False),
            "other": _seed_market(s, "other", org_id="org-other"),
        }
        s.add(User(id=_OPERATOR, email="op@example.com", full_name="Op",
                   hashed_password="x", is_active=True, is_operator=True))
        s.add(User(id=_VIEWER, email="viewer@example.com", full_name="Viewer",
                   hashed_password="x", is_active=True))
        s.flush()
        s.add(OrganizationMember(id="m-viewer-cap", organization_id="org-shared",
                                 user_id=_VIEWER, role=Role.VIEWER.value))
        s.commit()

    def _override_get_db():
        s = make()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # Every reader resolves the same cached settings object; pin both flags to the
    # shipped dark default so a test opts in explicitly (monkeypatch restores them).
    settings = get_settings()
    monkeypatch.setattr(settings, "scout_scheduling_enabled", False)
    monkeypatch.setattr(settings, "connector_rss_enabled", False)
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield _Harness(TestClient(app), make, markets)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _global(monkeypatch, capability: Capability, enabled: bool) -> None:
    attr = {SCHED: "scout_scheduling_enabled", RSS: "connector_rss_enabled"}[capability]
    monkeypatch.setattr(get_settings(), attr, enabled)


def _ceiling_blocks(monkeypatch, capability: Capability) -> None:
    """Make the safety ceiling (precedence rule 1) prohibit ``capability``.

    The shipped ceiling blocks only unregistered capabilities; a broader environment
    ceiling is a deferred decision (4A-C plan §8.28 Q1). Patching the rule-1 predicate
    is how these tests prove each enforcement point *consumes* a ceiling decision
    rather than reading a flag around it.
    """
    real = resolver_mod._ceiling_blocks
    monkeypatch.setattr(
        resolver_mod, "_ceiling_blocks", lambda c: c is capability or real(c)
    )


# --------------------------------------------------------------------------- #
# Scheduling — the precedence matrix, three views kept in lock-step (P6-UI-006)
# --------------------------------------------------------------------------- #
_MATRIX = [
    # (global flag, override for workspace "a" or None, expected effective)
    (False, None, False),  # shipped dark default
    (False, True, True),   # global off / override on — the P6-UI-006 reproduction
    (False, False, False),
    (True, None, True),
    (True, False, False),  # global on / override off
    (True, True, True),
]


@pytest.mark.parametrize(("global_on", "override", "expected"), _MATRIX)
def test_scheduling_enforcement_reflection_and_operator_view_agree(
    h, monkeypatch, global_on, override, expected
):
    _global(monkeypatch, SCHED, global_on)
    if override is not None:
        h.set_override("a", SCHED, enabled=override)

    operator = h.operator_effective("a", SCHED)
    reflected = h.reflect("a")
    created = h.create("a")

    assert operator["effective_enabled"] is expected
    assert reflected.status_code == 200
    assert reflected.json() == {"enabled": expected}
    assert created.status_code == (201 if expected else 503), created.text
    if not expected:
        assert created.json()["error"]["code"] == "capability_unavailable"
        assert h.schedule_count() == 0
    if override is not None:
        assert operator["decided_by"] == "workspace_override"


def test_override_enable_seeds_the_first_tick_while_global_flag_stays_off(h, monkeypatch):
    h.set_override("a", SCHED, enabled=True)
    assert h.create("a").status_code == 201
    with h.make() as s:
        ticks = s.scalars(
            select(Job).where(Job.job_type == JobType.SCOUT_SCHEDULE_TICK.value)
        ).all()
    assert len(ticks) == 1 and ticks[0].workspace_id == h.m("a").ws_id
    assert get_settings().scout_scheduling_enabled is False


def test_every_schedule_mutation_is_resolver_gated(h, monkeypatch):
    h.set_override("a", SCHED, enabled=True)
    assert h.create("a").status_code == 201
    assert h.pause("a").status_code == 200
    # Flip the decision for this workspace only: global on, but a disable override.
    _global(monkeypatch, SCHED, True)
    with h.make() as s:
        row = s.scalar(select(WorkspaceCapabilityOverride))
        row.enabled = False
        s.commit()
    assert h.resume("a").status_code == 503
    assert h.pause("a").status_code == 503
    r = h.client.delete(h._sched_url("a"), headers=h.m("a").owner_auth)
    assert r.status_code == 503
    assert h.schedule_count() == 1


def test_safety_ceiling_outranks_override_and_global_flag(h, monkeypatch):
    _global(monkeypatch, SCHED, True)
    h.set_override("a", SCHED, enabled=True)
    _ceiling_blocks(monkeypatch, SCHED)

    operator = h.operator_effective("a", SCHED)
    assert operator["effective_enabled"] is False
    assert operator["decided_by"] == "safety_ceiling"
    assert h.reflect("a").json() == {"enabled": False}
    assert h.create("a").status_code == 503
    assert h.schedule_count() == 0


def test_override_on_one_workspace_enables_neither_sibling_nor_other_org(h):
    h.set_override("a", SCHED, enabled=True)
    assert h.create("a").status_code == 201
    assert h.create("b").status_code == 503
    assert h.reflect("b").json() == {"enabled": False}
    assert h.create("other").status_code == 503
    assert h.reflect("other").json() == {"enabled": False}


def test_tenant_mismatched_override_is_not_honoured(h):
    h.set_override("a", SCHED, enabled=True, organization_id="org-other")
    assert h.reflect("a").json() == {"enabled": False}
    assert h.create("a").status_code == 503


def test_non_member_is_forbidden_even_where_scheduling_is_enabled(h):
    h.set_override("a", SCHED, enabled=True)
    outsider = h.m("other").owner_auth  # authenticated, not a member of org-shared
    assert h.create("a", auth=outsider).status_code == 403
    assert h.reflect("a", auth=outsider).status_code == 403
    assert h.schedule_count() == 0


def test_viewer_reads_the_reflection_but_cannot_mutate(h):
    h.set_override("a", SCHED, enabled=True)
    r = h.reflect("a", auth=h.viewer_auth)
    assert r.status_code == 200 and r.json() == {"enabled": True}
    assert h.create("a", auth=h.viewer_auth).status_code == 403


def test_reflection_discloses_only_the_boolean(h):
    h.set_override("a", SCHED, enabled=True)
    assert set(h.reflect("a").json()) == {"enabled"}


def test_resolver_failure_is_raised_never_an_enable(h, monkeypatch):
    h.set_override("a", SCHED, enabled=True)

    def _boom(**_kwargs):
        raise RuntimeError("override storage unavailable")

    monkeypatch.setattr(schedules_mod, "resolve_capability", _boom)
    with pytest.raises(RuntimeError):
        h.create("a")
    with pytest.raises(RuntimeError):
        h.reflect("a")
    assert h.schedule_count() == 0


# --------------------------------------------------------------------------- #
# Scheduling — the worker-side gates use the same decision
# --------------------------------------------------------------------------- #
def _live_schedule(h, key: str) -> str:
    """An enabled schedule row, created through the service at _T0."""
    with h.make() as s:
        req = s.get(ScoutRequest, f"sr-{key}")
        schedule = schedules_mod.create_schedule(s, request=req, interval="daily", now=_T0)
        s.commit()
        return schedule.id


def _tick(h, schedule_id: str) -> dict:
    occurrence = _T0 + timedelta(hours=24)
    with h.make() as s:
        result = schedules_mod.run_schedule_tick(
            s, schedule_id=schedule_id, occurrence_at=occurrence,
            now=occurrence + timedelta(minutes=1),
        )
        s.commit()
    return result


def test_tick_fans_out_under_a_workspace_override_with_global_off(h):
    schedule_id = _live_schedule(h, "a")
    h.set_override("a", SCHED, enabled=True)
    result = _tick(h, schedule_id)
    assert result["outcome"] == "fanned_out" and result["chained"] is True


def test_tick_is_a_no_op_under_a_disable_override_with_global_on(h, monkeypatch):
    _global(monkeypatch, SCHED, True)
    schedule_id = _live_schedule(h, "a")  # seeds a tick: the workspace was enabled
    h.set_override("a", SCHED, enabled=False)
    result = _tick(h, schedule_id)
    assert result == {"outcome": "feature_disabled", "run_enqueued": False, "chained": False}


def test_tick_is_a_no_op_under_the_safety_ceiling(h, monkeypatch):
    _global(monkeypatch, SCHED, True)
    schedule_id = _live_schedule(h, "a")
    _ceiling_blocks(monkeypatch, SCHED)
    assert _tick(h, schedule_id)["outcome"] == "feature_disabled"


def test_service_create_seeds_only_where_the_workspace_resolves_enabled(h):
    # Workspace "b" carries the enable override; "a" does not — "a" must not seed.
    h.set_override("b", SCHED, enabled=True)
    _live_schedule(h, "a")
    with h.make() as s:
        assert s.scalar(
            select(func.count()).select_from(Job)
            .where(Job.job_type == JobType.SCOUT_SCHEDULE_TICK.value)
        ) == 0


# --------------------------------------------------------------------------- #
# RSS — connector selection follows the resolver, and the operator view agrees
# --------------------------------------------------------------------------- #
_RSS_MATRIX = [
    (False, None, False),
    (True, None, True),
    (True, False, False),  # a disable override beats a true global flag
    # RSS is not workspace_enableable: a stored enable override is not honourable and
    # resolves to the secure default, so it can neither enable nor keep enabled.
    (False, True, False),
    (True, True, False),
]


@pytest.mark.parametrize(("global_on", "override", "expected"), _RSS_MATRIX)
def test_rss_selection_and_operator_view_agree(h, monkeypatch, global_on, override, expected):
    _global(monkeypatch, RSS, global_on)
    if override is not None:
        # Stored directly: the override service would refuse an RSS enable (422), but a
        # row that exists anyway must still never enable.
        h.set_override("a", RSS, enabled=override)

    mk = h.m("a")
    with h.make() as s:
        connector = get_connector(
            db=s, organization_id=mk.org_id, workspace_id=mk.ws_id,
            source_types=["rss_news"], market="Dallas, TX",
        )
    assert isinstance(connector, RssNewsConnector) is expected
    if not expected:
        assert isinstance(connector, FixtureConnector)
    assert h.operator_effective("a", RSS)["effective_enabled"] is expected


def test_rss_safety_ceiling_blocks_selection(h, monkeypatch):
    _global(monkeypatch, RSS, True)
    _ceiling_blocks(monkeypatch, RSS)
    mk = h.m("a")
    with h.make() as s:
        connector = get_connector(
            db=s, organization_id=mk.org_id, workspace_id=mk.ws_id,
            source_types=["rss_news"], market="Dallas, TX",
        )
    assert isinstance(connector, FixtureConnector)
    assert h.operator_effective("a", RSS)["decided_by"] == "safety_ceiling"


def test_rss_disable_override_is_workspace_scoped(h, monkeypatch):
    _global(monkeypatch, RSS, True)
    h.set_override("a", RSS, enabled=False)
    with h.make() as s:
        a = get_connector(db=s, organization_id="org-shared", workspace_id="ws-a",
                          source_types=["rss_news"], market="Dallas, TX")
        b = get_connector(db=s, organization_id="org-shared", workspace_id="ws-b",
                          source_types=["rss_news"], market="Dallas, TX")
    assert isinstance(a, FixtureConnector)
    assert isinstance(b, RssNewsConnector)


def test_pipeline_selects_the_connector_for_the_requests_own_tenant(h, monkeypatch):
    """The scout pipeline hands the request's durable tenant scope to the selector."""
    import app.jobs.pipeline as pipeline_mod

    class _Stop(Exception):
        pass

    seen: dict = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        raise _Stop

    monkeypatch.setattr(pipeline_mod, "get_connector", _spy)
    with h.make() as s, pytest.raises(_Stop):
        pipeline_mod._run(s, "sr-b")
    assert seen["db"] is not None
    assert (seen["organization_id"], seen["workspace_id"]) == ("org-shared", "ws-b")
