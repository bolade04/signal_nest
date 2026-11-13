"""Project Phase 5 CF-3 (W21): the CF-3 migration on PostgreSQL.

``test_cf3_contract_migration`` proves the revision on SQLite; this module proves the parts a
dialect can change, on a PostgreSQL database of its own (created and dropped here):

* the **O-8 back-fill** reads the JSON arrays and migrates only own-workspace ids, dropping and
  reporting the rest; the campaign vocabularies are normalised before the CHECKs;
* the **workspace-consistency keys** (composite foreign keys onto unique indexes) refuse a
  cross-workspace membership and cascade;
* the **jurisdiction CHECKs** refuse the ``UNRESOLVED`` sentinel and lower case, and the export
  row's refuses NULL;
* the **downgrade refuses before any DDL** while exports hold a row or a claim is
  jurisdiction-restricted (the JSON emptiness test is dialect-sensitive), then one step down
  and back up is clean and ``alembic check`` reports no drift.

Gate: :func:`require_postgres` — with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI and
skip only on a developer machine.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.exc import DataError, IntegrityError

from app.tests._auth2_support import pg_database, require_postgres
from app.tests.test_postgres_migration_proof import (
    _alembic,
    _columns,
    _confirmation,
    _run,
    _tables,
    _version,
)

#: This revision (W21 CF-3).
CF3 = "c3f8e1a6d2b9"
#: 5D-W1's campaign-location data repair (Amendment D-1, data only), which follows CF-3.
D1_REPAIR = "d5f616c0ec97"
#: W21 ADDR: the structured business address on ``geo_coverage_rules``, which follows the repair.
ADDR = "6f8a80cf1785"
#: D-N4 item 37's PostgreSQL append-only backstop, which follows ADDR.
BACKSTOP = "d0a8bce8de6a"
#: The code head (D-N4 item 36's closed claim kinds, data only), which follows the backstop.
HEAD = "1574637e56d3"
CF2 = "a7d2e9f4c1b8"
NEW_TABLES = {"campaign_locations", "exports", "notifications", "notification_preferences"}


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def pg_url() -> Iterator[str]:
    with pg_database("sn_cf3") as url:
        yield url


def _t(name: str, *columns: str) -> sa.TableClause:
    return sa.table(name, *(sa.column(c) for c in columns))


def _insert(url: str, table: sa.TableClause, rows: list[dict]) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(table.insert(), rows)
    finally:
        engine.dispose()


def _select(url: str, query) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(query)]
    finally:
        engine.dispose()


ORGS = _t("organizations", "id", "name", "slug")
WORKSPACES = _t("workspaces", "id", "organization_id", "name", "slug", "onboarding_completed")
BRANDS = _t("brands", "id", "organization_id", "workspace_id", "name")
LOCATIONS = sa.table(
    "business_locations",
    *(sa.column(c) for c in ("id", "organization_id", "workspace_id", "brand_id", "name",
                             "is_active")),
    *(sa.column(c, sa.JSON) for c in ("languages", "local_social_links", "local_competitors",
                                      "overrides")),
)
CAMPAIGNS = sa.table(
    "campaigns", sa.column("id"), sa.column("organization_id"), sa.column("workspace_id"),
    sa.column("brand_id"), sa.column("name"), sa.column("mode"), sa.column("status"),
    sa.column("location_ids", sa.JSON),
)
MEMBERSHIP = _t(
    "campaign_locations", "id", "organization_id", "workspace_id", "campaign_id", "location_id"
)
EXPORTS = _t(
    "exports", "id", "organization_id", "workspace_id", "draft_version", "format",
    "jurisdiction_code", "exported_by_snapshot", "is_simulated",
)
CLAIMS = sa.table(
    "claims_library", sa.column("id"), sa.column("organization_id"), sa.column("workspace_id"),
    sa.column("brand_id"), sa.column("text"), sa.column("kind"), sa.column("risk_level"),
    sa.column("jurisdiction_codes", sa.JSON),
)


def _world(url: str) -> None:
    _insert(url, ORGS, [{"id": o, "name": o, "slug": o} for o in ("org-a", "org-b")])
    spaces = (("ws-a1", "org-a"), ("ws-a2", "org-a"), ("ws-b1", "org-b"))
    _insert(url, WORKSPACES, [
        {"id": w, "organization_id": o, "name": w, "slug": w, "onboarding_completed": True}
        for w, o in spaces
    ])
    _insert(url, BRANDS, [
        {"id": f"brand-{w}", "organization_id": o, "workspace_id": w, "name": "Brand"}
        for w, o in spaces
    ])
    _insert(url, LOCATIONS, [
        {"id": loc, "organization_id": o, "workspace_id": w, "brand_id": f"brand-{w}",
         "name": "Shop", "languages": [], "local_social_links": {},
         "local_competitors": [], "overrides": {}, "is_active": active}
        for loc, w, o, active in (
            ("loc-a1", "ws-a1", "org-a", True), ("loc-a1-old", "ws-a1", "org-a", False),
            ("loc-a2", "ws-a2", "org-a", True), ("loc-b1", "ws-b1", "org-b", True),
        )
    ])


def _campaign(cid: str, ws: str, org: str, location_ids, mode: str, status: str) -> dict:
    return {
        "id": cid, "organization_id": org, "workspace_id": ws, "brand_id": f"brand-{ws}",
        "name": "Campaign", "mode": mode, "status": status, "location_ids": location_ids,
    }


def test_the_backfill_and_normalisation_on_postgresql(pg_url: str) -> None:
    _alembic(pg_url, "upgrade", CF2)
    _world(pg_url)
    _insert(pg_url, CAMPAIGNS, [
        _campaign("camp-mixed", "ws-a1", "org-a",
                  ["loc-a1", "loc-a1-old", "loc-a1", "loc-a2", "loc-b1", "loc-gone"],
                  "Per-Location", " Active "),
        _campaign("camp-malformed", "ws-a1", "org-a", {"x": 1}, "SAME FOR ALL", "running"),
        _campaign("camp-b", "ws-b1", "org-b", ["loc-b1"], "grouped", "draft"),
    ])

    up = _run(["upgrade", CF3], pg_url)

    assert up.returncode == 0, up.stdout + up.stderr
    out = up.stdout + up.stderr
    assert "3 membership(s) inserted, 4 reference(s) dropped" in out, out
    assert "location=loc-b1 reason=foreign_workspace" in out
    assert "location=None reason=malformed" in out
    assert "campaign status: campaign=camp-malformed stored='running' rewritten_to=paused" in out
    pairs = set(_select(pg_url, sa.select(MEMBERSHIP.c.campaign_id, MEMBERSHIP.c.location_id)))
    assert pairs == {
        ("camp-mixed", "loc-a1"), ("camp-mixed", "loc-a1-old"), ("camp-b", "loc-b1")
    }
    stored = dict(
        (r[0], (r[1], r[2]))
        for r in _select(pg_url, sa.select(CAMPAIGNS.c.id, CAMPAIGNS.c.mode, CAMPAIGNS.c.status))
    )
    assert stored == {
        "camp-mixed": ("per_location", "active"),
        "camp-malformed": ("same_for_all", "paused"),
        "camp-b": ("grouped", "draft"),
    }
    _alembic(pg_url, "upgrade", "head")  # check needs the head
    _alembic(pg_url, "check")


def test_the_constraints_hold_on_postgresql(pg_url: str) -> None:
    _alembic(pg_url, "upgrade", "head")
    _world(pg_url)
    _insert(pg_url, CAMPAIGNS, [_campaign("camp-a1", "ws-a1", "org-a", [], "grouped", "draft")])
    member = {"organization_id": "org-a", "workspace_id": "ws-a1", "campaign_id": "camp-a1"}
    _insert(pg_url, MEMBERSHIP, [{"id": "m-ok", **member, "location_id": "loc-a1"}])
    for row in (
        {"id": "m-other-ws", **member, "location_id": "loc-a2"},
        {"id": "m-other-org", **member, "location_id": "loc-b1"},
        {"id": "m-row-ws", **member, "workspace_id": "ws-a2", "location_id": "loc-a2"},
        {"id": "m-dup", **member, "location_id": "loc-a1"},
    ):
        with pytest.raises(IntegrityError):
            _insert(pg_url, MEMBERSHIP, [row])
    for bad in (
        _campaign("c-mode", "ws-a1", "org-a", [], "Grouped", "draft"),
        _campaign("c-status", "ws-a1", "org-a", [], "grouped", "live"),
    ):
        with pytest.raises(IntegrityError):
            _insert(pg_url, CAMPAIGNS, [bad])
    export = {"organization_id": "o", "workspace_id": "w", "draft_version": 1,
              "format": "text/plain", "exported_by_snapshot": "s", "is_simulated": False}
    _insert(pg_url, EXPORTS, [{"id": "e-ok", **export, "jurisdiction_code": "NG-LA"}])
    # The 10-character sentinel already exceeds VARCHAR(6) on PostgreSQL, so the length bound
    # refuses it (DataError) before the CHECK is evaluated; SQLite, which does not enforce
    # VARCHAR lengths, refuses it by the CHECK (test_cf3_contract_migration). Either way the
    # database never stores it.
    with pytest.raises(DataError):
        sentinel = {"id": "e-sentinel", **export, "jurisdiction_code": "UNRESOLVED"}
        _insert(pg_url, EXPORTS, [sentinel])
    for n, code in enumerate(("us-tx", "USA", "US_TX", None)):
        with pytest.raises(IntegrityError):  # the shape CHECK, or NOT NULL
            _insert(pg_url, EXPORTS, [{"id": f"e-bad-{n}", **export, "jurisdiction_code": code}])
    # Deleting the campaign removes its membership (the composite key cascades).
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(CAMPAIGNS.delete().where(CAMPAIGNS.c.id == "camp-a1"))
    finally:
        engine.dispose()
    assert _select(pg_url, sa.select(MEMBERSHIP.c.id)) == []


def test_the_downgrade_guards_and_round_trip_on_postgresql(pg_url: str) -> None:
    _alembic(pg_url, "upgrade", CF3)  # this revision's own downgrade is one step down
    _world(pg_url)
    claim = {"organization_id": "org-a", "workspace_id": "ws-a1", "brand_id": "brand-ws-a1",
             "text": "c", "kind": "approved", "risk_level": "low"}
    _insert(pg_url, CLAIMS, [
        {"id": "all-null", **claim, "jurisdiction_codes": None},
        {"id": "all-empty", **claim, "jurisdiction_codes": []},
        {"id": "texas", **claim, "jurisdiction_codes": ["US-TX"]},
    ])
    _insert(pg_url, EXPORTS, [{
        "id": "e-1", "organization_id": "o", "workspace_id": "w", "draft_version": 1,
        "format": "json", "jurisdiction_code": "GB", "exported_by_snapshot": "s",
        "is_simulated": False,
    }])

    def refused(message: str) -> None:
        token = _confirmation(pg_url, "-1")
        result = _run(["-x", f"confirm={token}", "downgrade", "-1"], pg_url)
        assert result.returncode != 0
        assert message in result.stdout + result.stderr, result.stdout + result.stderr
        assert _version(pg_url) == [CF3] and NEW_TABLES <= _tables(pg_url)

    refused("export record(s) exist")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(EXPORTS.delete())
    finally:
        engine.dispose()
    refused("1 claims-library entr(y/ies) are jurisdiction-restricted")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(CLAIMS.delete().where(CLAIMS.c.id == "texas"))
    finally:
        engine.dispose()

    token = _confirmation(pg_url, "-1")
    _alembic(pg_url, "-x", f"confirm={token}", "downgrade", "-1")
    assert _version(pg_url) == [CF2]
    assert not NEW_TABLES & _tables(pg_url)
    assert "jurisdiction_code" not in _columns(pg_url, "business_locations")
    assert "jurisdiction_codes" not in _columns(pg_url, "claims_library")
    assert {r[0] for r in _select(pg_url, sa.select(CLAIMS.c.id))} == {"all-null", "all-empty"}
    _alembic(pg_url, "upgrade", "head")
    assert _version(pg_url) == [HEAD]
    _alembic(pg_url, "check")
