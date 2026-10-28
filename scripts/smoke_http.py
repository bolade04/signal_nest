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
  * GET  <api>/workspaces/{ws}/scout-requests (P6-DATA-2)
        - every demo city's completed run states its source coverage (no silent empty)
  * GET  <api>/api/v1/openapi.json            -> 200 with "openapi" key

Real-data phase (P6-CI-11), when SMOKE_REAL_DATA is "replay" or "required" and the
database was extended by ``python -m app.connectors.replay seed`` (the live RSS
connector reading the capture corpus from a loopback server):
  * a run reports covered/live_sources and its location's opportunities are all
    in-location, each citing an rss_news source key on every evidence entry
  * a run reports not_covered and its location has no opportunities
  * "required": every live-source opportunity is real (is_simulated false); a
    synthetic corpus fails here, so "required" asserts real data, "replay" asserts
    the live code path and reports which it saw
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_PREFIX = os.environ.get("SMOKE_API_PREFIX", "/api/v1")
DEMO_EMAIL = os.environ.get("SMOKE_DEMO_EMAIL", "demo@signalnest.dev")
DEMO_PASSWORD = os.environ.get("SMOKE_DEMO_PASSWORD", "demo1234")
REAL_DATA_MODE = os.environ.get("SMOKE_REAL_DATA", "").strip().lower()
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

    # 9. Source coverage is stated for every demo run (P6-DATA-2) -------------
    runs = client.get(f"{API}/workspaces/{ws}/scout-requests", headers=auth).json()
    city_of = {loc["id"]: loc.get("city") for loc in locations}
    stated = {city_of.get(r["location_id"]) for r in runs if r.get("coverage")}
    if EXPECTED_CITIES - stated:
        fail(f"runs without a coverage reason: {sorted(EXPECTED_CITIES - stated)}")
    for run in runs:
        coverage = run.get("coverage")
        if city_of.get(run["location_id"]) in EXPECTED_CITIES and coverage["status"] != "covered":
            fail(f"demo run {run['name']!r} reports {coverage}")
    ok("every demo city's run states its source coverage")

    # 10. Real data through the live connector (P6-CI-11), when enabled ------
    if REAL_DATA_MODE and REAL_DATA_MODE not in ("replay", "required"):
        fail(f"SMOKE_REAL_DATA must be 'replay' or 'required', got {REAL_DATA_MODE!r}")
    if not REAL_DATA_MODE:
        print("  NOTE  SMOKE_REAL_DATA unset: real-data phase not run (fixture smoke only)")
    live = [r for r in runs if (r.get("coverage") or {}).get("reason") == "live_sources"]
    uncovered = [r for r in runs if (r.get("coverage") or {}).get("status") == "not_covered"]
    if REAL_DATA_MODE and not live:
        fail("no scout run reports covered/live_sources (was the replay seed run?)")
    if REAL_DATA_MODE and not uncovered:
        fail("no scout run reports not_covered (the replay seed adds one)")
    for run in live if REAL_DATA_MODE else []:
        rows = client.get(
            f"{API}/workspaces/{ws}/opportunities",
            params={"location_id": run["location_id"]},
            headers=auth,
        ).json()
        if not rows:
            fail(f"live-source market {run['resolved_market']} returned no opportunities")
        simulated = 0
        for row in rows:
            if row["location_id"] != run["location_id"]:
                fail(f"live-source feed leaked opportunity {row['id']} across locations")
            detail = client.get(
                f"{API}/workspaces/{ws}/opportunities/{row['id']}", headers=auth
            ).json()
            for entry in detail.get("observed_evidence") or []:
                if entry.get("source_type") != "rss_news" or not entry.get("source_key"):
                    fail(f"opportunity {row['id']} has evidence without a source key")
            simulated += 1 if row["is_simulated"] else 0
        if REAL_DATA_MODE == "required" and simulated:
            fail(
                f"{run['resolved_market']}: {simulated} of {len(rows)} live-source "
                "opportunities are simulated (synthetic capture corpus) - real data required"
            )
        kind = "simulated (synthetic captures)" if simulated else "real"
        ok(
            f"{run['resolved_market']}: {len(rows)} live-connector opportunity(ies), "
            f"all in-location with source provenance; data is {kind}"
        )
    for run in uncovered if REAL_DATA_MODE else []:
        rows = client.get(
            f"{API}/workspaces/{ws}/opportunities",
            params={"location_id": run["location_id"]},
            headers=auth,
        ).json()
        if rows:
            fail(f"not-covered market {run['resolved_market']} returned opportunities")
        ok(f"{run['resolved_market']}: not_covered stated, no opportunities")

    # 11. OpenAPI endpoint ---------------------------------------------------
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
