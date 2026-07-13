"""Integration tests exercising the real FastAPI app against the seeded SQLite DB.

These prove the security-critical guarantees end-to-end through HTTP:
  * unauthenticated requests are rejected,
  * a valid demo login yields a usable bearer token,
  * opportunity results are strictly isolated per location (no cross-market leak).

They require a migrated + seeded database (``npm run demo:setup``). If the demo
data is absent the whole module is skipped rather than failing spuriously.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.seed import DEMO_EMAIL, DEMO_PASSWORD
from app.main import app

API = get_settings().api_prefix


@pytest.fixture(scope="module")
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client: TestClient) -> dict[str, str]:
    resp = client.post(
        f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    if resp.status_code != 200:
        pytest.skip("Demo account not seeded; run `npm run demo:setup` first.")
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _first_workspace(client: TestClient, auth: dict[str, str]) -> str:
    orgs = client.get(f"{API}/organizations", headers=auth).json()
    assert orgs, "expected at least one seeded organization"
    ws = client.get(
        f"{API}/organizations/{orgs[0]['id']}/workspaces", headers=auth
    ).json()
    assert ws, "expected at least one seeded workspace"
    return ws[0]["id"]


def test_unauthenticated_request_is_rejected(client: TestClient):
    resp = client.get(f"{API}/organizations")
    assert resp.status_code in (401, 403)


def test_login_returns_token_and_membership(client: TestClient, auth: dict[str, str]):
    # The auth fixture already logged in; a protected call should now succeed.
    resp = client.get(f"{API}/organizations", headers=auth)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_opportunities_are_isolated_per_location(client: TestClient, auth: dict[str, str]):
    ws = _first_workspace(client, auth)
    locations = client.get(f"{API}/workspaces/{ws}/locations", headers=auth).json()
    assert len(locations) >= 2, "need multiple seeded locations to prove isolation"

    seen_ids: dict[str, set[str]] = {}
    for loc in locations:
        rows = client.get(
            f"{API}/workspaces/{ws}/opportunities",
            params={"location_id": loc["id"]},
            headers=auth,
        ).json()
        # Every returned opportunity must belong to the requested location only.
        for row in rows:
            assert row["location_id"] == loc["id"], (
                f"location {loc['id']} query leaked opportunity for {row['location_id']}"
            )
        seen_ids[loc["id"]] = {r["id"] for r in rows}

    # No opportunity id may appear under two different locations.
    all_ids = [oid for ids in seen_ids.values() for oid in ids]
    assert len(all_ids) == len(set(all_ids)), "opportunity leaked across locations"


def test_system_health_is_live(client: TestClient):
    resp = client.get(f"{API}/system/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_system_readiness_is_ready_in_seeded_local_mode(client: TestClient):
    resp = client.get(f"{API}/system/readiness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["schema_migrated"] is True
    assert body["unconfigured"] == []


def _register_non_operator(client: TestClient) -> dict[str, str]:
    """Register a fresh customer (is_operator defaults to False) and return auth."""
    import uuid

    email = f"customer-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post(
        f"{API}/auth/register",
        json={
            "email": email,
            "full_name": "Ordinary Customer",
            "password": "customer1234",
            "organization_name": "Customer Co",
        },
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    assert resp.json()["user"]["is_operator"] is False
    return {"Authorization": f"Bearer {token}"}


def test_system_capabilities_requires_authentication(client: TestClient):
    # The coarse summary is authenticated (no anonymous runtime introspection).
    resp = client.get(f"{API}/system/capabilities")
    assert resp.status_code == 401


def test_system_capabilities_is_coarse_summary_only(
    client: TestClient, auth: dict[str, str]
):
    # Any authenticated caller gets a coarse summary WITHOUT per-capability
    # backend topology (that detail is operator-only).
    resp = client.get(f"{API}/system/capabilities", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_local_mode"] is True
    assert "capabilities" not in body
    blob = resp.text.lower()
    for forbidden in ("password", "api_key", "secret", "redis://", "postgresql://"):
        assert forbidden not in blob


def test_internal_capabilities_requires_operator(client: TestClient):
    # Anonymous -> 401; authenticated non-operator -> 403.
    assert client.get(f"{API}/internal/system/capabilities").status_code == 401
    non_operator = _register_non_operator(client)
    resp = client.get(f"{API}/internal/system/capabilities", headers=non_operator)
    assert resp.status_code == 403


def test_internal_capabilities_is_detailed_and_secret_free(
    client: TestClient, auth: dict[str, str]
):
    # The seeded demo owner is an operator in the test environment.
    resp = client.get(f"{API}/internal/system/capabilities", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_local_mode"] is True
    names = {c["name"] for c in body["capabilities"]}
    assert names == {
        "database",
        "queue",
        "durable_queue",
        "cache",
        "vector",
        "storage",
        "llm",
    }
    blob = resp.text.lower()
    for forbidden in ("password", "api_key", "secret", "redis://", "postgresql://"):
        assert forbidden not in blob


def test_internal_readiness_requires_operator(client: TestClient):
    assert client.get(f"{API}/internal/system/readiness").status_code == 401
    non_operator = _register_non_operator(client)
    resp = client.get(f"{API}/internal/system/readiness", headers=non_operator)
    assert resp.status_code == 403


def test_internal_readiness_reports_probe_diagnostics(
    client: TestClient, auth: dict[str, str]
):
    resp = client.get(f"{API}/internal/system/readiness", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    names = {p["name"] for p in body["probes"]}
    assert names == {
        "database",
        "queue",
        "durable_queue",
        "cache",
        "vector",
        "storage",
        "llm",
    }
    # Diagnostics carry durations but never secret material.
    for probe in body["probes"]:
        assert probe["duration_ms"] >= 0
    blob = resp.text.lower()
    for forbidden in ("password", "api_key", "secret", "redis://", "postgresql://"):
        assert forbidden not in blob


def test_jobs_endpoint_requires_authentication(client: TestClient):
    # Tenant-scoped job listing is never anonymous.
    resp = client.get(f"{API}/workspaces/any-ws/jobs")
    assert resp.status_code in (401, 403)


def test_jobs_listing_is_customer_safe(client: TestClient, auth: dict[str, str]):
    ws = _first_workspace(client, auth)
    resp = client.get(f"{API}/workspaces/{ws}/jobs", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    # Paginated envelope shape.
    assert set(body) == {"items", "total", "limit", "offset"}
    # The customer view must never leak worker/lease/payload/infra fields, even
    # if the seeded workspace has jobs.
    blob = resp.text.lower()
    for forbidden in ("worker_id", "lease_expires", "payload_hash", "heartbeat", "idempotency"):
        assert forbidden not in blob


def test_internal_jobs_requires_operator(client: TestClient):
    # Anonymous -> 401; authenticated non-operator -> 403.
    assert client.get(f"{API}/internal/system/jobs").status_code == 401
    non_operator = _register_non_operator(client)
    resp = client.get(f"{API}/internal/system/jobs", headers=non_operator)
    assert resp.status_code == 403


def test_internal_jobs_is_diagnostics_and_secret_free(
    client: TestClient, auth: dict[str, str]
):
    resp = client.get(f"{API}/internal/system/jobs", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"status_counts", "recent"}
    assert isinstance(body["status_counts"], dict)
    blob = resp.text.lower()
    for forbidden in ("password", "api_key", "secret", "redis://", "postgresql://"):
        assert forbidden not in blob


def test_unfiltered_feed_is_superset_of_each_location(
    client: TestClient, auth: dict[str, str]
):
    ws = _first_workspace(client, auth)
    locations = client.get(f"{API}/workspaces/{ws}/locations", headers=auth).json()
    all_rows = client.get(f"{API}/workspaces/{ws}/opportunities", headers=auth).json()
    all_ids = {r["id"] for r in all_rows}
    for loc in locations:
        rows = client.get(
            f"{API}/workspaces/{ws}/opportunities",
            params={"location_id": loc["id"]},
            headers=auth,
        ).json()
        assert {r["id"] for r in rows}.issubset(all_ids)
