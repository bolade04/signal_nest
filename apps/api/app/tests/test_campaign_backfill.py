"""Project Phase 5D-W4 adversarial battery: the campaign-membership back-fill and its repair.

Lane 5D-W4 (W21) tests code it did not write, on SQLite and, when ``TEST_POSTGRES_URL`` is set,
on PostgreSQL, each case on a disposable database driven through the real ``alembic`` CLI:

* **CF-3's back-fill** (``c3f8e1a6d2b9``; exit criterion 1; plan §2 "Backfill fail-closed
  rule": "validates every id against the campaign's workspace, migrates only valid ids,
  **drops-and-reports** the rest in the migration output (never silently, never cross-tenant),
  and installs a workspace-consistency constraint"), on data the CF-3 module does not try:
  cross-workspace ids in both directions between organizations, near-miss ids (case, padding,
  empty), duplicates, and every JSON shape that is not a list of ids, plus text that is not JSON
  at all. It must be idempotent (physical rows unchanged on a second run), report every
  reference it does not migrate, and leave the composite keys and the vocabulary CHECKs in force.
* **The D-1 repair** (``d5f616c0ec97``; operator ruling D-1 (a), 2026-11-07), only the cases
  ``test_campaign_locations_repair.py`` lacks: a list changed after the back-fill to name
  another workspace's, another organization's, or another organization's location inside the
  same workspace; a membership swapped wholesale; and a second run after the 5D-W1 API, or a
  stale JSON-only writer, wrote in between.

Not in this module (a defect reported to the coordinator, reproduction in the lane's evidence
``defects/D-5DW4-03``): a membership row whose own ``organization_id`` names another
organization survives the repair as "S0 consistent".
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, StatementError

from app.tests._auth2_support import pg_database, require_postgres, sqlite_file_engine

API_DIR = Path(__file__).resolve().parents[2]
CF2, CF3, REPAIR = "a7d2e9f4c1b8", "c3f8e1a6d2b9", "d5f616c0ec97"
#: The repair cases upgrade to the repair itself, never to ``head``, as main's
#: ``test_campaign_locations_repair.py`` does: later revisions (W21 ADDR, D-N4 items 37 and 36)
#: follow the repair, so ``head`` is no longer the repair and ``downgrade -1`` from ``head``
#: would not undo it. From the repair, ``downgrade -1`` is CF-3.
VERSIONS = API_DIR / "alembic" / "versions"
BACKFILL_PREFIX = "CF-3 campaign_locations back-fill"
REPAIR_PREFIX = "5D-W1 campaign_locations repair"

#: Organization A has workspaces A1 and A2; organization B has B1.
WS = {"ws-a1": "org-a", "ws-a2": "org-a", "ws-b1": "org-b"}
#: location -> (workspace, organization, active). ``lx`` sits in A1 but names organization B
#: (no constraint ties a location's organization to its workspace's).
LOCS = {
    "la1": ("ws-a1", "org-a", True),
    "la1b": ("ws-a1", "org-a", True),
    "la1-off": ("ws-a1", "org-a", False),
    "la2": ("ws-a2", "org-a", True),
    "lb1": ("ws-b1", "org-b", True),
    "lx": ("ws-a1", "org-b", True),
}

ORGS = sa.table("organizations", sa.column("id"), sa.column("name"), sa.column("slug"))
WORKSPACES = sa.table(
    "workspaces",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("name"),
    sa.column("slug"),
    sa.column("onboarding_completed"),
)
BRANDS = sa.table(
    "brands",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("name"),
)
LOCATIONS = sa.table(
    "business_locations",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("brand_id"),
    sa.column("name"),
    sa.column("languages", sa.JSON),
    sa.column("local_social_links", sa.JSON),
    sa.column("local_competitors", sa.JSON),
    sa.column("overrides", sa.JSON),
    sa.column("is_active"),
)
CAMPAIGNS = sa.table(
    "campaigns",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("brand_id"),
    sa.column("name"),
    sa.column("mode"),
    sa.column("status"),
    sa.column("location_ids", sa.JSON),
)
MEMBERSHIP = sa.table(
    "campaign_locations",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("campaign_id"),
    sa.column("location_id"),
)


# --------------------------------------------------------------------------- #
# A disposable database of either engine, driven through the real alembic CLI
# --------------------------------------------------------------------------- #
@dataclass
class Db:
    url: str
    engine: Engine

    @property
    def pg(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["DATABASE_URL"] = self.url
        env.pop("SN_MIGRATION_MODE", None)
        return env

    def alembic(self, *args: str) -> subprocess.CompletedProcess:
        confirm: list[str] = []
        if args[0] == "downgrade":
            token = subprocess.run(
                [sys.executable, "-m", "app.db.migrate", "downgrade-confirmation", args[1]],
                cwd=API_DIR,
                env=self._env(),
                capture_output=True,
                text=True,
            )
            assert token.returncode == 0, token.stdout + token.stderr
            confirm = ["-x", f"confirm={token.stdout.strip()}"]
        return subprocess.run(
            [sys.executable, "-m", "alembic", *confirm, *args],
            cwd=API_DIR,
            env=self._env(),
            capture_output=True,
            text=True,
        )

    def ok(self, *args: str) -> str:
        result = self.alembic(*args)
        assert result.returncode == 0, (args, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def execute(self, statement, params=None) -> None:
        with self.engine.begin() as conn:
            if params is None:
                conn.execute(statement)
            else:
                conn.execute(statement, params)

    def version(self) -> list[str]:
        with self.engine.connect() as conn:
            return list(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars())

    def memberships(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        with self.engine.connect() as conn:
            for campaign, location in conn.execute(
                sa.select(MEMBERSHIP.c.campaign_id, MEMBERSHIP.c.location_id)
            ):
                out.setdefault(campaign, set()).add(location)
        return out

    def identity(self) -> dict[tuple[str, str], tuple]:
        statement = (
            "SELECT campaign_id, location_id, ctid::text, xmin::text, id, organization_id,"
            " workspace_id FROM campaign_locations"
            if self.pg
            else "SELECT campaign_id, location_id, rowid, id, organization_id, workspace_id"
            " FROM campaign_locations"
        )
        with self.engine.connect() as conn:
            return {(r[0], r[1]): tuple(r[2:]) for r in conn.execute(sa.text(statement))}

    def raw_json(self) -> dict[str, str]:
        with self.engine.connect() as conn:
            return dict(
                conn.execute(
                    sa.select(CAMPAIGNS.c.id, sa.cast(CAMPAIGNS.c.location_ids, sa.Text))
                ).all()
            )


@contextmanager
def _database(kind: str, directory: Path) -> Iterator[Db]:
    if kind == "sqlite":
        path = directory / "w4-backfill.db"
        engine = sqlite_file_engine(path)
        try:
            yield Db(f"sqlite:///{path}", engine)
        finally:
            engine.dispose()
        return
    require_postgres()
    with pg_database("sn_w4bf") as url:  # pragma: no cover - gated on PostgreSQL
        engine = create_engine(url, future=True)
        try:
            yield Db(url, engine)
        finally:
            engine.dispose()


def _tenants(db: Db) -> None:
    db.execute(ORGS.insert(), [{"id": o, "name": o, "slug": o} for o in ("org-a", "org-b")])
    db.execute(
        WORKSPACES.insert(),
        [
            {"id": ws, "organization_id": org, "name": ws, "slug": ws, "onboarding_completed": True}
            for ws, org in WS.items()
        ],
    )
    db.execute(
        BRANDS.insert(),
        [
            {"id": f"brand-{ws}", "organization_id": org, "workspace_id": ws, "name": "Brand"}
            for ws, org in WS.items()
        ],
    )
    db.execute(
        LOCATIONS.insert(),
        [
            {
                "id": loc,
                "organization_id": org,
                "workspace_id": ws,
                "brand_id": f"brand-{ws}",
                "name": "Shop",
                "languages": [],
                "local_social_links": {},
                "local_competitors": [],
                "overrides": {},
                "is_active": active,
            }
            for loc, (ws, org, active) in LOCS.items()
        ],
    )


def _campaigns(db: Db, seed: dict[str, tuple[str, object]]) -> None:
    db.execute(
        CAMPAIGNS.insert(),
        [
            {
                "id": cid,
                "organization_id": WS[ws],
                "workspace_id": ws,
                "brand_id": f"brand-{ws}",
                "name": "Campaign",
                "mode": "per_location",
                "status": "active",
                "location_ids": ids,
            }
            for cid, (ws, ids) in seed.items()
        ],
    )


def _set_json(db: Db, campaign_id: str, value) -> None:
    """A JSON-only write of a campaign's list, as the pre-5D-W1 generic factory made it (and
    as a direct write could make it, unvalidated)."""
    db.execute(CAMPAIGNS.update().where(CAMPAIGNS.c.id == campaign_id).values(location_ids=value))


def _module(revision: str):
    path = next(VERSIONS.glob(f"*-{revision}_*.py"))
    spec = importlib.util.spec_from_file_location(f"w4_rev_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lines(output: str, prefix: str, marker: str) -> list[str]:
    return [line for line in output.splitlines() if prefix in line and marker in line]


# --------------------------------------------------------------------------- #
# 1. CF-3's back-fill on adversarial data
# --------------------------------------------------------------------------- #
#: Each campaign's ``location_ids`` at CF-2 (workspace, value).
BACKFILL_SEED: dict[str, tuple[str, object]] = {
    "bf-cross-ws": ("ws-a1", ["la1", "la2"]),  # another workspace of the same organization
    "bf-cross-org": ("ws-a1", ["lb1", "la1b"]),  # another organization's location
    "bf-reverse": ("ws-b1", ["la1", "lb1"]),  # B's campaign naming A's location
    "bf-near-miss": ("ws-a1", ["gone", "LA1", " la1", "la1 ", "", "bf-cross-ws"]),
    "bf-dup": ("ws-a1", ["la1", "la1", "la1b", "la1"]),
    "bf-deactivated": ("ws-a1", ["la1-off"]),
    "bf-object": ("ws-a1", {"location_ids": ["la1"]}),
    "bf-string": ("ws-a1", "la1"),
    "bf-number": ("ws-a1", 7),
    "bf-nested": ("ws-a1", [["la1"]]),
    "bf-mixed": ("ws-a1", ["la1", 7]),
    "bf-null-element": ("ws-a1", ["la1", None]),
    "bf-empty": ("ws-a1", []),
}
MALFORMED = {"bf-object", "bf-string", "bf-number", "bf-nested", "bf-mixed", "bf-null-element"}
BACKFILLED = {
    "bf-cross-ws": {"la1"},
    "bf-cross-org": {"la1b"},
    "bf-reverse": {"lb1"},
    "bf-dup": {"la1", "la1b"},
    "bf-deactivated": {"la1-off"},
}
FOREIGN_DROPS = {
    ("bf-cross-ws", "ws-a1", "la2"),
    ("bf-cross-org", "ws-a1", "lb1"),
    ("bf-reverse", "ws-b1", "la1"),
}


@dataclass
class Backfilled:
    db: Db
    output: str
    before_json: dict[str, str]


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def backfilled(request, tmp_path_factory) -> Iterator[Backfilled]:
    with _database(request.param, tmp_path_factory.mktemp("w4-backfill")) as db:
        db.ok("upgrade", CF2)
        _tenants(db)
        _campaigns(db, BACKFILL_SEED)
        before = db.raw_json()
        output = db.ok("upgrade", CF3)
        yield Backfilled(db, output, before)


def test_only_own_workspace_ids_are_migrated(backfilled) -> None:
    assert backfilled.db.memberships() == BACKFILLED
    with backfilled.db.engine.connect() as conn:
        crossing = conn.execute(
            sa.text(
                "SELECT m.campaign_id, m.location_id FROM campaign_locations m"
                " JOIN campaigns c ON c.id = m.campaign_id"
                " JOIN business_locations l ON l.id = m.location_id"
                " WHERE m.workspace_id <> c.workspace_id OR l.workspace_id <> c.workspace_id"
                " OR m.organization_id <> c.organization_id"
            )
        ).all()
    assert crossing == []


_DROP_LINE = re.compile(
    r"dropped campaign=(?P<campaign>\S+) workspace=(?P<workspace>\S+) "
    r"location=(?P<location>.*) reason=(?P<reason>\w+)$"
)


def _dropped(output: str, prefix: str) -> list[tuple[str, str, str, str]]:
    found = []
    for line in output.splitlines():
        if prefix in line and (match := _DROP_LINE.search(line.rstrip("\r"))):
            found.append(
                (match["campaign"], match["workspace"], match["location"], match["reason"])
            )
    return found


def test_every_reference_not_migrated_is_reported_never_silently(backfilled) -> None:
    out = backfilled.output
    dropped = _dropped(out, BACKFILL_PREFIX)
    expected = {(c, ws, loc, "foreign_workspace") for c, ws, loc in FOREIGN_DROPS}
    expected |= {
        ("bf-near-miss", "ws-a1", loc, "missing")
        for loc in ("gone", "LA1", " la1", "la1 ", "", "bf-cross-ws")
    }
    expected |= {(c, "ws-a1", "None", "malformed") for c in MALFORMED}
    assert sorted(dropped) == sorted(expected)  # each exactly once, nothing else
    # Every element of every readable list is a membership now, or reported by name.
    members = backfilled.db.memberships()
    reported = {(c, loc) for c, _ws, loc, _reason in dropped}
    for campaign, (_ws, value) in BACKFILL_SEED.items():
        if campaign in MALFORMED:
            continue
        for element in value:
            assert element in members.get(campaign, set()) or (campaign, element) in reported, (
                campaign,
                element,
            )
    # No migrated membership is also reported as dropped.
    for campaign, locations in members.items():
        assert not locations & {loc for c, loc in reported if c == campaign}, campaign
    inserted = sum(len(v) for v in BACKFILLED.values())
    assert f"{inserted} membership(s) inserted, {len(expected)} reference(s) dropped" in out


def test_the_backfill_leaves_the_retained_json_untouched(backfilled) -> None:
    assert backfilled.db.raw_json() == backfilled.before_json


def test_a_second_backfill_changes_nothing_row_for_row(backfilled) -> None:
    db = backfilled.db
    before = db.identity()
    with db.engine.begin() as conn:
        inserted, dropped = _module(CF3).backfill_campaign_locations(conn)
    assert inserted == 0
    assert len(dropped) == len(FOREIGN_DROPS) + 6 + len(MALFORMED)  # still reported
    assert db.identity() == before  # the same physical rows (ctid/xmin or rowid), none added


def test_the_workspace_consistency_constraint_is_installed(backfilled) -> None:
    db = backfilled.db
    foreign_keys = {
        fk["name"]: fk for fk in inspect(db.engine).get_foreign_keys("campaign_locations")
    }
    assert foreign_keys["fk_campaign_locations_campaign_workspace"]["referred_columns"] == [
        "id",
        "workspace_id",
    ]
    assert foreign_keys["fk_campaign_locations_location_workspace"]["referred_columns"] == [
        "id",
        "workspace_id",
    ]
    for row_id, workspace, location in (
        ("w4-same-org", "ws-a1", "la2"),  # A1's campaign, A2's location
        ("w4-other-org", "ws-a1", "lb1"),  # A1's campaign, B1's location
        ("w4-moved", "ws-a2", "la2"),  # the row scoped to the location's workspace
    ):
        with pytest.raises(IntegrityError):
            db.execute(
                MEMBERSHIP.insert().values(
                    id=row_id,
                    organization_id="org-a",
                    workspace_id=workspace,
                    campaign_id="bf-cross-ws",
                    location_id=location,
                )
            )
    assert db.memberships() == BACKFILLED


@pytest.mark.parametrize(
    "column,bad",
    [("mode", "Grouped"), ("mode", " grouped"), ("status", "ACTIVE"), ("status", "live")],
)
def test_the_migrated_vocabulary_checks_refuse_invalid_values(backfilled, column, bad) -> None:
    with pytest.raises(IntegrityError):
        backfilled.db.execute(
            CAMPAIGNS.update().where(CAMPAIGNS.c.id == "bf-dup").values(**{column: bad})
        )


@pytest.mark.parametrize("kind", ["sqlite", "postgresql"])
def test_text_that_is_not_json_is_never_migrated_silently(kind, tmp_path) -> None:
    """A stored value that is not JSON at all. PostgreSQL's ``json`` refuses it on input, so the
    state cannot exist there. SQLite stores it: the upgrade must then either refuse loudly and
    leave the database at CF-2 untouched, or report the campaign; never insert silently."""
    with _database(kind, tmp_path) as db:
        db.ok("upgrade", CF2)
        _tenants(db)
        _campaigns(db, {"bf-text": ("ws-a1", [])})
        statement = sa.text("UPDATE campaigns SET location_ids = :raw WHERE id = 'bf-text'")
        if db.pg:
            with pytest.raises((DataError, StatementError)):  # pragma: no cover - PostgreSQL
                db.execute(statement, {"raw": "la1, la1b"})
            return
        db.execute(statement, {"raw": "la1, la1b"})
        result = db.alembic("upgrade", CF3)
        out = result.stdout + result.stderr
        if result.returncode != 0:
            assert db.version() == [CF2]
            assert "campaign_locations" not in inspect(db.engine).get_table_names()
        else:
            assert _lines(out, BACKFILL_PREFIX, "campaign=bf-text"), out
            assert "bf-text" not in db.memberships()


# --------------------------------------------------------------------------- #
# 2. The D-1 repair: the cases the 5D-W1 module lacks
# --------------------------------------------------------------------------- #
REPAIR_SEED: dict[str, tuple[str, object]] = {
    "r-swap": ("ws-a1", ["la1"]),
    "r-into-foreign": ("ws-a1", ["la1", "la1b"]),
    "r-org-in-workspace": ("ws-a1", ["la1"]),
    "r-steady": ("ws-a1", ["la1", "la1b"]),
    "r-tenant-b": ("ws-b1", ["lb1"]),
}
#: After the back-fill, the list is changed by JSON-only writes, unvalidated.
CHANGED_AFTER_BACKFILL = {
    "r-swap": ["la1b"],  # every row replaced
    "r-into-foreign": ["la2", "lb1", "la1b"],  # la1 removed; another workspace's, another org's
    "r-org-in-workspace": ["la1", "lx"],  # lx: in this workspace, another organization
}


@pytest.fixture(params=["sqlite", "postgresql"])
def changed(request, tmp_path) -> Iterator[tuple[Db, dict, str]]:
    with _database(request.param, tmp_path) as db:
        db.ok("upgrade", CF2)
        _tenants(db)
        _campaigns(db, REPAIR_SEED)
        db.ok("upgrade", CF3)
        for campaign, value in CHANGED_AFTER_BACKFILL.items():
            _set_json(db, campaign, value)
        before = db.identity()
        output = db.ok("upgrade", REPAIR)
        yield db, before, output


def test_a_list_changed_after_the_backfill_is_repaired_and_never_crosses_a_tenant(changed):
    db, before, out = changed
    assert db.version() == [REPAIR]
    assert db.memberships() == {
        "r-swap": {"la1b"},
        "r-into-foreign": {"la1b"},
        "r-org-in-workspace": {"la1"},
        "r-steady": {"la1", "la1b"},
        "r-tenant-b": {"lb1"},
    }
    for campaign, location, reason in (
        ("r-into-foreign", "la2", "foreign_workspace"),
        ("r-into-foreign", "lb1", "foreign_organization"),
        ("r-org-in-workspace", "lx", "foreign_organization"),
    ):
        line = f"dropped campaign={campaign} workspace=ws-a1 location={location} reason={reason}"
        assert len(_lines(out, REPAIR_PREFIX, line)) == 1, line
    after = db.identity()
    # Kept rows are the same physical rows; the removed ones are gone; nothing foreign added.
    for pair in (
        ("r-into-foreign", "la1b"),
        ("r-org-in-workspace", "la1"),
        ("r-steady", "la1"),
        ("r-steady", "la1b"),
        ("r-tenant-b", "lb1"),
    ):
        assert after[pair] == before[pair], pair
    assert set(before) - set(after) == {("r-swap", "la1"), ("r-into-foreign", "la1")}
    assert set(after) - set(before) == {("r-swap", "la1b")}
    assert json.loads(db.raw_json()["r-into-foreign"]) == ["la1b"]
    assert json.loads(db.raw_json()["r-org-in-workspace"]) == ["la1"]
    with db.engine.connect() as conn:
        crossing = conn.execute(
            sa.text(
                "SELECT m.campaign_id FROM campaign_locations m"
                " JOIN campaigns c ON c.id = m.campaign_id"
                " JOIN business_locations l ON l.id = m.location_id"
                " WHERE l.workspace_id <> c.workspace_id OR l.organization_id <> c.organization_id"
                " OR m.organization_id <> c.organization_id"
            )
        ).all()
    assert crossing == []
    # The revisions after the repair apply on top of it and leave its rows as they are.
    db.ok("upgrade", "head")
    assert len(db.version()) == 1 and db.version() != [REPAIR]
    assert db.identity() == after


def test_a_second_run_after_an_api_write_changes_nothing(changed):
    """Between the runs the 5D-W1 API reconciles a campaign (its own write: the row and the
    sorted mirror together). The second run (downgrade to CF-3, upgrade again) changes nothing."""
    db, _before, _out = changed
    db.execute(
        MEMBERSHIP.insert().values(
            id="w4-api-row",
            organization_id="org-a",
            workspace_id="ws-a1",
            campaign_id="r-swap",
            location_id="la1",
        )
    )
    _set_json(db, "r-swap", ["la1", "la1b"])
    settled = (db.identity(), db.raw_json())
    db.ok("downgrade", "-1")
    again = db.ok("upgrade", REPAIR)
    assert db.version() == [REPAIR]
    assert (db.identity(), db.raw_json()) == settled
    assert (
        "0 membership(s) inserted, 0 deleted, 0 mirror(s) rewritten, 0 reference(s) dropped"
    ) in again, again


def test_a_second_run_after_a_stale_json_write_repairs_again(changed):
    """A writer still on the pre-5D-W1 path changes only the JSON after the first run: the
    second run reconciles it exactly as the first would have, and touches nothing else."""
    db, _before, _out = changed
    settled = db.identity()
    _set_json(db, "r-steady", ["la1b", "lb1"])  # la1 removed; another organization's added
    db.ok("downgrade", "-1")
    again = db.ok("upgrade", REPAIR)
    assert db.version() == [REPAIR]
    assert db.memberships()["r-steady"] == {"la1b"}
    assert json.loads(db.raw_json()["r-steady"]) == ["la1b"]
    after = db.identity()
    assert set(settled) - set(after) == {("r-steady", "la1")}
    for pair in set(after):
        assert after[pair] == settled[pair], pair
    assert (
        "dropped campaign=r-steady workspace=ws-a1 location=lb1 reason=foreign_organization"
        in again
    )
    assert "0 membership(s) inserted, 1 deleted, 1 mirror(s) rewritten" in again


@pytest.mark.parametrize("kind", ["sqlite", "postgresql"])
def test_a_campaign_naming_another_organization_than_its_workspaces_never_gains_its_rows(
    kind, tmp_path
) -> None:
    """Another organization's data in the same workspace, from the campaign's side: a campaign
    row of organization B sitting in A's workspace (no constraint ties a campaign's
    organization to its workspace's). CF-3 back-filled A's location into it; the repair drops
    that location as another organization's and deletes its row."""
    with _database(kind, tmp_path) as db:
        db.ok("upgrade", CF2)
        _tenants(db)
        db.execute(
            CAMPAIGNS.insert().values(
                id="r-b-in-a1",
                organization_id="org-b",
                workspace_id="ws-a1",
                brand_id="brand-ws-a1",
                name="Campaign",
                mode="per_location",
                status="active",
                location_ids=["la1", "la1b"],
            )
        )
        db.ok("upgrade", CF3)
        assert db.memberships()["r-b-in-a1"] == {"la1", "la1b"}  # CF-3 checks workspaces only
        out = db.ok("upgrade", REPAIR)
        assert "r-b-in-a1" not in db.memberships()
        for location in ("la1", "la1b"):
            assert (
                f"dropped campaign=r-b-in-a1 workspace=ws-a1 location={location} "
                "reason=foreign_organization"
            ) in out
