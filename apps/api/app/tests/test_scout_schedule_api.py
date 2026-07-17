"""SB-C: route + authorization + lifecycle + hardening tests for the customer-facing
scouting-schedule API.

    GET    /api/v1/workspaces/{ws}/scout-requests/{id}/schedule
    POST   /api/v1/workspaces/{ws}/scout-requests/{id}/schedule
    POST   /api/v1/workspaces/{ws}/scout-requests/{id}/schedule/pause
    POST   /api/v1/workspaces/{ws}/scout-requests/{id}/schedule/resume
    DELETE /api/v1/workspaces/{ws}/scout-requests/{id}/schedule

Runs against the throwaway four-market demo seed (Dallas, London, Lagos, Nairobi) with
``get_db`` overridden. The seed creates no schedules and no durable ``Job`` rows, so any
schedule/tick observed here was produced by the code under test.

Covers only what the backend service suite (``test_scout_schedules.py``) does not: the
HTTP contract + customer-safe projection, role-based authorization (read-any / mutate-
editor), the dark-deploy feature gate on mutations, the derived ``state`` (including the
``activation_required`` dark-created policy), and the three mandatory hardening items
(transactional cap, already-active resume guard + self-count, pause idempotency). The
true concurrent-cap race is proven at compile time here and, when ``TEST_POSTGRES_URL``
is set, against a live PostgreSQL in ``test_scout_schedule_concurrency.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.enums import Role
from app.core.security import create_access_token
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.jobs.models import Job
from app.jobs.status import JobType
from app.main import app
from app.organizations.models import OrganizationMember, User
from app.scouting_requests.models import ScoutRequest, ScoutSchedule

API = get_settings().api_prefix

_MARKETS = ("dallas", "london", "lagos", "nairobi")

# The only fields the customer-safe schedule projection may ever expose.
_ALLOWED_KEYS = {
    "id",
    "scout_request_id",
    "location_id",
    "interval",
    "state",
    "enabled",
    "next_run_at",
    "last_tick_at",
    "created_at",
    "updated_at",
}
# Internal columns/tokens that must NEVER surface in a schedule projection.
_FORBIDDEN_KEYS = (
    "organization_id",
    "workspace_id",
    "payload",
    "payload_hash",
    "idempotency_key",
    "contract_version",
    "lease_token",
    "worker_id",
)


class _Harness:
    def __init__(self, client, factory):
        self.client = client
        self.factory = factory
        self.ws = seed_mod.sid("ws")
        self.org = seed_mod.sid("org")
        self.owner_auth = {"Authorization": f"Bearer {create_access_token(seed_mod.sid('user'))}"}
        self.viewer_auth = {"Authorization": f"Bearer {create_access_token('viewer-user')}"}
        self.outsider_auth = {"Authorization": f"Bearer {create_access_token('outsider-user')}"}

    def req(self, market_key: str) -> str:
        return seed_mod.sid("scout", market_key)

    def _url(self, request_id: str, *, ws: str | None = None, suffix: str = "") -> str:
        return (
            f"{API}/workspaces/{ws or self.ws}/scout-requests/{request_id}/schedule{suffix}"
        )

    def get(self, market="dallas", *, request_id=None, ws=None, auth=None):
        return self.client.get(
            self._url(request_id or self.req(market), ws=ws),
            headers=self.owner_auth if auth is None else auth,
        )

    def create(self, market="dallas", *, interval="daily", ws=None, auth=None):
        return self.client.post(
            self._url(self.req(market), ws=ws),
            json={"interval": interval},
            headers=self.owner_auth if auth is None else auth,
        )

    def pause(self, market="dallas", *, auth=None):
        return self.client.post(
            self._url(self.req(market), suffix="/pause"),
            headers=self.owner_auth if auth is None else auth,
        )

    def resume(self, market="dallas", *, auth=None):
        return self.client.post(
            self._url(self.req(market), suffix="/resume"),
            headers=self.owner_auth if auth is None else auth,
        )

    def delete(self, market="dallas", *, auth=None):
        return self.client.delete(
            self._url(self.req(market)),
            headers=self.owner_auth if auth is None else auth,
        )

    def ticks(self, market="dallas") -> list[Job]:
        with self.factory() as s:
            return list(
                s.execute(
                    select(Job).where(
                        Job.scout_request_id == self.req(market),
                        Job.job_type == JobType.SCOUT_SCHEDULE_TICK.value,
                    )
                ).scalars()
            )


@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scout_schedule_api")
    engine = create_engine(
        f"sqlite:///{tmp/'sched_api.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        with make() as s:
            # A non-member (exists + active, not in the demo org) → 403 everywhere.
            s.add(
                User(
                    id="outsider-user",
                    email="outsider@example.com",
                    full_name="Outsider",
                    hashed_password="x",
                    is_active=True,
                )
            )
            # A real workspace member with a view-only role: may read, never mutate.
            s.add(
                User(
                    id="viewer-user",
                    email="viewer@example.com",
                    full_name="Viewer",
                    hashed_password="x",
                    is_active=True,
                )
            )
            s.add(
                OrganizationMember(
                    id="viewer-member",
                    organization_id=seed_mod.sid("org"),
                    user_id="viewer-user",
                    role=Role.VIEWER.value,
                )
            )
            s.commit()
        yield make
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


@pytest.fixture
def h(factory):
    """A TestClient harness with ``get_db`` overridden onto the seeded engine.

    An autouse cleanup below wipes every schedule + schedule tick after each test, so
    the module-scoped database starts each test with no schedules and the per-workspace
    cap begins clean.
    """

    def _override_get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield _Harness(TestClient(app), factory)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset(factory, monkeypatch):
    # Feature is dark unless a test opts in; state is cleaned after every test.
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", False)
    yield
    with factory() as s:
        s.query(Job).filter(
            Job.job_type == JobType.SCOUT_SCHEDULE_TICK.value
        ).delete(synchronize_session=False)
        s.query(ScoutSchedule).delete(synchronize_session=False)
        s.commit()


@pytest.fixture
def flag_on():
    get_settings().scout_scheduling_enabled = True


def _seed_schedule(factory, market="dallas", *, enabled=True, interval="daily") -> str:
    """Create a schedule directly through the service (bypassing the feature gate).

    Used to stage a record — e.g. a dark-created ``activation_required`` schedule —
    without going through the flag-gated HTTP mutation.
    """
    from app.scouting_requests import schedules as sched

    with factory() as s:
        req = s.get(ScoutRequest, seed_mod.sid("scout", market))
        schedule = sched.create_schedule(s, request=req, interval=interval, enabled=enabled)
        s.commit()
        return schedule.id


# --------------------------------------------------------------------------- #
# Contract + customer-safe projection
# --------------------------------------------------------------------------- #
class TestContract:
    def test_create_returns_safe_projection(self, h, flag_on):
        r = h.create("dallas", interval="weekly")
        assert r.status_code == 201
        body = r.json()
        assert set(body) == _ALLOWED_KEYS
        for key in _FORBIDDEN_KEYS:
            assert key not in body, key
        assert body["interval"] == "weekly"
        assert body["enabled"] is True
        assert body["scout_request_id"] == h.req("dallas")

    def test_get_returns_same_shape(self, h, flag_on):
        h.create("london")
        body = h.get("london").json()
        assert set(body) == _ALLOWED_KEYS
        assert body["interval"] == "daily"

    def test_get_404_when_no_schedule(self, h):
        r = h.get("nairobi")
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Feature flag: mutations are dark by default, reads are not
# --------------------------------------------------------------------------- #
class TestFeatureGate:
    def test_create_disabled_returns_503(self, h):
        r = h.create("dallas")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "capability_unavailable"

    def test_pause_resume_delete_disabled_return_503(self, h, factory):
        _seed_schedule(factory, "dallas")  # a record exists, but the feature is dark
        assert h.pause("dallas").status_code == 503
        assert h.resume("dallas").status_code == 503
        assert h.delete("dallas").status_code == 503

    def test_read_allowed_while_dark(self, h, factory):
        _seed_schedule(factory, "dallas")
        r = h.get("dallas")
        assert r.status_code == 200
        # A dark-created enabled schedule is inert: enabled but not actually running.
        assert r.json()["state"] == "activation_required"


# --------------------------------------------------------------------------- #
# Lifecycle over HTTP + derived state
# --------------------------------------------------------------------------- #
class TestLifecycle:
    def test_create_is_active_and_seeds_one_tick_when_live(self, h, flag_on):
        body = h.create("dallas").json()
        assert body["state"] == "active"
        assert body["next_run_at"] is not None
        assert len(h.ticks("dallas")) == 1

    def test_pause_then_resume_round_trip(self, h, flag_on):
        h.create("dallas")
        paused = h.pause("dallas").json()
        assert paused["state"] == "paused"
        assert paused["enabled"] is False
        assert paused["next_run_at"] is None
        resumed = h.resume("dallas").json()
        assert resumed["state"] == "active"
        assert resumed["enabled"] is True
        assert resumed["next_run_at"] is not None

    def test_delete_removes_and_get_404s(self, h, flag_on):
        h.create("dallas")
        assert h.delete("dallas").status_code == 204
        assert h.get("dallas").status_code == 404

    def test_pause_is_idempotent(self, h, flag_on):
        h.create("dallas")
        first = h.pause("dallas")
        second = h.pause("dallas")
        assert first.status_code == 200 and second.status_code == 200
        assert second.json()["state"] == "paused"


# --------------------------------------------------------------------------- #
# Conflict + validation mapping
# --------------------------------------------------------------------------- #
class TestConflicts:
    def test_second_schedule_for_request_conflicts(self, h, flag_on):
        h.create("dallas")
        r = h.create("dallas")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "conflict"

    def test_cap_of_four_active_enforced_over_http(self, h, flag_on):
        for market in _MARKETS:  # four enabled schedules — at the cap
            assert h.create(market).status_code == 201
        # A fifth enabled schedule in the same workspace is rejected.
        with h.factory() as s:
            tpl = s.get(ScoutRequest, h.req("dallas"))
            fifth = ScoutRequest(
                organization_id=tpl.organization_id,
                workspace_id=tpl.workspace_id,
                brand_id=tpl.brand_id,
                name="fifth",
                status="draft",
                source_types=[],
                keywords=[],
                stats={},
            )
            s.add(fifth)
            s.commit()
            fifth_id = fifth.id
        r = h.client.post(
            h._url(fifth_id), json={"interval": "daily"}, headers=h.owner_auth
        )
        assert r.status_code == 409

    def test_invalid_interval_rejected_422(self, h, flag_on):
        r = h.client.post(
            h._url(h.req("dallas")), json={"interval": "hourly"}, headers=h.owner_auth
        )
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Dark-created activation policy (hardening item 2)
# --------------------------------------------------------------------------- #
class TestActivationPolicy:
    def test_dark_created_is_activation_required_and_not_auto_seeded(self, h, factory):
        # Created enabled while dark: record exists, no tick, state activation_required.
        _seed_schedule(factory, "dallas", enabled=True)
        assert h.get("dallas").json()["state"] == "activation_required"
        assert h.ticks("dallas") == []
        # Flag flips on: still NOT auto-seeded — the chain never starts by itself.
        get_settings().scout_scheduling_enabled = True
        assert h.get("dallas").json()["state"] == "activation_required"
        assert h.ticks("dallas") == []

    def test_explicit_resume_activates_and_seeds_exactly_one_tick(self, h, factory, flag_on):
        _seed_schedule(factory, "dallas", enabled=True)  # dark-created, inert
        r = h.resume("dallas")
        assert r.status_code == 200
        assert r.json()["state"] == "active"
        assert len(h.ticks("dallas")) == 1


# --------------------------------------------------------------------------- #
# Already-active resume guard + self-count (hardening item 3)
# --------------------------------------------------------------------------- #
class TestResumeGuard:
    def test_resume_of_active_is_noop_no_duplicate_tick(self, h, flag_on):
        h.create("dallas")
        assert len(h.ticks("dallas")) == 1
        # Compare the persisted next_run_at across the resume (both DB reads, so no
        # aware-vs-naive serialization skew) — it must be left exactly as it was.
        before = h.get("dallas").json()["next_run_at"]
        again = h.resume("dallas").json()
        after = h.get("dallas").json()["next_run_at"]
        assert len(h.ticks("dallas")) == 1  # no second tick
        assert after == before
        assert again["state"] == "active"

    def test_resume_does_not_count_itself_toward_cap(self, h, flag_on):
        # Fill the workspace to the cap of four enabled schedules...
        for market in _MARKETS:
            assert h.create(market).status_code == 201
        # ...then resume one of them. It is already counted; resuming must not
        # double-count and trip the cap on the schedule itself.
        r = h.resume("dallas")
        assert r.status_code == 200
        assert r.json()["state"] == "active"


# --------------------------------------------------------------------------- #
# AuthN / AuthZ — read is open to members, mutation is editors-only
# --------------------------------------------------------------------------- #
class TestSecurity:
    def test_unauthenticated_401(self, h):
        r = h.client.get(h._url(h.req("dallas")))
        assert r.status_code == 401

    def test_non_member_forbidden(self, h, factory, flag_on):
        _seed_schedule(factory, "dallas")
        assert h.get("dallas", auth=h.outsider_auth).status_code == 403
        assert h.create("london", auth=h.outsider_auth).status_code == 403
        assert h.pause("dallas", auth=h.outsider_auth).status_code == 403

    def test_viewer_can_read_but_not_mutate(self, h, factory, flag_on):
        _seed_schedule(factory, "dallas")
        # Read: allowed for any member, including a view-only role.
        assert h.get("dallas", auth=h.viewer_auth).status_code == 200
        # Mutations: forbidden for a view-only role.
        assert h.create("london", auth=h.viewer_auth).status_code == 403
        assert h.pause("dallas", auth=h.viewer_auth).status_code == 403
        assert h.resume("dallas", auth=h.viewer_auth).status_code == 403
        assert h.delete("dallas", auth=h.viewer_auth).status_code == 403

    def test_unknown_request_404(self, h, flag_on):
        r = h.client.get(h._url("scout-nope"), headers=h.owner_auth)
        assert r.status_code == 404

    def test_cross_workspace_request_404(self, h, flag_on):
        r = h.client.post(
            h._url(h.req("dallas"), ws="ws-nope"),
            json={"interval": "daily"},
            headers=h.owner_auth,
        )
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Transactional cap lock (hardening item 1) — compile-level proof
# --------------------------------------------------------------------------- #
class TestCapLockCompiles:
    def test_workspace_lock_emits_for_update_on_postgres(self):
        from sqlalchemy.dialects import postgresql

        from app.scouting_requests.schedules import _workspace_lock_select

        sql = str(
            _workspace_lock_select("ws-1").compile(dialect=postgresql.dialect())
        ).upper()
        assert "FOR UPDATE" in sql
        assert "WORKSPACES" in sql


# --------------------------------------------------------------------------- #
# Reads never mutate schedules or jobs
# --------------------------------------------------------------------------- #
class TestNonMutating:
    def test_get_never_writes(self, h, factory, flag_on):
        _seed_schedule(factory, "dallas")

        def snapshot():
            with factory() as s:
                sched_rows = s.execute(
                    select(ScoutSchedule.id, ScoutSchedule.enabled).order_by(ScoutSchedule.id)
                ).all()
                jobs = s.scalar(select(func.count()).select_from(Job))
                return sched_rows, jobs

        before = snapshot()
        for _ in range(3):
            h.get("dallas")
        assert snapshot() == before
