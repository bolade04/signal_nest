"""Phase 4A-C.4.3: operator override-list read — route tests.

Covers the third slice of the operator capability-governance surface:

    GET /internal/system/capabilities/overrides

The override-list read is the **first sanctioned production consumer of the merged
governed override service** (:func:`app.capabilities.service.list_capability_overrides`),
and it consumes only the service's *read* plane: it lists persisted per-workspace override
*intent* but writes no row, opens no transaction, and emits no audit. These tests assert:
operator-only authorization (401 anonymous / 403 non-operator), the secret-free
``CapabilityOverrideOut`` response shape (its governance fields — ``reason``,
``set_by_user_id``, timestamps — are surfaced, but no credential/URL/token), an empty page
by default, tenant-scoped listing (a workspace never sees another workspace's rows),
four-market isolation (Dallas / London / Lagos / Nairobi), non-enumerating tenant 404s,
bounded/clamped pagination (out-of-range ``limit``/``offset`` → 422; valid paging splits
the page and echoes the clamped bounds), and that the read writes no override row and no
audit — activating nothing while every global flag stays ``False``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.capabilities.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    set_capability_override,
)
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, User, Workspace

API = get_settings().api_prefix
OVERRIDES_PATH = "/internal/system/capabilities/overrides"

# The exact operator-safe field set of one stored-override item.
_ITEM_FIELDS = {
    "id",
    "organization_id",
    "workspace_id",
    "capability",
    "enabled",
    "reason",
    "set_by_user_id",
    "created_at",
    "updated_at",
}

# Substrings that must never appear in a governance-metadata response body. Note that
# ``reason`` is a legitimate (non-secret) field name here, so it is deliberately absent.
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

# Four markets under org A — used to prove per-workspace isolation of the list read.
_MARKETS = {
    "dallas": "ws-dallas",
    "london": "ws-london",
    "lagos": "ws-lagos",
    "nairobi": "ws-nairobi",
}


@pytest.fixture()
def h(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'overrides.db'}",
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


class _Harness:
    def __init__(self, client, factory):
        self.client = client
        self.factory = factory
        self.op = {"Authorization": f"Bearer {create_access_token(_OPERATOR)}"}
        self.cust = {"Authorization": f"Bearer {create_access_token(_CUSTOMER)}"}

    def overrides(self, *, org=_ORG_A, ws=_WS_A, limit=None, offset=None, auth=None, anon=False):
        params = {"organization_id": org, "workspace_id": ws}
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        headers = None if anon else (self.op if auth is None else auth)
        return self.client.get(f"{API}{OVERRIDES_PATH}", params=params, headers=headers)

    def seed(self, *, org, ws, capability, enabled, reason=None):
        with self.factory() as s:
            set_capability_override(
                s,
                organization_id=org,
                workspace_id=ws,
                capability=capability,
                enabled=enabled,
                actor_user_id=_OPERATOR,
                reason=reason,
            )
            s.commit()

    def seed_three_in_ws_a(self):
        # All three registry capabilities are workspace_disableable; RSS is disable-only.
        self.seed(org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True)
        self.seed(org=_ORG_A, ws=_WS_A, capability=Capability.SCOUT_SCHEDULING, enabled=True)
        self.seed(org=_ORG_A, ws=_WS_A, capability=Capability.CONNECTOR_RSS, enabled=False)

    def counts(self):
        with self.factory() as s:
            overrides = s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride))
            audits = s.scalar(select(func.count()).select_from(AuditLog))
        return overrides, audits


# --------------------------------------------------------------------------- #
# Authorization — the override-list read is operator-only (case 10)
# --------------------------------------------------------------------------- #
class TestAuthorization:
    def test_anonymous_is_401(self, h):
        assert h.overrides(anon=True).status_code == 401

    def test_non_operator_is_403(self, h):
        assert h.overrides(auth=h.cust).status_code == 403

    def test_operator_is_200(self, h):
        assert h.overrides().status_code == 200


# --------------------------------------------------------------------------- #
# Response shape + secret-free (cases 8, 21)
# --------------------------------------------------------------------------- #
class TestShape:
    def test_page_envelope_shape(self, h):
        body = h.overrides().json()
        assert set(body) == {"items", "total", "limit", "offset"}
        assert isinstance(body["items"], list)
        assert body["total"] == 0
        assert body["limit"] == DEFAULT_LIMIT
        assert body["offset"] == 0

    def test_default_page_is_empty(self, h):
        assert h.overrides().json()["items"] == []

    def test_each_item_has_exactly_the_operator_safe_fields(self, h):
        h.seed_three_in_ws_a()
        items = h.overrides().json()["items"]
        assert items
        for item in items:
            assert set(item) == _ITEM_FIELDS

    def test_item_projects_the_stored_row(self, h):
        h.seed(
            org=_ORG_A,
            ws=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            reason="market rollout note",
        )
        item = h.overrides().json()["items"][0]
        assert item["organization_id"] == _ORG_A
        assert item["workspace_id"] == _WS_A
        assert item["capability"] == "opportunity_feedback"
        assert item["enabled"] is True
        assert item["reason"] == "market rollout note"
        assert item["set_by_user_id"] == _OPERATOR
        assert item["id"]
        assert item["created_at"] and item["updated_at"]

    def test_response_body_carries_no_secret(self, h):
        h.seed(
            org=_ORG_A,
            ws=_WS_A,
            capability=Capability.OPPORTUNITY_FEEDBACK,
            enabled=True,
            reason="market rollout note",
        )
        blob = h.overrides().text.lower()
        for token in _FORBIDDEN:
            assert token not in blob


# --------------------------------------------------------------------------- #
# Tenant scoping + four-market isolation (cases 9, 20)
# --------------------------------------------------------------------------- #
class TestTenantScoping:
    def test_list_is_scoped_to_the_requested_workspace(self, h):
        h.seed_three_in_ws_a()
        body = h.overrides(org=_ORG_A, ws=_WS_A).json()
        assert body["total"] == 3
        assert {i["capability"] for i in body["items"]} == {
            "opportunity_feedback",
            "scout_scheduling",
            "connector_rss",
        }
        assert all(i["workspace_id"] == _WS_A for i in body["items"])

    def test_another_workspaces_rows_never_appear(self, h):
        # Seed only in ws-a; a sibling workspace under the same org lists nothing.
        h.seed_three_in_ws_a()
        body = h.overrides(org=_ORG_A, ws=_MARKETS["dallas"]).json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_four_market_isolation(self, h):
        # A distinct override in each market must appear in that market's list only.
        for slug, ws_id in _MARKETS.items():
            h.seed(
                org=_ORG_A,
                ws=ws_id,
                capability=Capability.OPPORTUNITY_FEEDBACK,
                enabled=True,
                reason=f"{slug} note",
            )
        for ws_id in _MARKETS.values():
            body = h.overrides(org=_ORG_A, ws=ws_id).json()
            assert body["total"] == 1
            assert body["items"][0]["workspace_id"] == ws_id


# --------------------------------------------------------------------------- #
# Non-enumerating tenant 404 (case 9)
# --------------------------------------------------------------------------- #
class TestTenancy404:
    def test_tenant_mismatch_is_404(self, h):
        # ws-b belongs to org-b; querying it under org-a must 404 (non-enumerating).
        assert h.overrides(org=_ORG_A, ws=_WS_B).status_code == 404

    def test_absent_workspace_is_404(self, h):
        assert h.overrides(org=_ORG_A, ws="ws-does-not-exist").status_code == 404

    def test_tenant_404_is_non_enumerating(self, h):
        mismatch = h.overrides(org=_ORG_A, ws=_WS_B)
        absent = h.overrides(org=_ORG_A, ws="ws-nope")
        assert mismatch.status_code == absent.status_code == 404
        # Same generic envelope: a caller cannot tell "exists but not yours" from "gone".
        assert mismatch.json()["error"]["code"] == absent.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- #
# Bounded / clamped pagination (case 8)
# --------------------------------------------------------------------------- #
class TestPagination:
    def test_limit_below_range_is_422(self, h):
        assert h.overrides(limit=0).status_code == 422

    def test_limit_above_range_is_422(self, h):
        assert h.overrides(limit=MAX_LIMIT + 1).status_code == 422

    def test_negative_offset_is_422(self, h):
        assert h.overrides(offset=-1).status_code == 422

    def test_paging_splits_the_page_and_echoes_bounds(self, h):
        h.seed_three_in_ws_a()
        first = h.overrides(limit=2, offset=0).json()
        assert first["total"] == 3
        assert first["limit"] == 2
        assert first["offset"] == 0
        assert len(first["items"]) == 2

        second = h.overrides(limit=2, offset=2).json()
        assert second["total"] == 3
        assert second["offset"] == 2
        assert len(second["items"]) == 1

        # The two pages are disjoint and together cover all three seeded overrides.
        seen = {i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}
        assert len(seen) == 3


# --------------------------------------------------------------------------- #
# Read-only + dark-state — the list read mutates nothing; flags stay False
# --------------------------------------------------------------------------- #
class TestReadOnlyAndDark:
    def test_list_read_writes_no_override_row_and_no_audit(self, h):
        h.seed_three_in_ws_a()
        before = h.counts()
        assert h.overrides().status_code == 200
        assert h.overrides(limit=1, offset=0).status_code == 200
        assert h.counts() == before  # nothing persisted, nothing audited by the read

    def test_all_three_global_flags_remain_false(self):
        settings = get_settings()
        assert settings.connector_rss_enabled is False
        assert settings.scout_scheduling_enabled is False
        assert settings.opportunity_feedback_enabled is False


# --------------------------------------------------------------------------- #
# Contract — the additive route is in the OpenAPI schema without disturbing peers
# --------------------------------------------------------------------------- #
class TestOpenAPI:
    def test_overrides_endpoint_and_operation_id_present(self):
        schema = app.openapi()
        full_path = f"{API}{OVERRIDES_PATH}"
        assert full_path in schema["paths"]
        get_op = schema["paths"][full_path]["get"]
        assert get_op.get("operationId")
        assert "overrides" in get_op["operationId"]

    def test_registry_and_effective_routes_still_present(self):
        schema = app.openapi()
        assert f"{API}/internal/system/capabilities/registry" in schema["paths"]
        assert f"{API}/internal/system/capabilities/effective" in schema["paths"]
