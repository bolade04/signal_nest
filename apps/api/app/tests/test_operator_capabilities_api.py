"""Phase 4A-C.4.1: operator capability registry read — route tests.

Covers the first, read-only slice of the operator capability-governance surface
added in ``app.system.internal_capabilities_routes``:

    GET /internal/system/capabilities/registry

The registry read is a pure projection of the closed capability registry
(:mod:`app.capabilities.registry`); it touches no database, consumes neither the
resolver nor the override service, and mutates nothing. These tests assert:
operator-only authorization (401 anonymous / 403 non-operator), the exact secret-free
response shape, that the projection reproduces ``iter_capabilities()`` + ``get_policy()``
exactly once each in canonical order (no unknown/duplicate/missing capability),
dark-state invariants (all three global flags remain ``False``; the read activates
nothing), that the registry router still imports the override service **not at all**
while now importing the resolver only as the sanctioned effective-read consumer added
in 4A-C.4.2 (the service stays unconsumed until 4A-C.4.3), and that the additive
route is present in the OpenAPI schema without disturbing existing operator routes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.capabilities.registry import get_policy, iter_capabilities
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import User

API = get_settings().api_prefix
REGISTRY_PATH = "/internal/system/capabilities/registry"

# apps/api/app — this test's grandparent package dir.
_APP_DIR = Path(__file__).resolve().parents[1]
_ROUTER_FILE = _APP_DIR / "system" / "internal_capabilities_routes.py"

# The exact operator-safe field set of one registry item.
_ITEM_FIELDS = {
    "capability",
    "label",
    "global_flag_attr",
    "workspace_enableable",
    "workspace_disableable",
    "future_activation_phase",
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


def _module_binds(tree: ast.AST, target: str) -> bool:
    """Whether ``tree`` really *imports* ``target`` (e.g. ``app.capabilities.resolver``).

    Recognizes ``import target[.x]``, ``from target[...] import x``, and
    ``from <parent> import <leaf>`` where ``<parent>.<leaf> == target``. A docstring or
    comment mention is ignored — only a binding ``import`` counts.
    """
    parent, _, leaf = target.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == target or alias.name.startswith(target + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == target or module.startswith(target + "."):
                return True
            if module == parent and any(a.name == leaf for a in node.names):
                return True
    return False


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("operator_capabilities")
    engine = create_engine(
        f"sqlite:///{tmp / 'caps.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
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
        yield _Harness(TestClient(app))
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class _Harness:
    def __init__(self, client):
        self.client = client
        self.op = {"Authorization": f"Bearer {create_access_token(_OPERATOR)}"}
        self.cust = {"Authorization": f"Bearer {create_access_token(_CUSTOMER)}"}

    def get(self, path: str, *, auth=None):
        return self.client.get(f"{API}{path}", headers=self.op if auth is None else auth)


# --------------------------------------------------------------------------- #
# Authorization — the registry read is operator-only
# --------------------------------------------------------------------------- #
class TestAuthorization:
    def test_anonymous_is_401(self, h):
        assert h.client.get(f"{API}{REGISTRY_PATH}").status_code == 401

    def test_non_operator_is_403(self, h):
        assert h.get(REGISTRY_PATH, auth=h.cust).status_code == 403

    def test_operator_is_200(self, h):
        assert h.get(REGISTRY_PATH).status_code == 200


# --------------------------------------------------------------------------- #
# Response shape + canonical registry projection
# --------------------------------------------------------------------------- #
class TestRegistryProjection:
    def test_envelope_shape(self, h):
        body = h.get(REGISTRY_PATH).json()
        assert set(body) == {"items"}
        assert isinstance(body["items"], list)

    def test_each_item_has_exactly_the_operator_safe_fields(self, h):
        for item in h.get(REGISTRY_PATH).json()["items"]:
            assert set(item) == _ITEM_FIELDS

    def test_projection_matches_iter_capabilities_in_order(self, h):
        # Deterministic canonical order, exactly the closed registry — no more, no less.
        returned = [i["capability"] for i in h.get(REGISTRY_PATH).json()["items"]]
        expected = [c.value for c in iter_capabilities()]
        assert returned == expected

    def test_no_duplicate_or_unknown_capability(self, h):
        returned = [i["capability"] for i in h.get(REGISTRY_PATH).json()["items"]]
        known = {c.value for c in iter_capabilities()}
        assert len(returned) == len(set(returned))  # no duplicate
        assert set(returned) == known  # no unknown, none missing

    def test_policy_fields_match_canonical_definitions(self, h):
        by_cap = {i["capability"]: i for i in h.get(REGISTRY_PATH).json()["items"]}
        for capability in iter_capabilities():
            policy = get_policy(capability)
            item = by_cap[capability.value]
            assert item["label"] == policy.label
            assert item["global_flag_attr"] == policy.global_flag_attr
            assert item["workspace_enableable"] == policy.workspace_enableable
            assert item["workspace_disableable"] == policy.workspace_disableable
            assert item["future_activation_phase"] == policy.future_activation_phase

    def test_rss_is_not_workspace_enableable(self, h):
        by_cap = {i["capability"]: i for i in h.get(REGISTRY_PATH).json()["items"]}
        assert by_cap["connector_rss"]["workspace_enableable"] is False


# --------------------------------------------------------------------------- #
# Secret-free + no internal implementation object leaks
# --------------------------------------------------------------------------- #
class TestSecretFree:
    def test_response_body_carries_no_secret(self, h):
        blob = h.get(REGISTRY_PATH).text.lower()
        for token in _FORBIDDEN:
            assert token not in blob


# --------------------------------------------------------------------------- #
# Dark-state — the registry read activates nothing; the router consumes neither
# the resolver nor the override service (4A-C.4.1 keeps both guards green).
# --------------------------------------------------------------------------- #
class TestDarkState:
    def test_all_three_global_flags_remain_false(self):
        settings = get_settings()
        assert settings.connector_rss_enabled is False
        assert settings.scout_scheduling_enabled is False
        assert settings.opportunity_feedback_enabled is False

    def test_registry_read_only_reports_future_activation_phase(self, h):
        # Every capability is still dark: the registry only documents the *future*
        # activation phase; it activates nothing now.
        for item in h.get(REGISTRY_PATH).json()["items"]:
            assert item["future_activation_phase"]

    def test_router_imports_the_resolver_as_the_sanctioned_consumer(self):
        # 4A-C.4.2 makes the operator router the FIRST sanctioned production consumer
        # of the resolver (the effective-read path). Assert the binding really exists
        # via a precise AST scan, so the reframed live-gate/allow-list guard in
        # test_capability_override_service.py is grounded in a real import.
        tree = ast.parse(_ROUTER_FILE.read_text(encoding="utf-8"), filename=str(_ROUTER_FILE))
        assert _module_binds(tree, "app.capabilities.resolver")

    def test_router_does_not_import_the_override_service(self):
        # The resolver is now consumed, but the override service is NOT: the write
        # plane (list/set/clear) lands in 4A-C.4.3+. The service therefore stays fully
        # unconsumed after 4A-C.4.2, keeping every mutation path dark. Assert the new
        # router binds no service symbol via a precise AST scan.
        tree = ast.parse(_ROUTER_FILE.read_text(encoding="utf-8"), filename=str(_ROUTER_FILE))
        assert not _module_binds(tree, "app.capabilities.service")


# --------------------------------------------------------------------------- #
# Contract — the additive route is in the OpenAPI schema without disturbing peers
# --------------------------------------------------------------------------- #
class TestOpenAPI:
    def test_registry_endpoint_and_operation_id_present(self):
        schema = app.openapi()
        full_path = f"{API}{REGISTRY_PATH}"
        assert full_path in schema["paths"]
        get_op = schema["paths"][full_path]["get"]
        assert get_op.get("operationId")
        assert "registry" in get_op["operationId"]

    def test_existing_operator_route_unaffected(self):
        schema = app.openapi()
        assert f"{API}/internal/system/overview" in schema["paths"]
