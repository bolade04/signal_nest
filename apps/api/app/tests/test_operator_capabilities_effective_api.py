"""Phase 4A-C.4.2: operator effective-state read — route tests.

Covers the second slice of the operator capability-governance surface:

    GET /internal/system/capabilities/effective

The effective read is the **first sanctioned production consumer** of the merged
deny-biased resolver (:func:`app.capabilities.resolver.resolve_capability`); it is not
a live gate (it gates no customer request and flips no flag). These tests assert:
operator-only authorization (401 anonymous / 403 non-operator), the secret-free
``CapabilityEffectiveOut`` response shape, dark-by-default resolution (all three flags
``False`` → every capability disabled via ``global_configuration``), the
persistence-vs-activation split (a service-persisted enable is reflected as
``workspace_override`` / ``effective_enabled=True`` while ``global_flag`` stays
``False``), the deny-biased RSS invariant (an enable is rejected and RSS stays
disabled), the optional per-capability filter, four-market/tenant isolation,
non-enumerating tenant 404s, an unknown-capability 422, and that the read writes no
override row and no audit — activating nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.capabilities.errors import CapabilityOverrideNotPermittedError
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability, iter_capabilities
from app.capabilities.service import clear_capability_override, set_capability_override
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, User, Workspace

API = get_settings().api_prefix
EFFECTIVE_PATH = "/internal/system/capabilities/effective"

# The exact operator-safe field set of one effective-state item.
_ITEM_FIELDS = {
    "capability",
    "workspace_id",
    "effective_enabled",
    "decided_by",
    "global_flag",
    "has_override",
    "override_value",
}

# Substrings that must never appear in a governance-metadata response body.
_FORBIDDEN = (
    "password",
    "hashed_password",
    "api_key",
    "secret",
    "token",
    "reason",
    "redis://",
    "postgresql://",
    "authorization",
)

_OPERATOR = "operator-user"
_CUSTOMER = "customer-user"
_ORG_A, _WS_A = "org-a", "ws-a"
_ORG_B, _WS_B = "org-b", "ws-b"


@pytest.fixture()
def h(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'effective.db'}",
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

    def effective(self, *, org=_ORG_A, ws=_WS_A, capability=None, auth=None, anon=False):
        params = {"organization_id": org, "workspace_id": ws}
        if capability is not None:
            params["capability"] = capability
        headers = None if anon else (self.op if auth is None else auth)
        return self.client.get(f"{API}{EFFECTIVE_PATH}", params=params, headers=headers)

    def seed_enable(self, *, org, ws, capability):
        with self.factory() as s:
            set_capability_override(
                s,
                organization_id=org,
                workspace_id=ws,
                capability=capability,
                enabled=True,
                actor_user_id=_OPERATOR,
            )
            s.commit()

    def clear(self, *, org, ws, capability):
        with self.factory() as s:
            clear_capability_override(
                s,
                organization_id=org,
                workspace_id=ws,
                capability=capability,
                actor_user_id=_OPERATOR,
            )
            s.commit()

    def counts(self):
        with self.factory() as s:
            overrides = s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride))
            audits = s.scalar(select(func.count()).select_from(AuditLog))
        return overrides, audits


# --------------------------------------------------------------------------- #
# Authorization — the effective read is operator-only (case 7)
# --------------------------------------------------------------------------- #
class TestAuthorization:
    def test_anonymous_is_401(self, h):
        assert h.effective(anon=True).status_code == 401

    def test_non_operator_is_403(self, h):
        assert h.effective(auth=h.cust).status_code == 403

    def test_operator_is_200(self, h):
        assert h.effective().status_code == 200


# --------------------------------------------------------------------------- #
# Response shape + secret-free (cases 3, 21)
# --------------------------------------------------------------------------- #
class TestShape:
    def test_envelope_shape(self, h):
        body = h.effective().json()
        assert set(body) == {"items"}
        assert isinstance(body["items"], list)

    def test_covers_every_capability_in_canonical_order(self, h):
        returned = [i["capability"] for i in h.effective().json()["items"]]
        assert returned == [c.value for c in iter_capabilities()]

    def test_each_item_has_exactly_the_operator_safe_fields(self, h):
        for item in h.effective().json()["items"]:
            assert set(item) == _ITEM_FIELDS

    def test_response_body_carries_no_secret(self, h):
        h.seed_enable(org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK)
        blob = h.effective().text.lower()
        for token in _FORBIDDEN:
            assert token not in blob


# --------------------------------------------------------------------------- #
# Dark-by-default — every capability disabled via global_configuration (case 3)
# --------------------------------------------------------------------------- #
class TestDarkByDefault:
    def test_every_capability_disabled_via_global_configuration(self, h):
        for item in h.effective().json()["items"]:
            assert item["effective_enabled"] is False, item
            assert item["decided_by"] == "global_configuration", item
            assert item["global_flag"] is False, item
            assert item["has_override"] is False, item
            assert item["override_value"] is None, item

    def test_all_three_global_flags_remain_false(self):
        settings = get_settings()
        assert settings.connector_rss_enabled is False
        assert settings.scout_scheduling_enabled is False
        assert settings.opportunity_feedback_enabled is False


# --------------------------------------------------------------------------- #
# Persistence-vs-activation split — a persisted enable is honored by the resolver
# alone while the global flag stays dark (case 4)
# --------------------------------------------------------------------------- #
class TestPersistenceVsActivation:
    def test_persisted_enable_is_reflected_without_global_activation(self, h):
        h.seed_enable(org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK)
        item = h.effective(capability="opportunity_feedback").json()["items"][0]
        assert item["has_override"] is True
        assert item["decided_by"] == "workspace_override"
        assert item["effective_enabled"] is True
        assert item["override_value"] is True
        # Persisted intent, NOT global activation: the bound flag is still dark.
        assert item["global_flag"] is False
        assert get_settings().opportunity_feedback_enabled is False

    def test_clearing_returns_to_the_dark_default(self, h):
        h.seed_enable(org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK)
        h.clear(org=_ORG_A, ws=_WS_A, capability=Capability.OPPORTUNITY_FEEDBACK)
        item = h.effective(capability="opportunity_feedback").json()["items"][0]
        assert item["effective_enabled"] is False
        assert item["decided_by"] == "global_configuration"
        assert item["has_override"] is False


# --------------------------------------------------------------------------- #
# Deny-biased RSS — an enable is rejected and RSS stays disabled (case 5)
# --------------------------------------------------------------------------- #
class TestRssStaysDark:
    def test_rss_enable_is_rejected_and_effective_stays_false(self, h):
        with pytest.raises(CapabilityOverrideNotPermittedError):
            h.seed_enable(org=_ORG_A, ws=_WS_A, capability=Capability.CONNECTOR_RSS)
        item = h.effective(capability="connector_rss").json()["items"][0]
        assert item["effective_enabled"] is False
        assert item["has_override"] is False


# --------------------------------------------------------------------------- #
# Filtering, tenancy, and unknown-capability rejection (cases 6, 12/§12)
# --------------------------------------------------------------------------- #
class TestFilterAndTenancy:
    def test_capability_filter_narrows_to_one(self, h):
        body = h.effective(capability="scout_scheduling").json()
        assert [i["capability"] for i in body["items"]] == ["scout_scheduling"]

    def test_tenant_mismatch_is_404(self, h):
        # ws-b belongs to org-b; querying it under org-a must 404 (non-enumerating).
        assert h.effective(org=_ORG_A, ws=_WS_B).status_code == 404

    def test_absent_workspace_is_404(self, h):
        assert h.effective(org=_ORG_A, ws="ws-does-not-exist").status_code == 404

    def test_tenant_404_is_non_enumerating(self, h):
        mismatch = h.effective(org=_ORG_A, ws=_WS_B)
        absent = h.effective(org=_ORG_A, ws="ws-nope")
        assert mismatch.status_code == absent.status_code == 404
        # Same generic envelope: a caller cannot tell "exists but not yours" from "gone".
        assert mismatch.json()["error"]["code"] == absent.json()["error"]["code"] == "not_found"

    def test_unknown_capability_value_is_422(self, h):
        assert h.effective(capability="not_a_capability").status_code == 422

    def test_override_in_one_workspace_does_not_leak_to_another(self, h):
        # Seed an enable in ws-a/org-a; ws-b/org-b must resolve independently to the
        # (dark) global default — no cross-tenant leak.
        h.seed_enable(org=_ORG_A, ws=_WS_A, capability=Capability.SCOUT_SCHEDULING)
        item = h.effective(org=_ORG_B, ws=_WS_B, capability="scout_scheduling").json()["items"][0]
        assert item["effective_enabled"] is False
        assert item["decided_by"] == "global_configuration"
        assert item["has_override"] is False


# --------------------------------------------------------------------------- #
# Read-only — the effective read mutates nothing (writes no row, no audit)
# --------------------------------------------------------------------------- #
class TestReadOnly:
    def test_effective_read_writes_no_override_row_and_no_audit(self, h):
        before = h.counts()
        assert h.effective().status_code == 200
        assert h.effective(capability="opportunity_feedback").status_code == 200
        assert h.counts() == before  # nothing persisted, nothing audited


# --------------------------------------------------------------------------- #
# Contract — the additive route is in the OpenAPI schema (case 25 presence)
# --------------------------------------------------------------------------- #
class TestOpenAPI:
    def test_effective_endpoint_and_operation_id_present(self):
        schema = app.openapi()
        full_path = f"{API}{EFFECTIVE_PATH}"
        assert full_path in schema["paths"]
        get_op = schema["paths"][full_path]["get"]
        assert get_op.get("operationId")
        assert "effective" in get_op["operationId"]

    def test_registry_route_still_present(self):
        schema = app.openapi()
        assert f"{API}/internal/system/capabilities/registry" in schema["paths"]
