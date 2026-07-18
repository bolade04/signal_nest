"""3C-C: route + authorization + feature-gate + isolation tests for the
feature-gated opportunity-feedback API.

    POST /api/v1/workspaces/{ws}/opportunities/{opp}/feedback
    GET  /api/v1/workspaces/{ws}/opportunities/{opp}/feedback

Runs against a fully self-contained four-market graph (Dallas / London / Lagos /
Nairobi) — each market its own organization + workspace + members + opportunity +
immutable intelligence record — so cross-tenant isolation is proven at the HTTP
boundary, not just in the service. ``get_db`` is overridden onto the seeded engine and
SQLite foreign keys are enforced so scope really bites.

Covers only the HTTP contract the service suite (``test_opportunity_feedback.py``) does
not: the strict request/safe response projection, editor-only authorization on *both*
submit and read, the dark-deploy feature gate on *both* operations, scope/IDOR
(unknown/cross-workspace opportunity and record), append-only history + pagination,
audit emission, capture-only regression, and route registration. A dedicated
concurrency proof lives in ``test_opportunity_feedback_concurrency.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
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

_MARKETS = ("dallas", "london", "lagos", "nairobi")

# The only fields the customer-safe feedback projection may ever expose.
_ALLOWED_KEYS = {
    "id",
    "opportunity_id",
    "intelligence_record_id",
    "is_useful",
    "reason_code",
    "submitted_by_user_id",
    "analysis_version",
    "scoring_version",
    "created_at",
}
# Internals that must NEVER surface in a feedback projection.
_FORBIDDEN_KEYS = (
    "organization_id",
    "workspace_id",
    "fingerprint",
    "updated_at",
)


@dataclass
class Market:
    """Handles for one isolated market seeded into the harness."""

    key: str
    org_id: str
    ws_id: str
    opp_id: str
    record_id: str
    owner_auth: dict
    marketer_auth: dict
    viewer_auth: dict


def _reset_rate_limiter() -> None:
    """Clear the shared in-memory fixed-window rate limiter.

    The app mounts a naive 240-req/60s limiter keyed by client host; every TestClient
    request shares the same host and the same in-process ``_hits`` bucket. This module
    issues many requests (the cross-market pair matrix especially), so we wipe the
    bucket around each test to leave no cumulative residue that would 429 later suites.
    """
    from app.core.middleware import RateLimitMiddleware

    stack = getattr(app, "middleware_stack", None)
    while stack is not None:
        if isinstance(stack, RateLimitMiddleware):
            stack._hits.clear()
            return
        stack = getattr(stack, "app", None)


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _seed_market(s, key: str) -> Market:
    """Create a full, independent tenant graph + members for one market."""
    from app.brands.models import Brand
    from app.organizations.models import Organization, OrganizationMember, User, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    org_id, ws_id = f"org-{key}", f"ws-{key}"
    s.add(Organization(id=org_id, name=f"Org {key}", slug=f"org-{key}"))
    s.flush()
    s.add(Workspace(id=ws_id, organization_id=org_id, name=f"WS {key}", slug=f"ws-{key}"))
    for role, uid in (
        (Role.OWNER, f"owner-{key}"),
        (Role.MARKETER, f"mkt-{key}"),
        (Role.VIEWER, f"view-{key}"),
    ):
        s.add(
            User(id=uid, email=f"{uid}@example.com", full_name=uid, hashed_password="x",
                 is_active=True)
        )
        s.flush()
        s.add(OrganizationMember(id=f"m-{uid}", organization_id=org_id, user_id=uid,
                                 role=role.value))
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
        normalized_signal_id=f"ns-{key}", opportunity_id=f"opp-{key}", analysis_version="3c",
        scoring_version="3c.1", fingerprint=f"fp-{key}", enricher="deterministic",
        accepted=True, classification="emerging"))
    s.flush()
    return Market(
        key=key, org_id=org_id, ws_id=ws_id, opp_id=f"opp-{key}", record_id=f"sir-{key}",
        owner_auth=_auth(f"owner-{key}"), marketer_auth=_auth(f"mkt-{key}"),
        viewer_auth=_auth(f"view-{key}"),
    )


def _seed_sibling_opportunity(s, key: str) -> tuple[str, str]:
    """A second opportunity + record in the *same* workspace as ``key``."""
    from app.signals.models import NormalizedSignal, RawSignal

    org_id, ws_id = f"org-{key}", f"ws-{key}"
    s.add(RawSignal(id=f"raw-{key}-2", organization_id=org_id, workspace_id=ws_id,
                    scout_request_id=f"sr-{key}", source_type="rss_news", content="c2"))
    s.flush()
    s.add(NormalizedSignal(id=f"ns-{key}-2", organization_id=org_id, workspace_id=ws_id,
                           scout_request_id=f"sr-{key}", raw_signal_id=f"raw-{key}-2",
                           source_type="rss_news", excerpt="e2", content_hash=f"h-{key}-2"))
    s.flush()
    s.add(Opportunity(id=f"opp-{key}-2", organization_id=org_id, workspace_id=ws_id,
                      brand_id=f"br-{key}", scout_request_id=f"sr-{key}", title="Second",
                      classification="emerging", decision="monitor", opportunity_score=50))
    s.flush()
    s.add(SignalIntelligenceRecord(
        id=f"sir-{key}-2", organization_id=org_id, workspace_id=ws_id, scout_request_id=f"sr-{key}",
        normalized_signal_id=f"ns-{key}-2", opportunity_id=f"opp-{key}-2", analysis_version="3c",
        scoring_version="3c.1", fingerprint=f"fp-{key}-2", enricher="deterministic",
        accepted=True, classification="emerging"))
    s.flush()
    return f"opp-{key}-2", f"sir-{key}-2"


class _Harness:
    def __init__(self, client, factory, markets):
        self.client = client
        self.factory = factory
        self.markets: dict[str, Market] = markets
        self.admin_auth = _auth("admin-dallas")
        self.outsider_auth = _auth("outsider-user")

    def m(self, key="dallas") -> Market:
        return self.markets[key]

    def url(self, ws: str, opp: str) -> str:
        return f"{API}/workspaces/{ws}/opportunities/{opp}/feedback"

    def post(self, key="dallas", *, is_useful=True, reason_code=None, record_id=None,
             ws=None, opp=None, auth=None, body=None):
        mk = self.m(key)
        payload = body if body is not None else {
            "intelligence_record_id": record_id or mk.record_id,
            "is_useful": is_useful,
            **({"reason_code": reason_code} if reason_code is not None else {}),
        }
        return self.client.post(
            self.url(ws or mk.ws_id, opp or mk.opp_id),
            json=payload,
            headers=mk.owner_auth if auth is None else auth,
        )

    def get(self, key="dallas", *, ws=None, opp=None, auth=None, params=None):
        mk = self.m(key)
        return self.client.get(
            self.url(ws or mk.ws_id, opp or mk.opp_id),
            headers=mk.owner_auth if auth is None else auth,
            params=params or {},
        )


@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("opportunity_feedback_api")
    engine = create_engine(
        f"sqlite:///{tmp/'feedback_api.db'}",
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
        markets = {key: _seed_market(s, key) for key in _MARKETS}
        _seed_sibling_opportunity(s, "dallas")
        # An admin member of the Dallas org (editor via a different role).
        from app.organizations.models import OrganizationMember, User

        s.add(User(id="admin-dallas", email="admin-dallas@example.com",
                   full_name="Admin", hashed_password="x", is_active=True))
        s.flush()
        s.add(OrganizationMember(id="m-admin-dallas", organization_id="org-dallas",
                                 user_id="admin-dallas", role=Role.ADMIN.value))
        # A non-member (exists + active, in no org) → 403 everywhere.
        s.add(User(id="outsider-user", email="outsider@example.com",
                   full_name="Outsider", hashed_password="x", is_active=True))
        s.commit()
    try:
        yield make, markets
    finally:
        engine.dispose()


@pytest.fixture
def h(factory):
    make, markets = factory

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

    app.dependency_overrides[get_db] = _override_get_db
    client = TestClient(app)
    _reset_rate_limiter()
    try:
        yield _Harness(client, make, markets)
    finally:
        app.dependency_overrides.clear()
        _reset_rate_limiter()


@pytest.fixture(autouse=True)
def _reset(factory, monkeypatch):
    # Feature is dark unless a test opts in; feedback is wiped after every test.
    monkeypatch.setattr(get_settings(), "opportunity_feedback_enabled", False)
    yield
    make, _ = factory
    with make() as s:
        s.query(AuditLog).delete(synchronize_session=False)
        s.query(OpportunityFeedback).delete(synchronize_session=False)
        s.commit()


@pytest.fixture
def flag_on():
    get_settings().opportunity_feedback_enabled = True


def _count(factory) -> int:
    make, _ = factory
    with make() as s:
        return int(s.scalar(select(func.count()).select_from(OpportunityFeedback)) or 0)


# --------------------------------------------------------------------------- #
# Feature gate — BOTH operations are dark by default
# --------------------------------------------------------------------------- #
class TestFeatureGate:
    def test_submit_disabled_returns_503(self, h):
        r = h.post("dallas", is_useful=True)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "capability_unavailable"

    def test_read_disabled_returns_503(self, h):
        r = h.get("dallas")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "capability_unavailable"

    def test_gate_writes_nothing_while_dark(self, h, factory):
        h.post("dallas", is_useful=True)
        assert _count(factory) == 0


# --------------------------------------------------------------------------- #
# Contract + customer-safe projection
# --------------------------------------------------------------------------- #
class TestContract:
    def test_submit_returns_safe_projection(self, h, flag_on):
        r = h.post("dallas", is_useful=True, reason_code="strong_evidence")
        assert r.status_code == 201
        body = r.json()
        assert set(body) == _ALLOWED_KEYS
        for key in _FORBIDDEN_KEYS:
            assert key not in body, key
        assert body["is_useful"] is True
        assert body["reason_code"] == "strong_evidence"
        assert body["opportunity_id"] == h.m("dallas").opp_id
        assert body["intelligence_record_id"] == h.m("dallas").record_id
        assert body["submitted_by_user_id"] == "owner-dallas"

    def test_provenance_is_copied_from_record_not_client(self, h, flag_on):
        r = h.post("dallas", is_useful=True)
        body = r.json()
        # Copied from sir-dallas (analysis_version=3c, scoring_version=3c.1).
        assert body["analysis_version"] == "3c"
        assert body["scoring_version"] == "3c.1"

    def test_reasonless_feedback_is_accepted(self, h, flag_on):
        r = h.post("london", is_useful=False)
        assert r.status_code == 201
        assert r.json()["reason_code"] is None


# --------------------------------------------------------------------------- #
# Strict request validation (422)
# --------------------------------------------------------------------------- #
class TestRequestValidation:
    def test_unknown_field_rejected(self, h, flag_on):
        r = h.post("dallas", body={
            "intelligence_record_id": h.m("dallas").record_id,
            "is_useful": True,
            "organization_id": "org-hack",  # smuggled scope
        })
        assert r.status_code == 422

    def test_client_supplied_provenance_rejected(self, h, flag_on):
        r = h.post("dallas", body={
            "intelligence_record_id": h.m("dallas").record_id,
            "is_useful": True,
            "fingerprint": "deadbeef",  # provenance is never client-supplied
        })
        assert r.status_code == 422

    def test_missing_is_useful_rejected(self, h, flag_on):
        r = h.post("dallas", body={"intelligence_record_id": h.m("dallas").record_id})
        assert r.status_code == 422

    def test_unknown_reason_rejected(self, h, flag_on):
        r = h.post("dallas", is_useful=True, reason_code="totally_made_up")
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Polarity (422 at the boundary)
# --------------------------------------------------------------------------- #
class TestPolarity:
    def test_positive_reason_rejected_when_not_useful(self, h, flag_on, factory):
        r = h.post("dallas", is_useful=False, reason_code="strong_evidence")
        assert r.status_code == 422
        assert _count(factory) == 0

    def test_negative_reason_rejected_when_useful(self, h, flag_on, factory):
        r = h.post("dallas", is_useful=True, reason_code="irrelevant")
        assert r.status_code == 422
        assert _count(factory) == 0


# --------------------------------------------------------------------------- #
# Append-only history + pagination
# --------------------------------------------------------------------------- #
class TestHistory:
    def test_history_is_append_only_and_reverse_chronological(self, h, flag_on):
        first = h.post("dallas", is_useful=True).json()
        second = h.post("dallas", is_useful=False, reason_code="outdated").json()
        body = h.get("dallas").json()
        assert body["total"] == 2
        assert [i["id"] for i in body["items"]] == [second["id"], first["id"]]
        assert set(body) == {"items", "total", "limit", "offset"}

    def test_pagination_limits_page(self, h, flag_on):
        for _ in range(3):
            h.post("dallas", is_useful=True)
        body = h.get("dallas", params={"limit": 2, "offset": 0}).json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["limit"] == 2

    def test_limit_over_max_rejected(self, h, flag_on):
        r = h.get("dallas", params={"limit": 101})
        assert r.status_code == 422

    def test_empty_history_is_valid(self, h, flag_on):
        body = h.get("nairobi").json()
        assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}


# --------------------------------------------------------------------------- #
# AuthN / AuthZ — editor-only on BOTH submit and read
# --------------------------------------------------------------------------- #
class TestSecurity:
    def test_unauthenticated_401(self, h, flag_on):
        mk = h.m("dallas")
        assert h.client.get(h.url(mk.ws_id, mk.opp_id)).status_code == 401
        assert h.client.post(h.url(mk.ws_id, mk.opp_id),
                             json={"intelligence_record_id": mk.record_id,
                                   "is_useful": True}).status_code == 401

    def test_non_member_forbidden(self, h, flag_on):
        assert h.post("dallas", auth=h.outsider_auth).status_code == 403
        assert h.get("dallas", auth=h.outsider_auth).status_code == 403

    def test_viewer_forbidden_on_both(self, h, flag_on):
        mk = h.m("dallas")
        assert h.post("dallas", auth=mk.viewer_auth).status_code == 403
        assert h.get("dallas", auth=mk.viewer_auth).status_code == 403

    def test_marketer_allowed(self, h, flag_on):
        mk = h.m("dallas")
        assert h.post("dallas", is_useful=True, auth=mk.marketer_auth).status_code == 201
        assert h.get("dallas", auth=mk.marketer_auth).status_code == 200

    def test_admin_allowed(self, h, flag_on):
        assert h.post("dallas", is_useful=True, auth=h.admin_auth).status_code == 201
        assert h.get("dallas", auth=h.admin_auth).status_code == 200


# --------------------------------------------------------------------------- #
# Scope / IDOR
# --------------------------------------------------------------------------- #
class TestScope:
    def test_unknown_opportunity_404(self, h, flag_on):
        mk = h.m("dallas")
        r = h.client.post(h.url(mk.ws_id, "opp-nope"),
                          json={"intelligence_record_id": mk.record_id, "is_useful": True},
                          headers=mk.owner_auth)
        assert r.status_code == 404

    def test_cross_workspace_opportunity_404(self, h, flag_on):
        # Dallas owner, own workspace path, but a London opportunity id → hidden IDOR 404.
        r = h.post("dallas", is_useful=True, opp="opp-london")
        assert r.status_code == 404

    def test_unknown_record_404(self, h, flag_on):
        r = h.post("dallas", is_useful=True, record_id="sir-nope")
        assert r.status_code == 404

    def test_cross_workspace_record_404(self, h, flag_on):
        # A record that exists but in another workspace is indistinguishable from missing.
        r = h.post("dallas", is_useful=True, record_id="sir-london")
        assert r.status_code == 404

    def test_sibling_opportunity_record_422(self, h, flag_on, factory):
        # A record in the SAME workspace but a different opportunity → service 422.
        r = h.post("dallas", is_useful=True, record_id="sir-dallas-2")
        assert r.status_code == 422
        assert _count(factory) == 0


# --------------------------------------------------------------------------- #
# Four-market isolation over HTTP
# --------------------------------------------------------------------------- #
class TestFourMarketIsolation:
    def test_each_market_captures_its_own(self, h, flag_on):
        for key in _MARKETS:
            r = h.post(key, is_useful=True)
            assert r.status_code == 201, key
            assert r.json()["opportunity_id"] == f"opp-{key}"

    def test_history_never_leaks_across_markets(self, h, flag_on):
        for key in _MARKETS:
            h.post(key, is_useful=True)
        for key in _MARKETS:
            body = h.get(key).json()
            assert body["total"] == 1
            assert body["items"][0]["opportunity_id"] == f"opp-{key}"

    def test_cross_market_record_is_rejected(self, h, flag_on):
        # Every cross-market (opp A, record from B) pair is rejected as 404: B's record
        # lives in B's workspace, never A's.
        for a in _MARKETS:
            for b in _MARKETS:
                if a == b:
                    continue
                r = h.post(a, is_useful=True, record_id=f"sir-{b}")
                assert r.status_code == 404, (a, b)


# --------------------------------------------------------------------------- #
# Audit + capture-only + non-mutating reads
# --------------------------------------------------------------------------- #
class TestSideEffects:
    def test_submit_emits_audit(self, h, flag_on, factory):
        h.post("dallas", is_useful=True)
        make, _ = factory
        with make() as s:
            row = s.scalar(
                select(AuditLog).where(AuditLog.action == "opportunity_feedback.created")
            )
            assert row is not None
            assert row.entity_type == "opportunity_feedback"
            assert row.actor_user_id == "owner-dallas"
            assert row.workspace_id == "ws-dallas"

    def test_capture_never_rescoring(self, h, flag_on, factory):
        make, _ = factory
        with make() as s:
            records_before = s.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
        h.post("dallas", is_useful=True, reason_code="commercially_relevant")
        with make() as s:
            opp = s.get(Opportunity, "opp-dallas")
            assert opp.opportunity_score == 50  # untouched
            records_after = s.scalar(select(func.count()).select_from(SignalIntelligenceRecord))
        assert records_after == records_before

    def test_reads_never_write(self, h, flag_on, factory):
        h.post("dallas", is_useful=True)
        before = _count(factory)
        for _ in range(3):
            h.get("dallas")
        assert _count(factory) == before


# --------------------------------------------------------------------------- #
# Route registration
# --------------------------------------------------------------------------- #
class TestRouteRegistration:
    def test_both_endpoints_registered(self):
        spec = app.openapi()
        path = "/api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback"
        assert path in spec["paths"]
        assert set(spec["paths"][path]) == {"post", "get"}
