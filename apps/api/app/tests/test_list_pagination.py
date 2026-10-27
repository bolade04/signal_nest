"""P6-PLAT-12: cursor pagination on the customer lists.

The lists are the opportunity feed, audit logs, scout requests and the nine
campaign-context collections. Each keeps its JSON-array body. ``X-Total-Count`` carries
the number of rows that match the filters. ``X-Next-Cursor`` is an opaque, HMAC-signed
cursor and is present only when another page follows. This module proves, through real
HTTP:

* walking every page with a small page size returns exactly the unpaged rows, in the same
  order, with no repeat and no gap, including when many rows share a sort value (the id
  breaks ties), and the total never equals the page length unless the page is the
  whole result;
* the defaults are what they were: the feed and audit logs return at most 100 rows, the
  scout-request and context lists return every row, and a default response carries the
  total;
* a cursor is tamper-evident and bound: an altered or foreign-key-signed cursor, or one
  replayed on another route, another workspace (same organization or not) or with other
  filters, gets ``422 "Invalid cursor."``;
* a row created while a client pages through the list does not repeat or displace an
  earlier row;
* the two headers are exposed to a cross-origin browser client.

Rows with no creation route (opportunities) and rows that need identical timestamps (the
tie cases) are written directly through a witness engine. Everything else goes through
the API on a temporary SQLite file. ``get_db`` is the only overridden dependency.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.campaign_context.models import ProductProfile
from app.core import pagination
from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.opportunities.models import Opportunity

API = get_settings().api_prefix
PASSWORD = "plat12-paging-pw-123"
INVALID = (422, "validation_error", "Invalid cursor.")
TOTAL, NEXT = "x-total-count", "x-next-cursor"
CONTEXT_PATHS = (
    "products",
    "audiences",
    "competitors",
    "brand-voice",
    "offers",
    "claims",
    "source-preferences",
    "channel-preferences",
    "campaigns",
)
CONTEXT_BODY = {
    "products": {"name": "P"},
    "audiences": {"label": "A"},
    "competitors": {"name": "C"},
    "brand-voice": {"tone": ["warm"]},
    "offers": {"name": "O"},
    "claims": {"text": "T"},
    "source-preferences": {"source_type": "news"},
    "channel-preferences": {"channel": "email"},
    "campaigns": {"name": "K"},
}
OPPORTUNITIES_IN_A1 = 105  # more than the feed's default page of 100


def _engine(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _rate_limiter() -> RateLimitMiddleware:
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None, "RateLimitMiddleware not found"
    return node


def _error(r) -> tuple[int, str | None, str | None]:
    payload = r.json() if r.content else {}
    err = payload.get("error") if isinstance(payload, dict) else None
    return r.status_code, (err or {}).get("code"), (err or {}).get("message")


@dataclass
class World:
    client: TestClient
    witness: Engine
    org_a: str
    owner_a: str
    owner_b: str
    ws_a1: str
    ws_a2: str
    ws_b: str
    brand_a1: str
    loc_a1: str

    def get(self, path: str, params: dict | None = None, token: str | None = None):
        assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
        _rate_limiter()._hits.clear()
        return self.client.get(
            API + path,
            params=params,
            headers={"Authorization": f"Bearer {token or self.owner_a}"},
        )

    def post(self, path: str, body: dict, token: str | None = None) -> dict:
        _rate_limiter()._hits.clear()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = self.client.post(API + path, json=body, headers=headers)
        assert r.status_code == 201, (path, r.status_code, r.text)
        return r.json()

    def walk(self, path: str, params: dict, limit: int) -> tuple[list[str], set[str]]:
        """Follow ``X-Next-Cursor`` to the end; returns the ids and every total seen."""
        ids: list[str] = []
        totals: set[str] = set()
        cursor = None
        for _ in range(1000):
            page_params = {**params, "limit": limit}
            if cursor:
                page_params["cursor"] = cursor
            r = self.get(path, page_params)
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body) <= limit
            ids.extend(row["id"] for row in body)
            totals.add(r.headers[TOTAL])
            cursor = r.headers.get(NEXT)
            if cursor is None:
                return ids, totals
            assert len(body) == limit  # only a full page may announce another
        raise AssertionError("pagination did not terminate")


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[World]:
    db_file = tmp_path_factory.mktemp("plat12") / "plat12.db"
    request_engine, witness = _engine(db_file), _engine(db_file)
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
    )

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
    client = TestClient(app)
    try:
        shell = World(client, witness, "", "", "", "", "", "", "", "")

        def register(key: str) -> tuple[str, str]:
            owner = shell.post(
                "/auth/register",
                {
                    "email": f"plat12-{key}@example.com",
                    "full_name": f"{key} owner",
                    "password": PASSWORD,
                    "organization_name": f"PLAT12 {key}",
                },
            )
            return owner["memberships"][0]["organization_id"], owner["access_token"]

        def workspace(org_id: str, token: str, name: str) -> tuple[str, str]:
            ws = shell.post(f"/organizations/{org_id}/workspaces", {"name": name}, token)
            ob = shell.post(
                f"/workspaces/{ws['id']}/onboarding",
                {"brand_name": f"Brand {name}", "profile": {"company_name": name}},
                token,
            )
            return ws["id"], ob["brand"]["id"]

        org_a, owner_a = register("a")
        org_b, owner_b = register("b")
        ws_a1, brand_a1 = workspace(org_a, owner_a, "A1")
        ws_a2, _ = workspace(org_a, owner_a, "A2")
        ws_b, _ = workspace(org_b, owner_b, "B")
        loc_a1 = shell.post(f"/workspaces/{ws_a1}/locations", {"name": "A1 HQ"}, owner_a)["id"]
        w = World(client, witness, org_a, owner_a, owner_b, ws_a1, ws_a2, ws_b, brand_a1, loc_a1)
        for ws, token in ((ws_a1, owner_a), (ws_a2, owner_a), (ws_b, owner_b)):
            for path in CONTEXT_PATHS:
                for _ in range(3):
                    w.post(f"/workspaces/{ws}/{path}", CONTEXT_BODY[path], token)
            for i in range(5):
                w.post(f"/workspaces/{ws}/scout-requests", {"name": f"Scout {i}"}, token)
        _write_opportunities(w)
        yield w
    finally:
        app.dependency_overrides.clear()
        _rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


def _write_opportunities(w: World) -> None:
    """105 opportunities in A1 with heavy ties on every sort key, and 2 in A2."""
    base = datetime(2026, 9, 1, tzinfo=UTC)
    request_a1 = w.get(f"/workspaces/{w.ws_a1}/scout-requests").json()[0]["id"]
    request_a2 = w.get(f"/workspaces/{w.ws_a2}/scout-requests").json()[0]["id"]
    rows = []
    for i in range(OPPORTUNITIES_IN_A1):
        rows.append(
            Opportunity(
                organization_id=w.org_a,
                workspace_id=w.ws_a1,
                brand_id=w.brand_a1,
                scout_request_id=request_a1,
                location_id=w.loc_a1 if i % 2 else None,
                title=f"Opportunity {i:03d}",
                classification="validated" if i % 3 else "early",
                decision="monitor",
                opportunity_score=50 + (i % 4),  # only four distinct values
                confidence_score=60,  # one value for every row
                priority_score=40 + (i % 7),
                relevance_score=70 + (i % 2),
                # Groups of five share a timestamp exactly.
                created_at=base + timedelta(minutes=i // 5),
            )
        )
    a2_brand = w.get(f"/workspaces/{w.ws_a2}/brands").json()[0]["id"]
    for i in range(2):
        rows.append(
            Opportunity(
                organization_id=w.org_a,
                workspace_id=w.ws_a2,
                brand_id=a2_brand,
                scout_request_id=request_a2,
                title=f"Other workspace {i}",
                classification="validated",
                decision="monitor",
            )
        )
    with sessionmaker(bind=w.witness, future=True)() as s:
        s.add_all(rows)
        s.commit()


def _all_ids(w: World, path: str, params: dict) -> list[str]:
    r = w.get(path, {**params, "limit": 500})
    assert r.status_code == 200 and NEXT not in r.headers, r.text
    return [row["id"] for row in r.json()]


# --------------------------------------------------------------------------- #
# 1. Walking every page reproduces the unpaged result exactly
# --------------------------------------------------------------------------- #
SORTS = ["priority", "opportunity", "confidence", "relevance", "created"]


@pytest.mark.parametrize("order", ["desc", "asc"])
@pytest.mark.parametrize("sort", SORTS)
def test_opportunity_pages_cover_every_row_once_in_order(world: World, sort, order):
    path = f"/workspaces/{world.ws_a1}/opportunities"
    params = {"sort": sort, "order": order}
    expected = _all_ids(world, path, params)
    assert len(expected) == OPPORTUNITIES_IN_A1
    for limit in (1, 7, 100):
        ids, totals = world.walk(path, params, limit)
        assert ids == expected, (sort, order, limit)
        assert totals == {str(OPPORTUNITIES_IN_A1)}


def test_opportunity_pages_respect_filters(world: World):
    path = f"/workspaces/{world.ws_a1}/opportunities"
    params = {"location_id": world.loc_a1, "classification": "validated", "sort": "created"}
    expected = _all_ids(world, path, params)
    assert 0 < len(expected) < OPPORTUNITIES_IN_A1
    ids, totals = world.walk(path, params, 4)
    assert ids == expected and totals == {str(len(expected))}


def test_the_tie_heavy_sort_really_is_ordered_by_value_then_id(world: World):
    r = world.get(
        f"/workspaces/{world.ws_a1}/opportunities",
        {"sort": "confidence", "order": "asc", "limit": 500},
    )
    ids = [row["id"] for row in r.json()]
    assert ids == sorted(ids)  # every confidence_score is equal, so the id alone orders


@pytest.mark.parametrize("path", ["scout-requests", "audit-logs", *CONTEXT_PATHS])
def test_other_lists_cover_every_row_once_in_order(world: World, path):
    url = f"/workspaces/{world.ws_a1}/{path}"
    expected = _all_ids(world, url, {})
    assert len(expected) >= 3
    for limit in (1, 2):
        ids, totals = world.walk(url, {}, limit)
        assert ids == expected
        assert totals == {str(len(expected))}


def test_context_rows_with_identical_timestamps_page_by_id(world: World):
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    with sessionmaker(bind=world.witness, future=True)() as s:
        s.add_all(
            ProductProfile(
                organization_id=world.org_a,
                workspace_id=world.ws_a1,
                brand_id=world.brand_a1,
                name=f"Tied {i}",
                created_at=stamp,
            )
            for i in range(6)
        )
        s.commit()
    url = f"/workspaces/{world.ws_a1}/products"
    expected = _all_ids(world, url, {})
    ids, _ = world.walk(url, {}, 2)
    assert ids == expected
    tied = [row for row in world.get(url).json() if row["name"].startswith("Tied")]
    assert [row["id"] for row in tied] == sorted(row["id"] for row in tied)


# --------------------------------------------------------------------------- #
# 2. Defaults are unchanged, and the total is the truth
# --------------------------------------------------------------------------- #
def test_feed_default_is_100_rows_with_the_true_total_and_a_next_cursor(world: World):
    r = world.get(f"/workspaces/{world.ws_a1}/opportunities")
    assert r.status_code == 200
    assert len(r.json()) == 100
    assert r.headers[TOTAL] == str(OPPORTUNITIES_IN_A1)  # never the page length
    assert r.headers.get(NEXT)


def test_audit_log_default_is_at_most_100_rows(world: World):
    r = world.get(f"/workspaces/{world.ws_a1}/audit-logs")
    assert r.status_code == 200 and len(r.json()) <= 100
    assert int(r.headers[TOTAL]) >= len(r.json())


@pytest.mark.parametrize("path", ["scout-requests", *CONTEXT_PATHS])
def test_unbounded_lists_still_return_every_row_by_default(world: World, path):
    r = world.get(f"/workspaces/{world.ws_a1}/{path}")
    assert r.status_code == 200
    assert r.headers[TOTAL] == str(len(r.json()))
    assert NEXT not in r.headers


@pytest.mark.parametrize("limit", ["0", "-1", "501"])
def test_audit_log_limit_is_bounded_on_both_sides(world: World, limit):
    r = world.get(f"/workspaces/{world.ws_a1}/audit-logs", {"limit": limit})
    assert r.status_code == 422, r.text


def test_unknown_sort_key_is_refused_not_silently_replaced(world: World):
    r = world.get(f"/workspaces/{world.ws_a1}/opportunities", {"sort": "opportunity_score"})
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# 3. Cursors are tamper-evident and bound to route, workspace and filters
# --------------------------------------------------------------------------- #
def _first_cursor(w: World, path: str, params: dict | None = None) -> str:
    r = w.get(path, {**(params or {}), "limit": 1})
    assert r.status_code == 200
    return r.headers[NEXT]


def _flip(text: str, index: int) -> str:
    ch = text[index]
    return text[:index] + ("A" if ch != "A" else "B") + text[index + 1 :]


def test_altered_or_garbage_cursors_are_refused(world: World):
    path = f"/workspaces/{world.ws_a1}/opportunities"
    cursor = _first_cursor(world, path)
    payload, mac = cursor.split(".")
    candidates = [
        _flip(payload, 3),
        f"{payload}.{_flip(mac, 3)}",
        payload,
        f"{cursor}.extra",
        "not-a-cursor",
        "....",
        "%%%.%%%",
    ]
    for bad in candidates:
        assert _error(world.get(path, {"cursor": bad})) == INVALID, bad


def test_a_cursor_signed_with_another_key_is_refused(world: World, monkeypatch):
    path = f"/workspaces/{world.ws_a1}/opportunities"
    settings = get_settings()
    real = settings.secret_key
    monkeypatch.setattr(settings, "secret_key", real + "-attacker")
    forged = pagination.encode_cursor(
        scope="opportunities",
        workspace_id=world.ws_a1,
        filters={"min_score": 0, "sort": "priority", "order": "desc"},
        key=[0, "0" * 32],
    )
    monkeypatch.setattr(settings, "secret_key", real)
    assert _error(world.get(path, {"cursor": forged})) == INVALID


def test_a_cursor_is_bound_to_its_route(world: World):
    cursor = _first_cursor(world, f"/workspaces/{world.ws_a1}/scout-requests")
    r = world.get(f"/workspaces/{world.ws_a1}/opportunities", {"cursor": cursor})
    assert _error(r) == INVALID
    product = _first_cursor(world, f"/workspaces/{world.ws_a1}/products")
    r = world.get(f"/workspaces/{world.ws_a1}/audiences", {"cursor": product})
    assert _error(r) == INVALID


def test_a_cursor_is_bound_to_its_workspace(world: World):
    cursor = _first_cursor(world, f"/workspaces/{world.ws_a1}/scout-requests")
    # Same organization, same member: only the binding refuses it.
    r = world.get(f"/workspaces/{world.ws_a2}/scout-requests", {"cursor": cursor})
    assert _error(r) == INVALID
    # Another organization replaying it on its own workspace.
    r = world.get(
        f"/workspaces/{world.ws_b}/scout-requests", {"cursor": cursor}, token=world.owner_b
    )
    assert _error(r) == INVALID


def test_a_cursor_is_bound_to_its_filters(world: World):
    path = f"/workspaces/{world.ws_a1}/opportunities"
    cursor = _first_cursor(world, path, {"sort": "created"})
    for changed in (
        {"sort": "priority"},
        {"sort": "created", "order": "asc"},
        {"sort": "created", "classification": "early"},
    ):
        assert _error(world.get(path, {**changed, "cursor": cursor})) == INVALID, changed
    ok = world.get(path, {"sort": "created", "cursor": cursor})
    assert ok.status_code == 200


def test_the_cursor_reveals_nothing_the_page_did_not(world: World):
    import base64
    import json

    path = f"/workspaces/{world.ws_a1}/opportunities"
    r = world.get(path, {"limit": 1, "sort": "created"})
    payload = r.headers[NEXT].split(".")[0]
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    last = r.json()[-1]
    assert data["w"] == world.ws_a1
    assert [v for _t, v in data["k"]][-1] == last["id"]


# --------------------------------------------------------------------------- #
# 4. Stable under concurrent inserts
# --------------------------------------------------------------------------- #
def test_a_row_created_mid_walk_neither_repeats_nor_displaces(world: World):
    url = f"/workspaces/{world.ws_a1}/scout-requests"
    before = _all_ids(world, url, {})
    first = world.get(url, {"limit": 2})
    seen = [row["id"] for row in first.json()]
    world.post(url, {"name": "Created mid-walk"}, world.owner_a)
    cursor = first.headers[NEXT]
    while cursor:
        r = world.get(url, {"limit": 2, "cursor": cursor})
        seen.extend(row["id"] for row in r.json())
        cursor = r.headers.get(NEXT)
    assert seen == before  # newest-first: the new row sorts before page one


# --------------------------------------------------------------------------- #
# 5. Browser clients on another origin can read the headers
# --------------------------------------------------------------------------- #
def test_paging_headers_are_exposed_to_cors_clients(world: World):
    origin = get_settings().cors_origins[0]
    _rate_limiter()._hits.clear()
    r = world.client.get(
        f"{API}/workspaces/{world.ws_a1}/opportunities",
        headers={"Authorization": f"Bearer {world.owner_a}", "Origin": origin},
    )
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"x-total-count", "x-next-cursor", "x-request-id"} <= exposed


def test_openapi_documents_the_headers_on_every_paginated_list():
    doc = app.openapi()["paths"]
    ws = f"{API}/workspaces/{{workspace_id}}"
    for path in ("opportunities", "audit-logs", "scout-requests", *CONTEXT_PATHS):
        headers = doc[f"{ws}/{path}"]["get"]["responses"]["200"]["headers"]
        assert set(headers) == {"X-Total-Count", "X-Next-Cursor"}, path
        params = {p["name"] for p in doc[f"{ws}/{path}"]["get"]["parameters"]}
        assert {"cursor", "limit"} <= params, path


# --------------------------------------------------------------------------- #
# 6. The codec itself
# --------------------------------------------------------------------------- #
def test_codec_round_trips_and_normalises_naive_timestamps_to_utc():
    naive = datetime(2026, 10, 20, 12, 30, 15, 123456)
    cursor = pagination.encode_cursor(
        scope="s", workspace_id="w", filters={"a": 1, "b": None}, key=[naive, 7, "id"]
    )
    values = pagination.decode_cursor(
        cursor, scope="s", workspace_id="w", filters={"a": 1}, arity=3
    )
    assert values == [naive.replace(tzinfo=UTC), 7, "id"]
    with pytest.raises(Exception) as exc:
        pagination.decode_cursor(cursor, scope="s", workspace_id="w", filters={"a": 1}, arity=2)
    assert getattr(exc.value, "message", "") == "Invalid cursor."


def test_audit_rows_are_scoped_before_paging(world: World):
    # The organization-wide rows (workspace_id NULL) of ANOTHER organization never appear.
    with world.witness.connect() as conn:
        foreign = {
            r.id
            for r in conn.execute(
                select(AuditLog.id).where(AuditLog.organization_id != world.org_a)
            )
        }
    ids, _ = world.walk(f"/workspaces/{world.ws_a1}/audit-logs", {}, 3)
    assert foreign and not (set(ids) & foreign)
