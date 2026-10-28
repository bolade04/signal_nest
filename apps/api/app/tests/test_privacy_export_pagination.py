"""D-9f (operator ruling 2026-10-27): the organization export is COMPLETE, by bounded pages.

    GET /api/v1/organizations/{id}/export                          index (exact OWNER)
    GET /api/v1/organizations/{id}/export/{table}?cursor=&limit=   one keyset page (exact OWNER)

The index carries every table's exact ``row_count``, its first page (default 1000 rows) and a
``next_cursor``; the page route walks a table to its end in ``(created_at, id)`` order, at
most 5000 rows a page. No row is ever dropped for size, so there is no ``truncated`` flag.

* **Traversal beyond 5000 rows** — 5,003 ``llm_usage_events`` rows (ties on ``created_at``
  included): every id appears exactly once across the index page and the pages after it, for
  the default page size and for the cap, and ``row_count`` is the exact total.
* **Current month** — rows stamped now are in the traversal, and the cost sum over the
  traversal equals the stored sum (the export never changes balances; nothing is written).
* **Tenant isolation** — another organization's rows never appear; B's OWNER, a non-OWNER of
  A and an anonymous caller cannot page A; a cursor carried from A to B's route can only
  position inside B's own rows.
* **Bounds and tampering** — ``limit`` above 5000 or below 1 is refused; a cursor that is not
  valid for the table (garbage, another table's, bad JSON, bad timestamp, wrong types) is
  422 ``invalid_cursor``; an unknown or not-exported table is the same 404 whatever it is.
* **Concurrent change** — the documented behaviour, pinned: a row inserted between pages
  appears iff its key sorts after the cursor, a row deleted before its page is read is
  absent, a row already served stays served; no row is ever served twice.
* **Plan** — the page query seeks ``ix_llm_usage_events_workspace_created`` (SQLite
  ``EXPLAIN QUERY PLAN``; the PostgreSQL plan is a CI matter).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, insert, select

from app.llm.models import LLMUsageEvent
from app.privacy import export as export_module
from app.privacy.inventory import TABLES
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    DAVE,
    ORG_A,
    ORG_B,
    WS_A,
    WS_A2,
    WS_B,
    PrivacyEnv,
    privacy_world,
)

TABLE = "llm_usage_events"
INDEX = f"{API}/organizations/{ORG_A}/export"
N_A = 5003


def _page_url(org: str, table: str) -> str:
    return f"{API}/organizations/{org}/export/{table}"


def _usage(row_id: str, org: str, ws: str, at: datetime, cost: float = 0.001) -> dict:
    return {
        "id": row_id,
        "organization_id": org,
        "workspace_id": ws,
        "scout_request_id": None,
        "job_id": None,
        "task": "signal.classify",
        "provider": "mock",
        "model": "mock-model",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": cost,
        "outcome": "ok",
        "created_at": at,
        "updated_at": at,
    }


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "pagination.db") as e:
        yield e


@pytest.fixture
def big(env: PrivacyEnv) -> PrivacyEnv:
    """A holds N_A usage rows over two workspaces: old months, ties on created_at, and the
    last 40 stamped now (the current month). B holds 3."""
    now = datetime.now(UTC)
    old = now - timedelta(days=120)
    rows = []
    for i in range(N_A):
        if i >= N_A - 40:
            at = now - timedelta(seconds=N_A - i)  # current month, distinct
        else:
            at = old + timedelta(seconds=i // 7)  # seven rows share each timestamp
        rows.append(
            _usage(f"a-u-{i:05d}", ORG_A, WS_A if i % 3 else WS_A2, at, cost=0.0001 * (i % 11 + 1))
        )
    rows += [_usage(f"b-u-{i}", ORG_B, WS_B, old + timedelta(seconds=i)) for i in range(3)]
    with env.session() as s:
        s.execute(insert(LLMUsageEvent.__table__), rows)
        s.commit()
    return env


def _walk(
    env: PrivacyEnv,
    table: str,
    *,
    user: str = ALICE,
    org: str = ORG_A,
    limit: int | None = None,
    headers=None,
):
    """Index first page, then the page route until ``next_cursor`` is null. Returns the rows
    in served order and the number of pages (index page included)."""
    headers = headers or env.auth(user)
    idx = env.client.get(f"{API}/organizations/{org}/export", headers=headers)
    assert idx.status_code == 200, idx.text
    entry = idx.json()["tables"][table]
    rows, cursor, pages = list(entry["rows"]), entry["next_cursor"], 1
    while cursor is not None:
        params = {"cursor": cursor} | ({"limit": limit} if limit else {})
        r = env.client.get(_page_url(org, table), params=params, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["table"] == table and len(body["rows"]) <= (limit or 1000)
        rows += body["rows"]
        cursor, pages = body["next_cursor"], pages + 1
    return rows, pages, entry["row_count"]


def test_traversal_beyond_5000_rows_default_page(big: PrivacyEnv):
    headers = big.auth(ALICE)  # a session is the only write; take the snapshot after it
    before = big.snapshot()
    rows, pages, total = _walk(big, TABLE, headers=headers)
    ids = [r["id"] for r in rows]
    assert total == N_A and len(ids) == N_A and len(set(ids)) == N_A  # each id exactly once
    assert pages == 6  # 5003 rows at 1000 a page
    assert set(ids) == {f"a-u-{i:05d}" for i in range(N_A)}
    assert not any(i.startswith("b-u-") for i in ids)
    assert [(r["created_at"], r["id"]) for r in rows] == sorted(
        (r["created_at"], r["id"]) for r in rows
    )
    assert big.snapshot() == before  # an export writes nothing


def test_traversal_at_the_page_cap_and_the_index_says_incomplete(big: PrivacyEnv):
    rows, pages, total = _walk(big, TABLE, limit=5000)
    assert (len(rows), pages, total) == (N_A, 2, N_A)
    assert len({r["id"] for r in rows}) == N_A
    index = big.client.get(INDEX, headers=big.auth(ALICE)).json()
    assert index["complete"] is False
    assert (index["page_size_default"], index["page_size_cap"]) == (1000, 5000)
    assert index["tables"][TABLE]["next_cursor"] is not None
    assert len(index["tables"][TABLE]["rows"]) == 1000
    assert set(index["tables"][TABLE]) == {"row_count", "rows", "next_cursor"}  # no `truncated`
    # a small table in the same export is whole in its first page
    assert index["tables"]["workspaces"]["next_cursor"] is None
    assert index["tables"]["workspaces"]["row_count"] == len(index["tables"]["workspaces"]["rows"])


def test_every_exported_table_reports_its_exact_row_count_and_is_traversable(
    env: PrivacyEnv, monkeypatch
):
    """With a page size of 1 every table, whatever its size, is walked to its end and the
    walk equals the table's exact row_count."""
    monkeypatch.setattr(export_module, "ORGANIZATION_EXPORT_DEFAULT_PAGE", 1)
    headers = env.auth(ALICE)
    index = env.client.get(INDEX, headers=headers).json()
    assert set(index["tables"]) == {n for n, e in TABLES.items() if e.exported}
    for name, entry in index["tables"].items():
        rows, cursor = list(entry["rows"]), entry["next_cursor"]
        while cursor:
            body = env.client.get(
                _page_url(ORG_A, name), params={"cursor": cursor, "limit": 1}, headers=headers
            ).json()
            rows += body["rows"]
            cursor = body["next_cursor"]
        assert entry["row_count"] == len(rows) == len({r["id"] for r in rows}), name
    assert index["complete"] is (all(t["next_cursor"] is None for t in index["tables"].values()))


