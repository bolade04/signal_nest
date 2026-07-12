#!/usr/bin/env python
"""HTTP smoke test for a running SignalNest API (SQLite + mock LLM, seeded demo).

Exercises the security- and product-critical guarantees over real HTTP against a
live server, complementing the in-process TestClient suite. Intended for CI's
integration-smoke job but runnable locally against any migrated + seeded instance:

    npm run demo:setup
    scripts/smoke_http.py            # defaults to http://127.0.0.1:8000

Checks (any failure exits non-zero):
  * GET  /health                              -> 200 {"status": "ok"}
  * GET  <api>/organizations (no auth)        -> 401/403
  * POST <api>/auth/login (demo creds)        -> 200 + bearer token
  * GET  <api>/organizations                  -> >=1 org
  * GET  <api>/organizations/{id}/workspaces  -> >=1 workspace
  * GET  <api>/workspaces/{ws}/locations      -> the four demo cities present
  * GET  <api>/workspaces/{ws}/opportunities?location_id=  (per city)
        - every returned row belongs to the requested location only
        - Dallas / London / Lagos / Nairobi each return >=1 opportunity
        - no opportunity id appears under two locations (no cross-market leak)
  * GET  <api>/workspaces/{ws}/opportunities/{id}   -> 200 detail, id matches
  * PUT  <api>/workspaces/{ws}/opportunities/{id}/status {"status": "saved"}
        then GET detail -> status persisted as "saved"
  * GET  <api>/api/v1/openapi.json            -> 200 with "openapi" key
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_PREFIX = os.environ.get("SMOKE_API_PREFIX", "/api/v1")
DEMO_EMAIL = os.environ.get("SMOKE_DEMO_EMAIL", "demo@signalnest.dev")
DEMO_PASSWORD = os.environ.get("SMOKE_DEMO_PASSWORD", "demo1234")
EXPECTED_CITIES = {"Dallas", "London", "Lagos", "Nairobi"}

API = f"{BASE_URL}{API_PREFIX}"

_passed = 0


def ok(msg: str) -> None:
    global _passed
    _passed += 1
    print(f"  PASS  {msg}")


def fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    print(f"SignalNest HTTP smoke test -> {BASE_URL}")
    client = httpx.Client(timeout=15.0)

    # 1. Health --------------------------------------------------------------
    r = client.get(f"{BASE_URL}/health")
    if r.status_code != 200 or r.json().get("status") != "ok":
        fail(f"/health returned {r.status_code}: {r.text}")
    ok("health endpoint reports ok")

    # 2. Unauthenticated protected request is rejected -----------------------
    r = client.get(f"{API}/organizations")
    if r.status_code not in (401, 403):
        fail(f"unauthenticated /organizations expected 401/403, got {r.status_code}")
    ok(f"unauthenticated protected request rejected ({r.status_code})")

    # 3. Demo login ----------------------------------------------------------
    r = client.post(
        f"{API}/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    if r.status_code != 200 or "access_token" not in r.json():
        fail(f"demo login failed ({r.status_code}): {r.text}")
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    ok("demo login succeeded")

    # 4. Organization + workspace -------------------------------------------
    orgs = client.get(f"{API}/organizations", headers=auth).json()
    if not orgs:
        fail("expected at least one seeded organization")
    workspaces = client.get(
        f"{API}/organizations/{orgs[0]['id']}/workspaces", headers=auth
    ).json()
    if not workspaces:
        fail("expected at least one seeded workspace")
    ws = workspaces[0]["id"]
    ok("resolved seeded organization and workspace")

    # 5. Locations: the four demo cities exist -------------------------------
    locations = client.get(f"{API}/workspaces/{ws}/locations", headers=auth).json()
    cities = {loc.get("city") for loc in locations}
    missing = EXPECTED_CITIES - cities
    if missing:
        fail(f"missing seeded demo cities: {sorted(missing)} (got {sorted(cities)})")
    ok(f"all four demo cities present: {sorted(EXPECTED_CITIES)}")

    # 6. Per-location opportunity feeds + strict isolation -------------------
    ids_by_city: dict[str, set[str]] = {}
    for loc in locations:
        city = loc.get("city")
        rows = client.get(
            f"{API}/workspaces/{ws}/opportunities",
            params={"location_id": loc["id"]},
            headers=auth,
        ).json()
        for row in rows:
            if row["location_id"] != loc["id"]:
                fail(
                    f"{city} feed leaked opportunity for location "
                    f"{row['location_id']} (expected {loc['id']})"
                )
        ids_by_city[city] = {r["id"] for r in rows}

    for city in EXPECTED_CITIES:
        count = len(ids_by_city.get(city, set()))
        if count < 1:
            fail(f"{city} feed returned no opportunities")
        ok(f"{city} opportunity feed returned {count} opportunity(ies), all in-market")

    all_ids = [oid for ids in ids_by_city.values() for oid in ids]
    if len(all_ids) != len(set(all_ids)):
        fail("an opportunity id appeared under more than one location (cross-market leak)")
    ok(f"no cross-market contamination across {len(all_ids)} opportunities")

    # 7. Opportunity detail --------------------------------------------------
    sample_city = "Dallas"
    sample_id = sorted(ids_by_city[sample_city])[0]
    r = client.get(
        f"{API}/workspaces/{ws}/opportunities/{sample_id}", headers=auth
    )
    if r.status_code != 200 or r.json().get("id") != sample_id:
        fail(f"opportunity detail failed ({r.status_code}): {r.text}")
    ok("opportunity detail endpoint returns the requested opportunity")

    # 8. Status update + persistence -----------------------------------------
    r = client.put(
        f"{API}/workspaces/{ws}/opportunities/{sample_id}/status",
        json={"status": "saved"},
        headers=auth,
    )
    if r.status_code != 200:
        fail(f"status update failed ({r.status_code}): {r.text}")
    detail = client.get(
        f"{API}/workspaces/{ws}/opportunities/{sample_id}", headers=auth
    ).json()
    if detail.get("status") != "saved":
        fail(f"status did not persist; got {detail.get('status')!r}")
    ok("opportunity status update persisted (status=saved)")

    # 9. OpenAPI endpoint ----------------------------------------------------
    r = client.get(f"{API}/openapi.json")
    if r.status_code != 200 or "openapi" not in r.json():
        fail(f"openapi endpoint failed ({r.status_code})")
    ok("openapi document served")

    print(f"\nSmoke test passed: {_passed} checks.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.HTTPError as exc:
        print(f"  FAIL  HTTP error contacting {BASE_URL}: {exc}", file=sys.stderr)
        sys.exit(1)
