"""Phase 4B-A: fail-closed opportunity-feedback capability-gate wiring tests.

The live feedback gate ``app.feedback.routes._require_feedback_feature`` no longer reads
the raw ``opportunity_feedback_enabled`` flag directly; it now routes every decision
through the central deny-biased resolver (:func:`app.capabilities.resolver.resolve_capability`)
using the authenticated tenant context's server-resolved organization/workspace identity.

These tests prove the wiring is *dark and fail-closed* over the HTTP boundary, using
isolated, per-test SQLite state (a fresh engine + a real ``workspace_capability_overrides``
table). They never touch the shipped global flags, the production environment, or the
runtime canary workspace. All override rows here are synthetic test intent on synthetic
workspaces — creating one is not runtime activation.

Coverage (plan §6 "Required 4B-A tests"):

* Default dark denial: global flag ``False`` + no override → 503, no write.
* Authorized isolated enable override for workspace A → A alone passes the gate (201/200).
* Explicit disable override → still rejected.
* Cross-workspace isolation: an enable on A does not authorize sibling workspace B
  (same org) nor a workspace in another organization.
* Tenant-mismatched override (right workspace id, wrong organization scope) → rejected.
* Clearing the override → restores rejection.
* Resolver/dependency failure → never enables; rejected before any write (fail-closed).
* Unrelated capability resolutions (scheduling / RSS) are unchanged by a feedback override.
* The disabled decision keeps the repository-standard ``503 capability_unavailable`` shape.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

import app.feedback.routes as feedback_routes
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.capabilities.resolver import resolve_capability
from app.core.config import get_settings
from app.core.enums import Role
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.feedback.models import OpportunityFeedback
from app.intelligence.records import SignalIntelligenceRecord
from app.main import app
from app.opportunities.models import Opportunity

API = get_settings().api_prefix
FEEDBACK = Capability.OPPORTUNITY_FEEDBACK.value


@dataclass
class Market:
    """Handles for one isolated tenant graph."""

    key: str
    org_id: str
    ws_id: str
    opp_id: str
    record_id: str
    owner_auth: dict


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _seed_market(s, key: str, org_id: str, *, create_org: bool = True) -> Market:
    """A tenant graph (workspace + owner + opportunity + record) under ``org_id``.

    ``create_org=False`` reuses an already-seeded organization so two workspaces can
    share one organization (used to prove same-org cross-workspace isolation).
    """
    from app.brands.models import Brand
    from app.organizations.models import Organization, OrganizationMember, User, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    ws_id = f"ws-{key}"
    owner = f"owner-{key}"
    if create_org:
        s.add(Organization(id=org_id, name=f"Org {org_id}", slug=org_id))
        s.flush()
    s.add(Workspace(id=ws_id, organization_id=org_id, name=f"WS {key}", slug=f"ws-{key}"))
    s.add(User(id=owner, email=f"{owner}@example.com", full_name=owner,
               hashed_password="x", is_active=True))
    s.flush()
    s.add(OrganizationMember(id=f"m-{owner}", organization_id=org_id, user_id=owner,
                             role=Role.OWNER.value))
    s.add(Brand(id=f"br-{key}", organization_id=org_id, workspace_id=ws_id, name=f"Brand {key}"))
    s.flush()
    s.add(ScoutRequest(id=f"sr-{key}", organization_id=org_id, workspace_id=ws_id,
                       brand_id=f"br-{key}", name=f"scout {key}"))
    s.flush()
    s.add(RawSignal(id=f"raw-{key}", organization_id=org_id, workspace_id=ws_id,
                    scout_request_id=f"sr-{key}", source_type="rss_news", content="c"))
    s.flush()
    s.add(NormalizedSignal(id=f"ns-{key}", organization_id=org_id, workspace_id=ws_id,
                           scout_request_id=f"sr-{key}", raw_signal_id=f"raw-{key}",
                           source_type="rss_news", excerpt="e", content_hash=f"h-{key}"))
    s.flush()
    s.add(Opportunity(id=f"opp-{key}", organization_id=org_id, workspace_id=ws_id,
                      brand_id=f"br-{key}", scout_request_id=f"sr-{key}", title=f"Opp {key}",
                      classification="emerging", decision="monitor", opportunity_score=50))
    s.flush()
    s.add(SignalIntelligenceRecord(
        id=f"sir-{key}", organization_id=org_id, workspace_id=ws_id, scout_request_id=f"sr-{key}",
        normalized_signal_id=f"ns-{key}", opportunity_id=f"opp-{key}", analysis_version="4b",
        scoring_version="4b.1", fingerprint=f"fp-{key}", enricher="deterministic",
        accepted=True, classification="emerging"))
    s.flush()
    return Market(key=key, org_id=org_id, ws_id=ws_id, opp_id=f"opp-{key}",
                  record_id=f"sir-{key}", owner_auth=_auth(owner))


class _Harness:
    def __init__(self, client, factory, markets):
        self.client = client
        self.factory = factory
        self.markets: dict[str, Market] = markets

    def m(self, key: str) -> Market:
        return self.markets[key]

    def url(self, ws: str, opp: str) -> str:
        return f"{API}/workspaces/{ws}/opportunities/{opp}/feedback"

    def post(self, key: str):
        mk = self.m(key)
        return self.client.post(
            self.url(mk.ws_id, mk.opp_id),
            json={"intelligence_record_id": mk.record_id, "is_useful": True},
            headers=mk.owner_auth,
        )

    def get(self, key: str):
        mk = self.m(key)
        return self.client.get(self.url(mk.ws_id, mk.opp_id), headers=mk.owner_auth)

    def set_override(self, key: str, *, enabled: bool, organization_id: str | None = None) -> None:
        """Persist a synthetic per-workspace override (test intent, not activation)."""
        mk = self.m(key)
        make, _ = self.factory
        with make() as s:
            s.add(WorkspaceCapabilityOverride(
                organization_id=organization_id or mk.org_id,
                workspace_id=mk.ws_id,
                capability=FEEDBACK,
                enabled=enabled,
            ))
            s.commit()

    def clear_overrides(self) -> None:
        make, _ = self.factory
        with make() as s:
            s.query(WorkspaceCapabilityOverride).delete(synchronize_session=False)
            s.commit()

    def feedback_count(self) -> int:
        make, _ = self.factory
        with make() as s:
            return int(s.scalar(select(func.count()).select_from(OpportunityFeedback)) or 0)


@pytest.fixture
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("feedback_capability_gate")
    engine = create_engine(
        f"sqlite:///{tmp/'gate.db'}",
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

    # The gate reads the process-wide settings flag through the resolver; assert and pin
    # it to the shipped dark default so no ambient mutation can leak in.
    settings = get_settings()
    assert settings.opportunity_feedback_enabled is False
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield _Harness(TestClient(app), (make, markets), markets)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# --------------------------------------------------------------------------- #
# A. Default dark denial — flag False, no override
# --------------------------------------------------------------------------- #
def test_default_dark_denies_and_writes_nothing(h):
    r = h.post("a")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "capability_unavailable"
    assert h.get("a").status_code == 503
    assert h.feedback_count() == 0


def test_seeded_org_shares_flag_false_baseline(h):
    # The resolver decides disabled via GLOBAL_CONFIGURATION for a real workspace scope.
    make, _ = h.factory
    with make() as s:
        res = resolve_capability(
            session=s, settings=get_settings(),
            capability=Capability.OPPORTUNITY_FEEDBACK,
            organization_id="org-shared", workspace_id="ws-a",
        )
    assert res.effective_enabled is False
    assert res.decided_by.value == "global_configuration"


# --------------------------------------------------------------------------- #
# B. Authorized isolated enable override — workspace A alone passes
# --------------------------------------------------------------------------- #
def test_enable_override_opens_gate_for_that_workspace(h):
    h.set_override("a", enabled=True)
    r = h.post("a")
    assert r.status_code == 201, r.text
    assert h.get("a").status_code == 200
    # Global flag stayed False the whole time — activation was override-scoped only.
    assert get_settings().opportunity_feedback_enabled is False


# --------------------------------------------------------------------------- #
# C. Cross-scope isolation — an enable on A authorizes neither B nor another org
# --------------------------------------------------------------------------- #
def test_enable_on_a_does_not_authorize_sibling_workspace_b(h):
    h.set_override("a", enabled=True)
    assert h.post("a").status_code == 201
    # Sibling workspace in the SAME organization has no override → still dark.
    assert h.post("b").status_code == 503
    assert h.get("b").status_code == 503


def test_enable_in_one_org_does_not_leak_to_another_org(h):
    h.set_override("a", enabled=True)
    assert h.post("other").status_code == 503


def test_tenant_mismatched_override_is_not_honored(h):
    # An override row for ws-a but stamped with the WRONG organization scope must not
    # enable: the resolver deny-biases a cross-tenant/inconsistent row to "no override".
    h.set_override("a", enabled=True, organization_id="org-other")
    assert h.post("a").status_code == 503
    assert h.feedback_count() == 0


# --------------------------------------------------------------------------- #
# D/F. Explicit non-allow — a disable override keeps the gate closed
# --------------------------------------------------------------------------- #
def test_disable_override_keeps_gate_closed(h):
    h.set_override("a", enabled=False)
    assert h.post("a").status_code == 503
    assert h.feedback_count() == 0


def test_clearing_override_restores_rejection(h):
    h.set_override("a", enabled=True)
    assert h.post("a").status_code == 201
    h.clear_overrides()
    assert h.post("a").status_code == 503
    assert h.get("a").status_code == 503


# --------------------------------------------------------------------------- #
# E. Resolver / dependency failure — never enables (fail-closed)
# --------------------------------------------------------------------------- #
def test_resolver_failure_fails_closed_before_any_write(h, monkeypatch):
    # Even with an enable override present, a resolver failure must never fail open.
    h.set_override("a", enabled=True)

    def _boom(**_kwargs):
        raise RuntimeError("resolver/storage unavailable")

    monkeypatch.setattr(feedback_routes, "resolve_capability", _boom)
    # The failure propagates (a 5xx for a real client) instead of ever returning a 201:
    # the gate never swallows it into an enable. TestClient re-raises server exceptions.
    with pytest.raises(RuntimeError):
        h.post("a")
    assert h.feedback_count() == 0  # rejected before any write


# --------------------------------------------------------------------------- #
# G. Unrelated capabilities are unaffected by a feedback override
# --------------------------------------------------------------------------- #
def test_feedback_override_does_not_alter_other_capability_resolutions(h):
    h.set_override("a", enabled=True)
    make, _ = h.factory
    with make() as s:
        for cap in (Capability.SCOUT_SCHEDULING, Capability.CONNECTOR_RSS):
            res = resolve_capability(
                session=s, settings=get_settings(), capability=cap,
                organization_id="org-shared", workspace_id="ws-a",
            )
            assert res.effective_enabled is False, cap
            assert res.decided_by.value == "global_configuration", cap