def test_current_month_rows_are_exported_and_the_totals_match_the_ledger(big: PrivacyEnv):
    rows, _, _ = _walk(big, TABLE)
    month = datetime.now(UTC).strftime("%Y-%m")
    recent = [r for r in rows if r["created_at"].startswith(month)]
    assert {r["id"] for r in recent} >= {f"a-u-{i:05d}" for i in range(N_A - 40, N_A)}
    with big.witness.connect() as conn:
        stored = conn.execute(
            select(func.sum(LLMUsageEvent.cost_usd)).where(LLMUsageEvent.organization_id == ORG_A)
        ).scalar_one()
    assert sum(r["cost_usd"] for r in rows) == pytest.approx(stored)


def test_pages_hold_only_the_requesting_organizations_rows(big: PrivacyEnv):
    rows_b, _, total_b = _walk(big, TABLE, user=BOB, org=ORG_B)
    assert total_b == 3 and {r["id"] for r in rows_b} == {f"b-u-{i}" for i in range(3)}
    rows_a, _, _ = _walk(big, TABLE)
    assert {r["organization_id"] for r in rows_a} == {ORG_A}
    assert {r["workspace_id"] for r in rows_a} == {WS_A, WS_A2}


def test_a_cursor_carried_to_another_organization_only_positions_inside_its_rows(big: PrivacyEnv):
    first = big.client.get(INDEX, headers=big.auth(ALICE)).json()["tables"][TABLE]["next_cursor"]
    r = big.client.get(_page_url(ORG_B, TABLE), params={"cursor": first}, headers=big.auth(BOB))
    assert r.status_code == 200
    assert all(row["organization_id"] == ORG_B for row in r.json()["rows"])
    assert "a-u-" not in r.text


