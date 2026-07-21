"""Phase 4A-C.4.4: operator override-set write — route tests.

Covers the fourth slice — and the **first write path** — of the operator
capability-governance surface:

    PUT /internal/system/capabilities/overrides

The set write path is the operator router's use of the merged governed override
service's *set* plane (:func:`app.capabilities.service.set_capability_override`): it
records per-workspace override *intent* under the service's ordered, fail-closed gates
(authoritative tenant validation → deny-biased registry policy → bounded reason
validation → ``SELECT … FOR UPDATE``/SAVEPOINT upsert → ``.created``/``.updated`` audit),
all inside the request-scoped transaction. These tests assert: operator-only
authorization (401 anonymous / 403 non-operator), the secret-free
``CapabilityOverrideMutationOut`` response shape, the idempotent-upsert semantics
(``created``/``changed`` and their audit trail — a re-PUT of the same state writes no new
audit; a changed re-PUT emits ``.updated``), deny-biased policy rejection (RSS
``enabled=True`` → 422 ``capability_override_not_permitted``, with the request transaction
fully rolled back at the HTTP boundary so no row and no audit persist through the route),
unknown-capability rejection by the enum (422, no service call/row), over-length
reason rejection (422, no row), non-enumerating tenant 404s, server-side attribution (the
authenticated operator, never a body-supplied actor), four-market isolation, a secret-free
body, GET/PUT method separation on the shared ``/overrides`` path, and that recording
intent activates nothing — every global flag stays ``False``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.service import MAX_REASON_LEN
from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, User, Workspace

API = get_settings().api_prefix
OVERRIDES_PATH = "/internal/system/capabilities/overrides"

# The exact operator-safe field set of one mutation-summary response body.
_MUTATION_FIELDS = {
    "capability",
    "workspace_id",
    "created",
    "changed",
    "enabled",
    "override_id",
}

# Substrings that must never appear in a governance-metadata response body.
_FORBIDDEN = (
    "password",
    "hashed_password",
    "api_key",
    "secret",
    "token",
    "redis://",
    "postgresql://",
    "authorization",
)

_OPERATOR = "operator-user"
_CUSTOMER = "customer-user"
_ORG_A, _WS_A = "org-a", "ws-a"
_ORG_B, _WS_B = "org-b", "ws-b"

# Four markets under org A — used to prove per-workspace isolation of the write path.
_MARKETS = {
    "dallas": "ws-dallas",
    "london": "ws-london",
    "lagos": "ws-lagos",
    "nairobi": "ws-nairobi",
}


def _reset_rate_limiter() -> None:
    """Clear the process-shared fixed-window rate limiter's hit ledger.

    The app mounts a single :class:`RateLimitMiddleware` instance whose in-memory
    ``_hits`` budget (240 requests / 60s, keyed by client host) is shared across every
    test that drives ``app.main.app`` in this process. This write-path suite issues many
    PUTs; without a reset it could exhaust that global budget and make *later* files' HTTP
    tests spuriously 429. Reset at both setup and teardown so this file inherits — and
    leaves behind — a clean window, keeping the suite hermetic. Test-only; touches no
    production state.
    """
    # Operate on the *live* cached stack the TestClient actually dispatches through
    # (a fresh ``build_middleware_stack()`` would yield throwaway instances). Building and
    # caching it when absent makes that cached instance the one future requests reuse.
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            node._hits.clear()
            return
        node = getattr(node, "app", None)


@pytest.fixture()
def h(tmp_path):
    _reset_rate_limiter()
    engine = create_engine(
        f"sqlite:///{tmp_path / 'override_set.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    # Enforce FKs for THIS engine only so tenant-scope rows are real.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with factory() as s:
        s.add(
            User(
                id=_OPERATOR,
                email="operator@example.com",
                full_name="Operator",
                hashed_password="x",
                is_active=True,
                is_operator=True,
            )
        )
        s.add(
            User(
                id=_CUSTOMER,
                email="customer@example.com",
                full_name="Customer",
                hashed_password="x",
                is_active=True,
                is_operator=False,
            )
        )
        s.add(Organization(id=_ORG_A, name="A", slug="a"))
        s.add(Organization(id=_ORG_B, name="B", slug="b"))
        s.add(Workspace(id=_WS_A, organization_id=_ORG_A, name="WA", slug="wa"))
        s.add(Workspace(id=_WS_B, organization_id=_ORG_B, name="WB", slug="wb"))
        for slug, ws_id in _MARKETS.items():
            s.add(Workspace(id=ws_id, organization_id=_ORG_A, name=slug, slug=slug))
        s.commit()

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
        engine.dispose()
        _reset_rate_limiter()


class _Harness:
    def __init__(self, client, factory):
        self.client = client
        self.factory = factory
        self.op = {"Authorization": f"Bearer {create_access_token(_OPERATOR)}"}
        self.cust = {"Authorization": f"Bearer {create_access_token(_CUSTOMER)}"}

    def set(self, *, org=_ORG_A, ws=_WS_A, capability="opportunity_feedback", enabled=True,
            reason=None, auth=None, anon=False):
        body = {
            "organization_id": org,
            "workspace_id": ws,
            "capability": capability,
            "enabled": enabled,
        }
        if reason is not None:
            body["reason"] = reason
        headers = None if anon else (self.op if auth is None else auth)
        return self.client.put(f"{API}{OVERRIDES_PATH}", json=body, headers=headers)

    def counts(self):
        with self.factory() as s:
            overrides = s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride))
            audits = s.scalar(select(func.count()).select_from(AuditLog))
        return overrides, audits

    def audit_actions(self):
        with self.factory() as s:
            return sorted(s.scalars(select(AuditLog.action)).all())

    def rows(self, *, ws=_WS_A):
        with self.factory() as s:
            stmt = select(WorkspaceCapabilityOverride).where(
                WorkspaceCapabilityOverride.workspace_id == ws
            )
            return list(s.scalars(stmt).all())


# --------------------------------------------------------------------------- #
# Authorization — the override-set write is operator-only (case 16)
# --------------------------------------------------------------------------- #
class TestAuthorization:
    def test_anonymous_is_401(self, h):
        assert h.set(anon=True).status_code == 401

    def test_non_operator_is_403(self, h):
        assert h.set(auth=h.cust).status_code == 403

    def test_operator_is_200(self, h):
        assert h.set().status_code == 200

    def test_rejected_write_persists_nothing(self, h):
        # A 401/403 must fail closed: no row, no audit written by an unauthorized attempt.
        h.set(anon=True)
        h.set(auth=h.cust)
        assert h.counts() == (0, 0)


# --------------------------------------------------------------------------- #
# Response shape + secret-free (cases 11, 21)
# --------------------------------------------------------------------------- #
class TestShape:
    def test_mutation_envelope_shape(self, h):
        body = h.set().json()
        assert set(body) == _MUTATION_FIELDS

    def test_create_reports_created_and_changed(self, h):
        body = h.set(capability="opportunity_feedback", enabled=True, reason="rollout").json()
        assert body["capability"] == "opportunity_feedback"
        assert body["workspace_id"] == _WS_A
        assert body["created"] is True
        assert body["changed"] is True
        assert body["enabled"] is True
        assert body["override_id"]

    def test_response_body_carries_no_secret(self, h):
        blob = h.set(reason="market rollout note").text.lower()
        for token in _FORBIDDEN:
            assert token not in blob


# --------------------------------------------------------------------------- #
# Idempotent upsert semantics + audit trail (case 11)
# --------------------------------------------------------------------------- #
class TestIdempotentUpsert:
    def test_first_put_creates_one_row_and_one_created_audit(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        assert h.counts() == (1, 1)
        assert h.audit_actions() == ["workspace_capability_override.created"]

    def test_identical_reput_is_a_no_op_with_no_new_audit(self, h):
        h.set(capability="opportunity_feedback", enabled=True, reason="note")
        first = h.counts()
        again = h.set(capability="opportunity_feedback", enabled=True, reason="note")
        assert again.status_code == 200
        assert again.json()["created"] is False
        assert again.json()["changed"] is False
        # No new row, no new audit — the identical re-PUT is a pure no-op.
        assert h.counts() == first
        assert h.audit_actions() == ["workspace_capability_override.created"]

    def test_changed_reput_updates_in_place_and_emits_updated_audit(self, h):
        create = h.set(capability="opportunity_feedback", enabled=True)
        # Flip enabled -> disable (opportunity_feedback is workspace_disableable).
        update = h.set(capability="opportunity_feedback", enabled=False)
        assert update.status_code == 200
        assert update.json()["created"] is False
        assert update.json()["changed"] is True
        assert update.json()["enabled"] is False
        # Same surviving row (updated in place, never duplicated); two audits now.
        assert update.json()["override_id"] == create.json()["override_id"]
        assert h.counts() == (1, 2)
        assert h.audit_actions() == [
            "workspace_capability_override.created",
            "workspace_capability_override.updated",
        ]


# --------------------------------------------------------------------------- #
# Deny-biased policy rejection — RSS is disable-only (case 12)
# --------------------------------------------------------------------------- #
class TestPolicyRejection:
    def test_rss_enable_is_422_capability_override_not_permitted(self, h):
        resp = h.set(capability="connector_rss", enabled=True)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "capability_override_not_permitted"

    def test_rss_enable_write_is_fully_rolled_back(self, h):
        # Deny-biased: the service emits a ``.rejected`` audit (a service-level concern
        # covered by the service tests), but at the HTTP boundary the raised 422 rolls
        # back the whole request transaction — so through the route nothing persists at
        # all: no override row and no audit. The write fails fully closed.
        h.set(capability="connector_rss", enabled=True)
        assert h.counts() == (0, 0)

    def test_rss_disable_is_allowed(self, h):
        # RSS is disable-only: an explicit disable override is a legitimate write.
        resp = h.set(capability="connector_rss", enabled=False)
        assert resp.status_code == 200
        assert resp.json()["created"] is True
        assert resp.json()["enabled"] is False


# --------------------------------------------------------------------------- #
# Unknown capability rejected by the enum before any service call (case 13)
# --------------------------------------------------------------------------- #
class TestUnknownCapability:
    def test_unknown_capability_is_422(self, h):
        assert h.set(capability="does_not_exist").status_code == 422

    def test_unknown_capability_writes_no_row_and_no_audit(self, h):
        h.set(capability="does_not_exist")
        # The typed enum rejects the body before the service runs: nothing persisted.
        assert h.counts() == (0, 0)


# --------------------------------------------------------------------------- #
# Bounded reason validation (case 14)
# --------------------------------------------------------------------------- #
class TestReasonBounds:
    def test_over_length_reason_is_422(self, h):
        resp = h.set(reason="x" * (MAX_REASON_LEN + 1))
        assert resp.status_code == 422

    def test_over_length_reason_writes_no_row(self, h):
        h.set(reason="x" * (MAX_REASON_LEN + 1))
        assert h.counts() == (0, 0)

    def test_max_length_reason_is_accepted(self, h):
        resp = h.set(reason="x" * MAX_REASON_LEN)
        assert resp.status_code == 200
        assert resp.json()["created"] is True


# --------------------------------------------------------------------------- #
# Non-enumerating tenant 404 (case 15)
# --------------------------------------------------------------------------- #
class TestTenancy404:
    def test_tenant_mismatch_is_404(self, h):
        # ws-b belongs to org-b; setting it under org-a must 404 (non-enumerating).
        assert h.set(org=_ORG_A, ws=_WS_B).status_code == 404

    def test_absent_workspace_is_404(self, h):
        assert h.set(org=_ORG_A, ws="ws-does-not-exist").status_code == 404

    def test_tenant_404_is_non_enumerating(self, h):
        mismatch = h.set(org=_ORG_A, ws=_WS_B)
        absent = h.set(org=_ORG_A, ws="ws-nope")
        assert mismatch.status_code == absent.status_code == 404
        assert mismatch.json()["error"]["code"] == absent.json()["error"]["code"] == "not_found"

    def test_tenant_reject_writes_no_row_and_no_audit(self, h):
        h.set(org=_ORG_A, ws=_WS_B)
        h.set(org=_ORG_A, ws="ws-nope")
        assert h.counts() == (0, 0)


# --------------------------------------------------------------------------- #
# Server-side attribution — the actor is the authenticated operator, never the body
# --------------------------------------------------------------------------- #
class TestAttribution:
    def test_attribution_is_taken_from_the_authenticated_operator(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        rows = h.rows(ws=_WS_A)
        assert len(rows) == 1
        assert rows[0].set_by_user_id == _OPERATOR

    def test_body_supplied_actor_is_ignored(self, h):
        # A spoofed actor id in the body must not override server-side attribution.
        body = {
            "organization_id": _ORG_A,
            "workspace_id": _WS_A,
            "capability": "opportunity_feedback",
            "enabled": True,
            "actor_user_id": _CUSTOMER,
            "set_by_user_id": _CUSTOMER,
        }
        assert h.client.put(f"{API}{OVERRIDES_PATH}", json=body, headers=h.op).status_code == 200
        rows = h.rows(ws=_WS_A)
        assert rows[0].set_by_user_id == _OPERATOR


# --------------------------------------------------------------------------- #
# Four-market isolation (case 20)
# --------------------------------------------------------------------------- #
class TestFourMarketIsolation:
    def test_each_market_write_is_scoped_to_its_own_workspace(self, h):
        for ws_id in _MARKETS.values():
            resp = h.set(org=_ORG_A, ws=ws_id, capability="opportunity_feedback", enabled=True)
            assert resp.status_code == 200
            assert resp.json()["workspace_id"] == ws_id
        # Exactly one row per market — no cross-market bleed.
        for ws_id in _MARKETS.values():
            rows = h.rows(ws=ws_id)
            assert len(rows) == 1
            assert rows[0].workspace_id == ws_id
        overrides, _ = h.counts()
        assert overrides == len(_MARKETS)


# --------------------------------------------------------------------------- #
# GET/PUT method separation on the shared /overrides path (case 22)
# --------------------------------------------------------------------------- #
class TestMethodSeparation:
    def test_get_and_put_coexist_on_the_same_path(self, h):
        # The write path (PUT) records intent; the read path (GET) lists it back.
        h.set(capability="opportunity_feedback", enabled=True, reason="note")
        listed = h.client.get(
            f"{API}{OVERRIDES_PATH}",
            params={"organization_id": _ORG_A, "workspace_id": _WS_A},
            headers=h.op,
        )
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["capability"] == "opportunity_feedback"
        assert items[0]["set_by_user_id"] == _OPERATOR


# --------------------------------------------------------------------------- #
# Dark-state — recording intent activates nothing; flags stay False
# --------------------------------------------------------------------------- #
class TestDarkState:
    def test_all_three_global_flags_remain_false_after_a_write(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        h.set(capability="scout_scheduling", enabled=True)
        settings = get_settings()
        assert settings.connector_rss_enabled is False
        assert settings.scout_scheduling_enabled is False
        assert settings.opportunity_feedback_enabled is False


# --------------------------------------------------------------------------- #
# Contract — the additive PUT is in the OpenAPI schema without disturbing peers
# --------------------------------------------------------------------------- #
class TestOpenAPI:
    def test_put_operation_present_on_overrides_path(self):
        schema = app.openapi()
        full_path = f"{API}{OVERRIDES_PATH}"
        assert full_path in schema["paths"]
        put_op = schema["paths"][full_path]["put"]
        assert put_op.get("operationId")

    def test_get_overrides_and_sibling_routes_still_present(self):
        schema = app.openapi()
        full_path = f"{API}{OVERRIDES_PATH}"
        assert "get" in schema["paths"][full_path]  # GET/PUT coexist on the one path
        assert f"{API}/internal/system/capabilities/registry" in schema["paths"]
        assert f"{API}/internal/system/capabilities/effective" in schema["paths"]
