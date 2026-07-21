"""Phase 4A-C.4.5: operator override-clear write — route tests.

Covers the fifth slice — and the **second write path** — of the operator
capability-governance surface:

    DELETE /internal/system/capabilities/overrides

The clear write path is the operator router's use of the merged governed override
service's *clear* plane (:func:`app.capabilities.service.clear_capability_override`): it
removes any recorded per-workspace override *intent* under the service's ordered,
fail-closed gates (authoritative tenant validation → ``SELECT … FOR UPDATE`` lock →
delete-or-no-op with a ``.cleared`` audit), all inside the request-scoped transaction.
Clearing is deny-biased and always permitted (removing an override can only relax toward
the secure default) and takes no reason. These tests assert: operator-only authorization
(401 anonymous / 403 non-operator, and that a rejected clear deletes nothing), the
secret-free ``CapabilityOverrideMutationOut`` response shape (``enabled``/``override_id``
come back ``None``), the delete-then-dark effective round-trip (case 17), idempotent
absent-clear semantics (``changed=False`` with no audit — case 18), non-enumerating tenant
404s and unknown-capability rejection by the enum (case 19), server-side attribution of
the ``.cleared`` audit, four-market isolation (case 20), a secret-free body (case 21),
GET/PUT/DELETE method separation on the shared ``/overrides`` path (case 22), an
end-to-end mutation smoke over real PostgreSQL (case 23, ``[PG]``), and that clearing
activates nothing — every global flag stays ``False``.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, User, Workspace

API = get_settings().api_prefix
OVERRIDES_PATH = "/internal/system/capabilities/overrides"
EFFECTIVE_PATH = "/internal/system/capabilities/effective"

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

# Four markets under org A — used to prove per-workspace isolation of the clear path.
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
    PUTs and DELETEs; without a reset it could exhaust that global budget and make *later*
    files' HTTP tests spuriously 429. Reset at both setup and teardown so this file
    inherits — and leaves behind — a clean window, keeping the suite hermetic. Test-only;
    touches no production state.
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


def _seed_tenant(factory) -> None:
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


def _override_get_db_factory(factory):
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

    return _override_get_db


@pytest.fixture()
def h(tmp_path):
    _reset_rate_limiter()
    engine = create_engine(
        f"sqlite:///{tmp_path / 'override_clear.db'}",
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
    _seed_tenant(factory)

    app.dependency_overrides[get_db] = _override_get_db_factory(factory)
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
            reason=None):
        body = {
            "organization_id": org,
            "workspace_id": ws,
            "capability": capability,
            "enabled": enabled,
        }
        if reason is not None:
            body["reason"] = reason
        return self.client.put(f"{API}{OVERRIDES_PATH}", json=body, headers=self.op)

    def clear(self, *, org=_ORG_A, ws=_WS_A, capability="opportunity_feedback",
              auth=None, anon=False):
        params = {"organization_id": org, "workspace_id": ws, "capability": capability}
        headers = None if anon else (self.op if auth is None else auth)
        return self.client.request(
            "DELETE", f"{API}{OVERRIDES_PATH}", params=params, headers=headers
        )

    def effective(self, *, org=_ORG_A, ws=_WS_A, capability="opportunity_feedback"):
        return self.client.get(
            f"{API}{EFFECTIVE_PATH}",
            params={"organization_id": org, "workspace_id": ws, "capability": capability},
            headers=self.op,
        )

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
# Authorization — the override-clear write is operator-only (case 19)
# --------------------------------------------------------------------------- #
class TestAuthorization:
    def test_anonymous_is_401(self, h):
        assert h.clear(anon=True).status_code == 401

    def test_non_operator_is_403(self, h):
        assert h.clear(auth=h.cust).status_code == 403

    def test_operator_is_200(self, h):
        assert h.clear().status_code == 200

    def test_rejected_clear_deletes_nothing(self, h):
        # Seed one override, then attempt unauthorized clears: the row must survive.
        h.set(capability="opportunity_feedback", enabled=True)
        h.clear(anon=True)
        h.clear(auth=h.cust)
        assert h.counts() == (1, 1)  # still one row, only the seeding .created audit


# --------------------------------------------------------------------------- #
# Response shape + secret-free (cases 21)
# --------------------------------------------------------------------------- #
class TestShape:
    def test_mutation_envelope_shape(self, h):
        body = h.clear().json()
        assert set(body) == _MUTATION_FIELDS

    def test_clear_returns_none_enabled_and_none_override_id(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        body = h.clear(capability="opportunity_feedback").json()
        assert body["capability"] == "opportunity_feedback"
        assert body["workspace_id"] == _WS_A
        assert body["created"] is False
        assert body["changed"] is True
        # No override remains, so the summary carries no surviving state.
        assert body["enabled"] is None
        assert body["override_id"] is None

    def test_response_body_carries_no_secret(self, h):
        h.set(capability="opportunity_feedback", enabled=True, reason="market rollout note")
        blob = h.clear(capability="opportunity_feedback").text.lower()
        for token in _FORBIDDEN:
            assert token not in blob


# --------------------------------------------------------------------------- #
# Delete-then-dark round-trip + one .cleared audit (case 17)
# --------------------------------------------------------------------------- #
class TestClearRemovesAndReturnsDark:
    def test_clear_removes_the_row_and_writes_one_cleared_audit(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        assert h.counts() == (1, 1)
        resp = h.clear(capability="opportunity_feedback")
        assert resp.status_code == 200
        assert resp.json()["changed"] is True
        # Row gone; the seeding .created plus the .cleared audit both persist.
        assert h.counts() == (0, 2)
        assert h.audit_actions() == [
            "workspace_capability_override.cleared",
            "workspace_capability_override.created",
        ]

    def test_effective_returns_to_dark_default_after_clear(self, h):
        # An enable makes the effective read honor the override...
        h.set(capability="opportunity_feedback", enabled=True)
        enabled = h.effective(capability="opportunity_feedback").json()["items"][0]
        assert enabled["has_override"] is True
        assert enabled["decided_by"] == "workspace_override"
        assert enabled["effective_enabled"] is True
        # ...clearing returns the pair to the dark, globally-decided default.
        h.clear(capability="opportunity_feedback")
        after = h.effective(capability="opportunity_feedback").json()["items"][0]
        assert after["has_override"] is False
        assert after["decided_by"] == "global_configuration"
        assert after["effective_enabled"] is False
        assert after["global_flag"] is False


# --------------------------------------------------------------------------- #
# Idempotent absent-clear — no row, no audit (case 18)
# --------------------------------------------------------------------------- #
class TestIdempotentAbsentClear:
    def test_clear_of_absent_override_is_success_with_no_audit(self, h):
        resp = h.clear(capability="opportunity_feedback")
        assert resp.status_code == 200
        assert resp.json()["created"] is False
        assert resp.json()["changed"] is False
        # Clearing an override that never existed is a benign no-op: nothing written.
        assert h.counts() == (0, 0)
        assert h.audit_actions() == []

    def test_second_clear_after_a_real_clear_is_a_no_op(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        h.clear(capability="opportunity_feedback")
        first = h.counts()
        again = h.clear(capability="opportunity_feedback")
        assert again.status_code == 200
        assert again.json()["changed"] is False
        # The already-empty second clear adds neither a row nor an audit.
        assert h.counts() == first


# --------------------------------------------------------------------------- #
# Unknown capability rejected by the enum before any service call (case 19)
# --------------------------------------------------------------------------- #
class TestUnknownCapability:
    def test_unknown_capability_is_422(self, h):
        assert h.clear(capability="does_not_exist").status_code == 422

    def test_unknown_capability_writes_nothing(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        h.clear(capability="does_not_exist")
        # The typed enum rejects the query param before the service runs: the real
        # override is untouched, and no audit is written.
        assert h.counts() == (1, 1)


# --------------------------------------------------------------------------- #
# Non-enumerating tenant 404 (case 19)
# --------------------------------------------------------------------------- #
class TestTenancy404:
    def test_tenant_mismatch_is_404(self, h):
        # ws-b belongs to org-b; clearing it under org-a must 404 (non-enumerating).
        assert h.clear(org=_ORG_A, ws=_WS_B).status_code == 404

    def test_absent_workspace_is_404(self, h):
        assert h.clear(org=_ORG_A, ws="ws-does-not-exist").status_code == 404

    def test_tenant_404_is_non_enumerating(self, h):
        mismatch = h.clear(org=_ORG_A, ws=_WS_B)
        absent = h.clear(org=_ORG_A, ws="ws-nope")
        assert mismatch.status_code == absent.status_code == 404
        assert mismatch.json()["error"]["code"] == absent.json()["error"]["code"] == "not_found"

    def test_tenant_reject_deletes_nothing(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        h.clear(org=_ORG_A, ws=_WS_B)
        h.clear(org=_ORG_A, ws="ws-nope")
        # Cross-tenant / absent clears touch no state: the real override survives.
        assert h.counts() == (1, 1)


# --------------------------------------------------------------------------- #
# Server-side attribution — the .cleared audit actor is the authenticated operator
# --------------------------------------------------------------------------- #
class TestAttribution:
    def test_cleared_audit_actor_is_the_operator(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        h.clear(capability="opportunity_feedback")
        with h.factory() as s:
            actor = s.scalar(
                select(AuditLog.actor_user_id).where(
                    AuditLog.action == "workspace_capability_override.cleared"
                )
            )
        assert actor == _OPERATOR


# --------------------------------------------------------------------------- #
# Four-market isolation (case 20)
# --------------------------------------------------------------------------- #
class TestFourMarketIsolation:
    def test_clear_in_one_market_leaves_the_others_intact(self, h):
        # Seed the same override in all four markets...
        for ws_id in _MARKETS.values():
            assert h.set(org=_ORG_A, ws=ws_id, capability="opportunity_feedback",
                         enabled=True).status_code == 200
        overrides, _ = h.counts()
        assert overrides == len(_MARKETS)
        # ...clear exactly one; the other three must remain untouched.
        cleared_ws = _MARKETS["london"]
        cleared = h.clear(org=_ORG_A, ws=cleared_ws, capability="opportunity_feedback")
        assert cleared.status_code == 200
        assert h.rows(ws=cleared_ws) == []
        for ws_id in _MARKETS.values():
            if ws_id == cleared_ws:
                continue
            rows = h.rows(ws=ws_id)
            assert len(rows) == 1
            assert rows[0].workspace_id == ws_id


# --------------------------------------------------------------------------- #
# GET/PUT/DELETE method separation on the shared /overrides path (case 22)
# --------------------------------------------------------------------------- #
class TestMethodSeparation:
    def test_get_put_delete_coexist_on_the_same_path(self, h):
        # PUT records intent, GET lists it, DELETE removes it — all on one path.
        h.set(capability="opportunity_feedback", enabled=True, reason="note")
        listed = h.client.get(
            f"{API}{OVERRIDES_PATH}",
            params={"organization_id": _ORG_A, "workspace_id": _WS_A},
            headers=h.op,
        )
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1
        assert h.clear(capability="opportunity_feedback").status_code == 200
        after = h.client.get(
            f"{API}{OVERRIDES_PATH}",
            params={"organization_id": _ORG_A, "workspace_id": _WS_A},
            headers=h.op,
        )
        assert after.json()["items"] == []


# --------------------------------------------------------------------------- #
# Dark-state — clearing activates nothing; flags stay False
# --------------------------------------------------------------------------- #
class TestDarkState:
    def test_all_three_global_flags_remain_false_after_a_clear(self, h):
        h.set(capability="opportunity_feedback", enabled=True)
        h.clear(capability="opportunity_feedback")
        settings = get_settings()
        assert settings.connector_rss_enabled is False
        assert settings.scout_scheduling_enabled is False
        assert settings.opportunity_feedback_enabled is False


# --------------------------------------------------------------------------- #
# Contract — the additive DELETE is in the OpenAPI schema without disturbing peers
# --------------------------------------------------------------------------- #
class TestOpenAPI:
    def test_delete_operation_present_on_overrides_path(self):
        schema = app.openapi()
        full_path = f"{API}{OVERRIDES_PATH}"
        assert full_path in schema["paths"]
        delete_op = schema["paths"][full_path]["delete"]
        assert delete_op.get("operationId")

    def test_sibling_methods_and_routes_still_present(self):
        schema = app.openapi()
        full_path = f"{API}{OVERRIDES_PATH}"
        # GET/PUT/DELETE all coexist on the one path; the read routes are intact.
        assert "get" in schema["paths"][full_path]
        assert "put" in schema["paths"][full_path]
        assert f"{API}/internal/system/capabilities/registry" in schema["paths"]
        assert f"{API}{EFFECTIVE_PATH}" in schema["paths"]


# --------------------------------------------------------------------------- #
# [PG] End-to-end mutation smoke on real PostgreSQL (case 23)
# --------------------------------------------------------------------------- #
# The clear plane's row lock only truly blocks on PostgreSQL; this end-to-end smoke
# proves the full route path — PUT → GET effective → DELETE — over the real dialect's
# ``FOR UPDATE``/SAVEPOINT service, preserving the single-row + audit invariant. Gated on
# ``TEST_POSTGRES_URL`` (skipped otherwise), mirroring the merged 4A-C.3.5 concurrency
# gate; the SQLite tests above stand in locally.
_pg_only = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL override-clear smoke",
)


@_pg_only
def test_pg_put_effective_delete_round_trip() -> None:  # pragma: no cover - gated on live PG
    """PUT → GET effective → DELETE preserves the single-row + audit invariant (case 23)."""
    _reset_rate_limiter()
    engine = create_engine(os.environ["TEST_POSTGRES_URL"], future=True)
    assert engine.dialect.name == "postgresql"
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    try:
        _seed_tenant(factory)
        app.dependency_overrides[get_db] = _override_get_db_factory(factory)
        h = _Harness(TestClient(app), factory)

        # PUT records an enable...
        put = h.set(capability="opportunity_feedback", enabled=True)
        assert put.status_code == 200
        assert put.json()["created"] is True
        assert h.counts() == (1, 1)

        # ...GET effective honors it through the resolver on the real dialect...
        eff = h.effective(capability="opportunity_feedback").json()["items"][0]
        assert eff["has_override"] is True
        assert eff["effective_enabled"] is True
        assert eff["global_flag"] is False

        # ...DELETE removes exactly the one row and writes exactly one .cleared audit.
        cleared = h.clear(capability="opportunity_feedback")
        assert cleared.status_code == 200
        assert cleared.json()["changed"] is True
        assert h.counts() == (0, 2)
        assert h.audit_actions() == [
            "workspace_capability_override.cleared",
            "workspace_capability_override.created",
        ]

        # Effective is dark again; the flag never flipped.
        after = h.effective(capability="opportunity_feedback").json()["items"][0]
        assert after["has_override"] is False
        assert after["effective_enabled"] is False
        assert get_settings().opportunity_feedback_enabled is False
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        _reset_rate_limiter()