@pytest.mark.parametrize(
    ("user", "status", "message"),
    [
        (BOB, 403, "You are not a member of this organization."),
        (DAVE, 403, "Role 'admin' is not permitted for this action."),
        (CAROL, 403, "Role 'viewer' is not permitted for this action."),
    ],
)
def test_only_the_exact_owner_can_page_a_table(big: PrivacyEnv, user, status, message):
    r = big.client.get(_page_url(ORG_A, TABLE), headers=big.auth(user))
    assert (r.status_code, r.json()["error"]["message"]) == (status, message)
    assert "a-u-" not in r.text


def test_anonymous_is_401(env: PrivacyEnv):
    assert env.client.get(_page_url(ORG_A, TABLE)).status_code == 401


@pytest.mark.parametrize("limit", [5001, 100000, 0, -1])
def test_limit_outside_1_to_5000_is_refused(big: PrivacyEnv, limit):
    r = big.client.get(_page_url(ORG_A, TABLE), params={"limit": limit}, headers=big.auth(ALICE))
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    assert "a-u-" not in r.text


def test_limit_5000_is_accepted_and_the_default_is_1000(big: PrivacyEnv):
    h = big.auth(ALICE)
    assert (
        len(
            big.client.get(_page_url(ORG_A, TABLE), params={"limit": 5000}, headers=h).json()[
                "rows"
            ]
        )
        == 5000
    )
    body = big.client.get(_page_url(ORG_A, TABLE), headers=h).json()
    assert (len(body["rows"]), body["limit"]) == (1000, 1000)


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def test_a_tampered_or_foreign_cursor_is_refused(big: PrivacyEnv):
    h = big.auth(ALICE)
    good = big.client.get(INDEX, headers=h).json()["tables"][TABLE]["next_cursor"]
    brands = export_module.encode_cursor("brands", datetime.now(UTC), "x")
    bad = [
        "!!!not-base64!!!",
        "",
        "AAAA",
        good[:-3],
        good + "zz",
        brands,
        _b64("not json"),
        _b64('["llm_usage_events"]'),
        _b64('["llm_usage_events","2026-01-01T00:00:00",5]'),
        _b64('["llm_usage_events",7,"x"]'),
        _b64('["llm_usage_events","yesterday","x"]'),
        _b64('{"t":"llm_usage_events"}'),
        _b64("null"),
    ]
    for cursor in bad:
        r = big.client.get(_page_url(ORG_A, TABLE), params={"cursor": cursor}, headers=h)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor", (
            cursor,
            r.text,
        )
    # r11 MINOR-3: exact shape, control characters (NUL, Cc, Cf) and bounds are refused
    ts = "2026-01-01T00:00:00"
    hardened = [
        _b64(json.dumps([TABLE, ts, "x\u0000y"])),  # a NUL-bearing forged id
        _b64('["llm_usage_events","2026-01-01T00:00:00","x\\u0000y"]'),
        _b64(json.dumps([TABLE, ts, "a\u200bb"])),  # zero-width (Cf)
        _b64(json.dumps([TABLE, ts, "a\nb"])),  # control (Cc)
        _b64(json.dumps([TABLE, ts + "\u0000", "x"])),
        _b64(json.dumps([TABLE, ts + "\u202e", "x"])),  # bidi override (Cf)
        _b64(json.dumps([TABLE, ts, ""])),  # empty id
        _b64(json.dumps([TABLE, ts, "x" * 129])),  # id too long
        _b64(json.dumps([TABLE, "2026-01-01T00:00:00" + "0" * 40, "x"])),  # timestamp too long
        _b64(json.dumps({"a": TABLE, "b": ts, "c": "x"})),  # an object, not an array
        _b64(json.dumps([TABLE, ts, "x", "extra"])),  # four elements
        _b64(json.dumps([TABLE, ts])),  # two elements
        good.replace("-", "+").replace("_", "/") + "!",  # outside the URL-safe alphabet
    ]
    for cursor in hardened:
        r = big.client.get(_page_url(ORG_A, TABLE), params={"cursor": cursor}, headers=h)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor", (
            cursor,
            r.text,
        )
    # a well-formed forged position is accepted and stays inside the requester's rows
    forged = _b64(json.dumps([TABLE, ts, "no-such-id"]))
    r = big.client.get(_page_url(ORG_A, TABLE), params={"cursor": forged}, headers=h)
    assert r.status_code == 200 and {x["organization_id"] for x in r.json()["rows"]} == {ORG_A}
    # a cursor longer than the bound never reaches the decoder
    r = big.client.get(_page_url(ORG_A, TABLE), params={"cursor": "A" * 600}, headers=h)
    assert r.status_code == 422


def test_unknown_and_not_exported_tables_are_one_and_the_same_404(env: PrivacyEnv):
    h = env.auth(ALICE)
    seen = set()
    for name in (
        "users",
        "jobs",
        "job_events",
        "no_such_table",
        "LLM_USAGE_EVENTS",
        "sqlite_master",
    ):
        r = env.client.get(_page_url(ORG_A, name), headers=h)
        assert r.status_code == 404, (name, r.text)
        seen.add(json.dumps(r.json()["error"]["message"]))
    assert len(seen) == 1


def test_concurrent_changes_between_pages(big: PrivacyEnv):
    """Keyset behaviour, pinned: an insert after the cursor is included, an insert before it is
    not, a delete before its page is read is absent, served rows stay served, no duplicates."""
    h = big.auth(ALICE)
    first = big.client.get(_page_url(ORG_A, TABLE), params={"limit": 1000}, headers=h).json()
    served = [r["id"] for r in first["rows"]]
    last = first["rows"][-1]
    assert first["next_cursor"] is not None

    far_future = datetime.now(UTC) + timedelta(days=1)
    before_cursor = datetime.fromisoformat(first["rows"][0]["created_at"]) - timedelta(days=1)
    gone_later = "a-u-04000"  # sorts well after page 1 (distinct from the served ids)
    assert gone_later not in served
    with big.session() as s:
        s.execute(
            insert(LLMUsageEvent.__table__),
            [
                _usage("c-after", ORG_A, WS_A, far_future),  # sorts after every cursor
                _usage("c-before", ORG_A, WS_A, before_cursor),  # sorts before the cursor in use
                _usage("c-tie", ORG_A, WS_A, datetime.fromisoformat(last["created_at"]), cost=0.5),
            ],
        )
        s.execute(delete(LLMUsageEvent.__table__).where(LLMUsageEvent.id == gone_later))
        s.execute(
            delete(LLMUsageEvent.__table__).where(LLMUsageEvent.id == served[0])
        )  # already served
        s.commit()

    rows, cursor = [], first["next_cursor"]
    while cursor:
        body = big.client.get(
            _page_url(ORG_A, TABLE), params={"cursor": cursor, "limit": 1000}, headers=h
        ).json()
        rows += [r["id"] for r in body["rows"]]
        cursor = body["next_cursor"]

    everything = served + rows
    assert len(everything) == len(set(everything))  # never twice
    assert "c-after" in rows  # inserted after the cursor: included
    assert "c-before" not in everything  # sorted before the cursor: not seen
    assert gone_later not in everything  # deleted before its page was read: absent
    assert served[0] not in rows  # a row already served stays served (once) although deleted since
    # the tie row shares the last served created_at: included iff its id sorts after the cursor
    assert ("c-tie" in rows) is ("c-tie" > last["id"])
    expected_rest = {f"a-u-{i:05d}" for i in range(N_A)} - set(served) - {gone_later}
    assert expected_rest <= set(rows)
    # the index of a later call reflects the new state (row_count is exact, per call)
    again = big.client.get(INDEX, headers=h).json()
    assert again["tables"][TABLE]["row_count"] == N_A + 3 - 2


def test_the_page_query_seeks_the_workspace_created_index(big: PrivacyEnv):
    """EXPLAIN QUERY PLAN on SQLite (the PostgreSQL plan is read in CI): the tenant filter is
    seekable through ``ix_llm_usage_events_workspace_created`` rather than a table scan."""
    captured = []
    from sqlalchemy import event

    @event.listens_for(big.engine, "before_cursor_execute")
    def grab(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        if (
            statement.lstrip().startswith("SELECT")
            and "FROM llm_usage_events" in statement
            and "ORDER BY" in statement
        ):
            captured.append((statement, parameters))

    first = big.client.get(INDEX, headers=big.auth(ALICE)).json()["tables"][TABLE]["next_cursor"]
    big.client.get(_page_url(ORG_A, TABLE), params={"cursor": first}, headers=big.auth(ALICE))
    assert len(captured) >= 2
    with big.witness.connect() as conn:
        for statement, parameters in captured:
            plan = [
                r[3] for r in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
            ]
            assert any("ix_llm_usage_events_workspace_created" in p for p in plan), plan
            assert not any(p == "SCAN llm_usage_events" for p in plan), plan
