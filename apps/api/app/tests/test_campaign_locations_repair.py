"""Project Phase 5D, lane 5D-W1, Amendment D-1: the campaign-location data repair ``d5f616c0ec97``.

The revision (``alembic/versions/20261107_2300-d5f616c0ec97_reconcile_campaign_locations.py``)
makes every campaign's ``campaign_locations`` rows and ``location_ids`` mirror agree with its
validated ``location_ids`` (B-6, integration item I-CF3-2). Every case runs on SQLite and, when
``TEST_POSTGRES_URL`` is set, on PostgreSQL 16 (unset FAILS in CI and skips only on a developer
machine), on a disposable database of its own, through the real ``alembic`` CLI and ``env.py``.

* **The reported case, reproduced end to end**: a database at CF-2 (``a7d2e9f4c1b8``) with a
  campaign; CF-3 (``c3f8e1a6d2b9``) back-fills its membership; the campaign is then edited the way
  the pre-5D-W1 API did it (the generic factory's PATCH at ``c7c4d277``,
  ``app/campaign_context/routes.py:253-276``: ``validate_location_ids`` then
  ``setattr(obj, "location_ids", ...)``, a write of the JSON alone). Without the repair the
  5D-W1 API answers the back-filled membership and the next membership PATCH loses the edit;
  after it, the API answers the intended membership and a PATCH keeps it.
* **Every state S0-S8** of the revision's docstring has its own case, with the report's counts.
* **Tenant isolation**: ids of another workspace (same organization, another organization) and
  a location of the campaign's workspace that belongs to another organization are dropped and
  reported; no membership crosses a tenant afterwards. The composite key
  ``fk_campaign_locations_location_workspace`` refuses a cross-workspace row by itself; nothing
  in the schema refuses the organization mismatch inside one workspace (CF-3's back-fill
  inserted it), so the repair's own check is what removes it.
* **Cross-tenant rows (S8, D-5DW4-03)**: a membership row whose own ``organization_id`` names
  another organization than its campaign's, written straight to the database (no API path
  writes one), is deleted and reported even when its (campaign, location) pair is valid, and a
  correct row of a new id takes its place where the membership is kept, on a readable campaign
  and on an unreadable (S6) one. A row whose own or whose location's *workspace* is not the
  campaign's is the same state; only SQLite with foreign keys off can hold it.
* **Preservation**: every membership the repair keeps is the same physical row afterwards
  (PostgreSQL ``ctid`` and ``xmin``; SQLite ``rowid``; and ``id`` / ``created_at`` on both).
* **Transactional failure**: a failure injected after the deletes and inserts have run (a
  trigger of the test database refuses the first mirror rewrite) leaves memberships, JSON and
  ``alembic_version`` exactly as they were; a clean re-run then completes.
* **The lock** (PostgreSQL): the repair's first statement locks ``campaigns`` and
  ``campaign_locations`` in SHARE ROW EXCLUSIVE mode; while its transaction is open, writers of
  either table wait (on rows it never touched) and readers proceed.
* **Safe repeat**: a second run changes nothing; upgrade, the no-op downgrade, upgrade changes
  nothing; an empty database goes from base to head with no report at all.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.auth import service as auth_service
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import SessionLocal, get_db
from app.main import app
from app.organizations.models import OrganizationMember, User
from app.tests._auth2_support import (
    active_rate_limiter,
    pg_database,
    require_postgres,
    sqlite_file_engine,
)

API_DIR = Path(__file__).resolve().parents[2]
REPAIR = "d5f616c0ec97"
CF3 = "c3f8e1a6d2b9"
CF2 = "a7d2e9f4c1b8"
MIGRATION = next((API_DIR / "alembic" / "versions").glob(f"*-{REPAIR}_*.py"))
PREFIX = "5D-W1 campaign_locations repair"
API = get_settings().api_prefix

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
    sa.column("updated_at"),
)
MEMBERSHIP = sa.table(
    "campaign_locations",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("campaign_id"),
    sa.column("location_id"),
)

#: Tenants: organization A with workspaces A1 and A2, organization B with B1.
WS = {"ws-a1": "org-a", "ws-a2": "org-a", "ws-b1": "org-b"}
#: location -> (workspace, organization, active). ``lx`` sits in A1 but names organization B:
#: an inconsistent row no constraint refuses (the location table has no composite key).
LOCS = {
    "la1": ("ws-a1", "org-a", True),
    "la1b": ("ws-a1", "org-a", True),
    "la1c": ("ws-a1", "org-a", True),
    "la1-off": ("ws-a1", "org-a", False),
    "la2": ("ws-a2", "org-a", True),
    "lb1": ("ws-b1", "org-b", True),
    "lx": ("ws-a1", "org-b", True),
}
#: Each campaign: (workspace, its location_ids at CF-2, before the CF-3 back-fill).
CAMPAIGN_SEED = {
    "s0-consistent": ("ws-a1", ["la1", "la1b"]),
    "s0-empty": ("ws-a1", []),
    "s1-added": ("ws-a1", ["la1"]),
    "s2-removed": ("ws-a1", ["la1", "la1b"]),
    "s2-removed-all": ("ws-a1", ["la1"]),
    "s3-foreign": ("ws-a1", ["la1", "la2", "lb1", "lx"]),
    "s4-missing": ("ws-a1", ["la1", "gone"]),
    "s4-invalid": ("ws-a1", ["la1", 7, "", None]),
    "s5-duplicate": ("ws-a1", ["la1b", "la1", "la1b"]),
    "s6-json-null": ("ws-a1", ["la1"]),
    "s6-object": ("ws-a1", ["la1"]),
    "s6-string": ("ws-a1", ["la1"]),
    "s6-unparseable": ("ws-a1", ["la1"]),  # SQLite only (PostgreSQL's json refuses the text)
    "s6-cross-tenant": ("ws-a1", ["la1", "la1b", "lx"]),  # the back-fill keeps lx (S3's case)
    "s7-deactivated": ("ws-a1", ["la1", "la1-off"]),
    "s8-row-organization": ("ws-a1", ["la1", "la1b"]),
    "s8-row-outside": ("ws-a1", ["la1"]),
    "unsorted": ("ws-a1", ["la1b", "la1"]),
    "zb-other-tenant": ("ws-b1", ["lb1"]),
}
#: After the back-fill: the API edits as the generic factory's PATCH made them (JSON only).
FACTORY_EDITS = {"s1-added": ["la1", "la1b"], "s2-removed": ["la1"], "s2-removed-all": []}
#: After the back-fill: S6 values, JSON (a JSON ``null`` included) or raw text. SQL NULL cannot
#: be stored: the column is NOT NULL (``test_the_schema_refuses_sql_null``).
S6_VALUES = {
    "s6-json-null": ("json", None),
    "s6-object": ("json", {"location_ids": ["la1"]}),
    "s6-string": ("json", "la1"),
    "s6-unparseable": ("text", "la1, la1b"),
    "s6-cross-tenant": ("json", {"ids": ["la1", "la1b", "lx"]}),
}
#: After the back-fill: membership rows naming another organization than their campaign's
#: (D-5DW4-03), written straight to the database; no API path writes one. (campaign, location)
#: -> the row's organization; an existing row is relabelled, a missing one inserted.
CROSS_TENANT_ROWS = {
    ("s8-row-organization", "la1"): "org-b",  # in the JSON: replaced by a correct row
    # Not in the JSON: deleted, not replaced. This campaign has no other fault, so only the
    # cross-tenant row keeps it out of S0 (the defect reported it as consistent).
    ("s8-row-outside", "la1c"): "org-b",
    ("s6-cross-tenant", "la1b"): "org-b",  # S6: replaced, as its memberships stay
}
#: The pairs whose cross-tenant row is replaced by a correct row of a new id.
REPLACED = {("s8-row-organization", "la1"), ("s6-cross-tenant", "la1b")}

#: The documented outcome: campaign -> (memberships after, JSON after as a list or "unchanged").
EXPECTED = {
    "s0-consistent": ({"la1", "la1b"}, ["la1", "la1b"]),
    "s0-empty": (set(), []),
    "s1-added": ({"la1", "la1b"}, ["la1", "la1b"]),
    "s2-removed": ({"la1"}, ["la1"]),
    "s2-removed-all": (set(), []),
    "s3-foreign": ({"la1"}, ["la1"]),
    "s4-missing": ({"la1"}, ["la1"]),
    "s4-invalid": ({"la1"}, ["la1"]),
    "s5-duplicate": ({"la1", "la1b"}, ["la1", "la1b"]),
    "s6-json-null": ({"la1"}, "unchanged"),
    "s6-object": ({"la1"}, "unchanged"),
    "s6-string": ({"la1"}, "unchanged"),
    "s6-unparseable": ({"la1"}, "unchanged"),
    "s6-cross-tenant": ({"la1", "la1b"}, "unchanged"),
    "s7-deactivated": ({"la1", "la1-off"}, ["la1", "la1-off"]),
    "s8-row-organization": ({"la1", "la1b"}, ["la1", "la1b"]),
    "s8-row-outside": ({"la1"}, ["la1"]),
    "unsorted": ({"la1", "la1b"}, ["la1", "la1b"]),
    "zb-other-tenant": ({"lb1"}, ["lb1"]),
}


# --------------------------------------------------------------------------- #
# A disposable database, either engine, driven through the real alembic CLI
# --------------------------------------------------------------------------- #
@dataclass
class Db:
    url: str
    engine: Engine  # the witness: foreign keys enforced on SQLite, as the app runs it

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
            conn.execute(statement, params) if params is not None else conn.execute(statement)

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
        """(campaign, location) -> the row's physical and logical identity, ending with
        ``(id, created_at, organization_id, workspace_id)`` on both engines."""
        statement = (
            "SELECT campaign_id, location_id, ctid::text, xmin::text, id, created_at,"
            " organization_id, workspace_id FROM campaign_locations"
            if self.pg
            else "SELECT campaign_id, location_id, rowid, id, created_at, organization_id,"
            " workspace_id FROM campaign_locations"
        )
        with self.engine.connect() as conn:
            return {(r[0], r[1]): tuple(r[2:]) for r in conn.execute(sa.text(statement))}

    def raw_json(self) -> dict[str, str | None]:
        """Each campaign's stored ``location_ids`` as text, exactly."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                sa.select(CAMPAIGNS.c.id, sa.cast(CAMPAIGNS.c.location_ids, sa.Text))
            )
            return dict(rows.all())

    def snapshot(self) -> tuple:
        return (self.identity(), self.raw_json(), self.version())


@contextmanager
def _database(kind: str, directory: Path) -> Iterator[Db]:
    if kind == "sqlite":
        path = directory / "repair.db"
        engine = sqlite_file_engine(path)
        try:
            yield Db(f"sqlite:///{path}", engine)
        finally:
            engine.dispose()
        return
    require_postgres()
    with pg_database("sn_rep") as url:  # pragma: no cover - gated on PostgreSQL
        engine = create_engine(url, future=True)
        try:
            yield Db(url, engine)
        finally:
            engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path) -> Iterator[Db]:
    with _database(request.param, tmp_path) as database:
        yield database


def _json(value) -> list | None:
    return None if value is None else json.loads(value)


# --------------------------------------------------------------------------- #
# Building the states: CF-2 rows, the CF-3 back-fill, then the factory's JSON-only edits
# --------------------------------------------------------------------------- #
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


def _campaigns(db: Db, seed: dict[str, tuple[str, list]]) -> None:
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


def _factory_patch(db: Db, campaign_id: str, location_ids: list[str]) -> None:
    """What the generic factory's PATCH at ``c7c4d277`` wrote for a campaign's list
    (``campaign_context/routes.py:253-276``): the JSON and ``updated_at``; never the join table."""
    db.execute(
        CAMPAIGNS.update()
        .where(CAMPAIGNS.c.id == campaign_id)
        .values(location_ids=location_ids, updated_at=sa.func.now())
    )


def _set_s6(db: Db, campaign_id: str, kind: str, value) -> None:
    if kind == "sql":
        db.execute(
            CAMPAIGNS.update().where(CAMPAIGNS.c.id == campaign_id).values(location_ids=sa.null())
        )
    elif kind == "json":
        db.execute(
            CAMPAIGNS.update().where(CAMPAIGNS.c.id == campaign_id).values(location_ids=value)
        )
    else:
        db.execute(
            sa.text("UPDATE campaigns SET location_ids = :raw WHERE id = :id"),
            {"raw": value, "id": campaign_id},
        )


def _cross_tenant_rows(db: Db) -> None:
    """The direct database writes of ``CROSS_TENANT_ROWS`` (D-5DW4-03)."""
    present = db.memberships()
    for (campaign_id, location_id), organization_id in CROSS_TENANT_ROWS.items():
        if location_id in present.get(campaign_id, set()):
            db.execute(
                MEMBERSHIP.update()
                .where(
                    MEMBERSHIP.c.campaign_id == campaign_id,
                    MEMBERSHIP.c.location_id == location_id,
                )
                .values(organization_id=organization_id)
            )
        else:
            db.execute(
                MEMBERSHIP.insert().values(
                    id=f"x-{campaign_id}-{location_id}",
                    organization_id=organization_id,
                    workspace_id=CAMPAIGN_SEED[campaign_id][0],
                    campaign_id=campaign_id,
                    location_id=location_id,
                )
            )


def _crossing(db: Db) -> list[tuple[str, str]]:
    """Every membership that crosses its campaign's tenant (W1's isolation predicate)."""
    with db.engine.connect() as conn:
        return sorted(
            tuple(row)
            for row in conn.execute(
                sa.text(
                    "SELECT m.campaign_id, m.location_id FROM campaign_locations m"
                    " JOIN campaigns c ON c.id = m.campaign_id"
                    " JOIN business_locations l ON l.id = m.location_id"
                    " WHERE m.workspace_id <> c.workspace_id"
                    " OR m.organization_id <> c.organization_id"
                    " OR l.workspace_id <> c.workspace_id OR l.organization_id <> c.organization_id"
                )
            )
        )


def _state_world(db: Db) -> list[str]:
    """Every state S0-S8 at CF-3, as the pre-5D-W1 API (and, for S8, a direct database write)
    could have left it. Returns the ids."""
    db.ok("upgrade", CF2)
    _tenants(db)
    seed = dict(CAMPAIGN_SEED)
    if db.pg:
        seed.pop("s6-unparseable")
    _campaigns(db, seed)
    db.ok("upgrade", CF3)
    for campaign_id, ids in FACTORY_EDITS.items():
        _factory_patch(db, campaign_id, ids)
    for campaign_id, (kind, value) in S6_VALUES.items():
        if campaign_id in seed:
            _set_s6(db, campaign_id, kind, value)
    _cross_tenant_rows(db)
    return sorted(seed)


@dataclass
class Repaired:
    db: Db
    campaigns: list[str]
    before: tuple
    after: tuple
    output: str
    crossing_before: list[tuple[str, str]]


def _repair(db: Db) -> Repaired:
    campaigns = _state_world(db)
    before, crossing = db.snapshot(), _crossing(db)
    output = db.ok("upgrade", "head")
    return Repaired(db, campaigns, before, db.snapshot(), output, crossing)


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def shared(request, tmp_path_factory) -> Iterator[Repaired]:
    """The state world repaired once per engine, for the cases that only read it."""
    with _database(request.param, tmp_path_factory.mktemp("repair-shared")) as database:
        result = _repair(database)
        yield result
        assert database.snapshot() == result.after  # no reading case wrote anything


@pytest.fixture
def repaired(db: Db) -> Repaired:
    """A repaired state world of the case's own, for the cases that run more steps on it."""
    return _repair(db)


def _lines(output: str, marker: str) -> list[str]:
    return [line for line in output.splitlines() if PREFIX in line and marker in line]


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #
def test_the_repair_is_the_single_head_on_cf3_and_data_only() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert [r.revision for r in script.get_revisions("heads")] == [REPAIR]
    assert script.get_revision(REPAIR).down_revision == CF3
    source = MIGRATION.read_text()
    for ddl in (
        "op.create",
        "op.drop",
        "op.add_column",
        "op.alter",
        "batch_alter_table",
        "op.execute",
        "CREATE ",
        "DROP ",
        "ALTER ",
    ):
        assert ddl not in source, ddl


# --------------------------------------------------------------------------- #
# The reported case, end to end through the 5D-W1 API
# --------------------------------------------------------------------------- #
@contextmanager
def _api(db: Db) -> Iterator[tuple[TestClient, dict[str, str]]]:
    """The application on this database, as an owner of organization A (only ``get_db`` is
    overridden)."""
    factory = sessionmaker(bind=db.engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        user = User(
            id="rep-owner",
            email="owner@repair.example",
            full_name="Owner",
            hashed_password=hash_password("repair-pass-1"),
        )
        s.add(user)
        s.flush()
        s.add(OrganizationMember(organization_id="org-a", user_id=user.id, role="owner"))
        session = auth_service.create_session(s, user)
        s.commit()
        token = auth_service.issue_token(user, session)

    def _get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    saved = SessionLocal.kw.get("bind")
    SessionLocal.kw["bind"] = db.engine
    app.dependency_overrides[get_db] = _get_db
    active_rate_limiter()._hits.clear()
    try:
        yield TestClient(app), {"Authorization": f"Bearer {token}"}
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        SessionLocal.kw["bind"] = saved


def _edited_after_the_backfill(db: Db) -> None:
    """CF-2: a campaign with la1. CF-3 back-fills la1. Then the customer adds la1b through the
    pre-5D-W1 API (the factory PATCH: the JSON alone)."""
    db.ok("upgrade", CF2)
    _tenants(db)
    _campaigns(db, {"edited": ("ws-a1", ["la1"])})
    db.ok("upgrade", CF3)
    assert db.memberships() == {"edited": {"la1"}}  # what the back-fill copied
    _factory_patch(db, "edited", ["la1", "la1b"])
    assert _json(db.raw_json()["edited"]) == ["la1", "la1b"]  # the customer's latest intent


URL = f"{API}/workspaces/ws-a1/campaigns/edited"


def test_without_the_repair_5dw1_answers_the_backfill_and_the_next_patch_loses_the_edit(db):
    _edited_after_the_backfill(db)
    assert db.version() == [CF3]
    with _api(db) as (client, headers):
        seen = client.get(URL, headers=headers)
        assert seen.status_code == 200, seen.text
        assert seen.json()["location_ids"] == ["la1"]  # the back-filled membership, not la1b
        # The customer adds la1c to what the screen shows: la1b, added before, is lost.
        patched = client.patch(
            URL, headers=headers, json={"location_ids": [*seen.json()["location_ids"], "la1c"]}
        )
        assert patched.status_code == 200 and patched.json()["location_ids"] == ["la1", "la1c"]
    assert db.memberships() == {"edited": {"la1", "la1c"}}
    assert _json(db.raw_json()["edited"]) == ["la1", "la1c"]  # the earlier edit is gone


def test_with_the_repair_5dw1_answers_the_intended_membership_and_a_patch_keeps_it(db):
    _edited_after_the_backfill(db)
    output = db.ok("upgrade", "head")
    assert db.version() == [REPAIR]
    assert f"{PREFIX}: S1 inserted: 1 campaigns=[edited]" in output, output
    with _api(db) as (client, headers):
        seen = client.get(URL, headers=headers)
        assert seen.status_code == 200 and seen.json()["location_ids"] == ["la1", "la1b"]
        patched = client.patch(
            URL, headers=headers, json={"location_ids": [*seen.json()["location_ids"], "la1c"]}
        )
        assert patched.json()["location_ids"] == ["la1", "la1b", "la1c"]
    assert db.memberships() == {"edited": {"la1", "la1b", "la1c"}}


# --------------------------------------------------------------------------- #
# Every state, S0-S8
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("campaign_id", sorted(EXPECTED))
def test_each_state_is_repaired_as_documented(shared: Repaired, campaign_id: str) -> None:
    if campaign_id not in shared.campaigns:
        assert shared.db.pg and campaign_id == "s6-unparseable"
        return  # PostgreSQL cannot hold the state: see the next test
    members, mirror = EXPECTED[campaign_id]
    assert shared.db.memberships().get(campaign_id, set()) == members
    before_json, after_json = shared.before[1][campaign_id], shared.after[1][campaign_id]
    if mirror == "unchanged":
        assert after_json == before_json  # S6: the unreadable source is left as it is
    else:
        assert _json(after_json) == mirror


def test_postgresql_refuses_unparseable_json_so_that_s6_case_is_sqlite_only(db: Db) -> None:
    db.ok("upgrade", CF2)
    _tenants(db)
    _campaigns(db, {"c": ("ws-a1", [])})
    if not db.pg:
        _set_s6(db, "c", "text", "la1, la1b")
        assert db.raw_json()["c"] == "la1, la1b"  # SQLite stores it: the S6 case exists
        return
    with pytest.raises(sa.exc.DataError):  # pragma: no cover - gated on PostgreSQL
        _set_s6(db, "c", "text", "la1, la1b")


def test_the_schema_refuses_sql_null_so_the_s6_null_case_is_a_json_null(db: Db) -> None:
    """``campaigns.location_ids`` is NOT NULL (the model's ``Mapped[list]``), on both engines: SQL
    NULL is impossible; a JSON ``null`` is not, and is the S6 ``null`` case."""
    db.ok("upgrade", CF3)
    _tenants(db)
    _campaigns(db, {"c": ("ws-a1", [])})
    with pytest.raises(IntegrityError):
        _set_s6(db, "c", "sql", None)
    _set_s6(db, "c", "json", None)
    assert db.raw_json()["c"] == "null"


def test_the_report_counts_every_state(shared: Repaired) -> None:
    out = shared.output
    unreadable = 4 if shared.db.pg else 5
    for line in (
        f"{PREFIX}: S0 consistent: 4 campaigns=[s0-consistent,s0-empty,s7-deactivated,"
        "zb-other-tenant]",
        f"{PREFIX}: S1 inserted: 4 campaigns=[s1-added,s4-invalid,s6-cross-tenant,"
        "s8-row-organization]",
        f"{PREFIX}: S2 deleted: 2 campaigns=[s2-removed,s2-removed-all]",
        f"{PREFIX}: S3 foreign: 3 campaigns=[s3-foreign]",
        f"{PREFIX}: S4 missing or invalid: 4 campaigns=[s4-invalid,s4-missing]",
        f"{PREFIX}: S5 duplicate: 1 campaigns=[s5-duplicate]",
        f"{PREFIX}: S7 deactivated kept: 1 campaigns=[s7-deactivated]",
        f"{PREFIX}: S8 cross-tenant row deleted: 5 campaigns=[s3-foreign,s6-cross-tenant,"
        "s8-row-organization,s8-row-outside]",
        f"{PREFIX}: mirror rewritten: 5 campaigns=[s3-foreign,s4-invalid,s4-missing,"
        "s5-duplicate,unsorted]",
        f"{PREFIX}: {len(shared.campaigns)} campaign(s) read, 4 membership(s) inserted, "
        f"7 deleted, 5 mirror(s) rewritten, 8 reference(s) dropped, {unreadable} unreadable",
    ):
        assert line in out, (line, out)
    s6 = [c for c in shared.campaigns if c.startswith("s6-")]
    assert f"{PREFIX}: S6 unreadable: {unreadable} campaigns=[{','.join(s6)}]" in out
    reasons = {
        "s6-cross-tenant": "not_a_list",
        "s6-json-null": "null",
        "s6-object": "not_a_list",
        "s6-string": "not_a_list",
        "s6-unparseable": "unparseable",
    }
    for campaign_id in s6:
        line = (
            f"unreadable campaign={campaign_id} workspace=ws-a1 reason={reasons[campaign_id]} "
            "memberships kept"
        )
        assert line in out, line
    # Dropped references: ids only; an element that names no location is never echoed.
    for location, reason in (
        ("la2", "foreign_workspace"),
        ("lb1", "foreign_organization"),
        ("lx", "foreign_organization"),
    ):
        assert (
            f"dropped campaign=s3-foreign workspace=ws-a1 location={location} reason={reason}"
            in out
        )
    assert "dropped campaign=s4-missing workspace=ws-a1 location=None reason=missing" in out
    assert len(_lines(out, "campaign=s4-invalid workspace=ws-a1 location=None reason=invalid")) == 3
    assert "dropped campaign=s5-duplicate workspace=ws-a1 location=None reason=duplicate" in out
    assert "gone" not in out
    # Cross-tenant rows (S8): one line each, ids only.
    for campaign_id, location, reason in (
        ("s3-foreign", "lx", "location_foreign_organization"),
        ("s6-cross-tenant", "la1b", "row_foreign_organization"),
        ("s6-cross-tenant", "lx", "location_foreign_organization"),
        ("s8-row-organization", "la1", "row_foreign_organization"),
        ("s8-row-outside", "la1c", "row_foreign_organization"),
    ):
        line = (
            f"{PREFIX}: cross-tenant row deleted campaign={campaign_id} workspace=ws-a1 "
            f"location={location} reason={reason}"
        )
        assert line in out, line
    assert len(_lines(out, "cross-tenant row deleted campaign=")) == 5


def test_tenant_isolation_no_membership_crosses_a_tenant(shared: Repaired) -> None:
    db = shared.db
    # Before: CF-3's back-fill had kept lx (right workspace, wrong organization), and the direct
    # writes had left rows naming organization B on three campaigns of organization A.
    assert shared.crossing_before == [
        ("s3-foreign", "lx"),
        ("s6-cross-tenant", "la1b"),
        ("s6-cross-tenant", "lx"),
        ("s8-row-organization", "la1"),
        ("s8-row-outside", "la1c"),
    ]
    assert _crossing(db) == []
    assert db.memberships()["zb-other-tenant"] == {"lb1"}  # the other tenant is untouched
    assert (
        shared.after[0][("zb-other-tenant", "lb1")] == shared.before[0][("zb-other-tenant", "lb1")]
    )


def test_the_schema_refuses_a_cross_workspace_membership_but_not_an_organization_mismatch(
    db: Db,
) -> None:
    db.ok("upgrade", CF2)
    _tenants(db)
    _campaigns(db, {"c": ("ws-a1", [])})
    db.ok("upgrade", CF3)
    row = {"organization_id": "org-a", "workspace_id": "ws-a1", "campaign_id": "c"}
    for location in ("la2", "lb1"):  # another workspace, same or other organization
        with pytest.raises(IntegrityError):
            db.execute(MEMBERSHIP.insert().values(id=f"x-{location}", location_id=location, **row))
    # lx is in ws-a1 but names organization B: the workspace key admits it; only the repair's
    # organization check removes it.
    db.execute(MEMBERSHIP.insert().values(id="x-lx", location_id="lx", **row))
    assert db.memberships() == {"c": {"lx"}}


def test_every_kept_membership_is_the_same_physical_row(shared: Repaired) -> None:
    """Every correct row the repair keeps is the same physical row. The one exception is a
    replaced cross-tenant row (``REPLACED``), whose pair is kept with a new, correct row: the
    next test."""
    before, after = shared.before[0], shared.after[0]
    kept = (set(before) & set(after)) - REPLACED
    expected_kept = {
        (cid, loc)
        for cid, (members, _mirror) in EXPECTED.items()
        if cid in shared.campaigns
        for loc in members
        if (cid, loc) in before and (cid, loc) not in REPLACED
    }
    assert kept == expected_kept
    assert len(kept) >= 14
    for pair in kept:
        # ctid/xmin (PostgreSQL) or rowid; id, created_at and the tenant columns
        assert after[pair] == before[pair], pair
    assert set(before) - set(after) == {
        ("s2-removed", "la1b"),
        ("s2-removed-all", "la1"),
        ("s3-foreign", "lx"),
        ("s6-cross-tenant", "lx"),
        ("s8-row-outside", "la1c"),
    }
    assert set(after) - set(before) == {("s1-added", "la1b"), ("s4-invalid", "la1")}


def test_a_cross_tenant_row_is_deleted_and_a_kept_membership_gets_a_correct_row(
    shared: Repaired,
) -> None:
    """S8 (D-5DW4-03): a row naming organization B on a campaign of organization A goes, even
    though its (campaign, location) pair is valid; where the membership is kept (in the JSON,
    or on an S6 campaign) a new row with the campaign's tenant takes its place. The campaign's
    correct rows stay the same physical rows, and its JSON is not rewritten."""
    before, after = shared.before[0], shared.after[0]
    for pair in sorted(REPLACED):
        assert before[pair][-2:] == ("org-b", "ws-a1"), pair  # (organization, workspace)
        assert after[pair][-2:] == ("org-a", "ws-a1"), pair
        assert after[pair][-4] != before[pair][-4], pair  # a new row id: deleted, then inserted
    for pair in (
        ("s8-row-organization", "la1b"),
        ("s8-row-outside", "la1"),
        ("s6-cross-tenant", "la1"),
    ):
        assert after[pair] == before[pair], pair  # the campaign's correct row, untouched
    for pair in (("s8-row-outside", "la1c"), ("s6-cross-tenant", "lx")):
        assert pair in before and pair not in after, pair  # not a kept membership: gone
    for campaign_id in ("s8-row-organization", "s8-row-outside", "s6-cross-tenant"):
        assert shared.after[1][campaign_id] == shared.before[1][campaign_id]  # JSON as it was
        # Not S0: s8-row-outside's only fault is its cross-tenant row, which the defect missed.
        assert campaign_id not in _lines(shared.output, "S0 consistent")[0]


def test_a_row_of_another_workspace_is_replaced_where_only_sqlite_can_hold_it(db: Db) -> None:
    """S8's workspace reasons. The composite keys refuse a row whose own workspace, or whose
    location's workspace, is not its campaign's: always on PostgreSQL, and on SQLite with
    foreign keys on, as the application runs it. A SQLite database written with them off
    (SQLite's default, and the migration connection's) can hold one; the repair removes it,
    on a readable campaign and on an S6 one, and replaces it where the membership is kept."""
    db.ok("upgrade", CF2)
    _tenants(db)
    _campaigns(db, {"r": ("ws-a1", ["la1"]), "u": ("ws-a1", ["la1", "la1b"])})
    db.ok("upgrade", CF3)
    _set_s6(db, "u", "json", {"ids": ["la1", "la1b"]})
    writes = (
        MEMBERSHIP.update().where(MEMBERSHIP.c.campaign_id == "r").values(workspace_id="ws-a2"),
        MEMBERSHIP.update()
        .where(MEMBERSHIP.c.campaign_id == "u", MEMBERSHIP.c.location_id == "la1")
        .values(workspace_id="ws-a2"),
        MEMBERSHIP.insert().values(
            id="x-u-la2",
            organization_id="org-a",
            workspace_id="ws-a1",
            campaign_id="u",
            location_id="la2",
        ),
    )
    if db.pg:
        for write in writes:  # pragma: no cover - gated on PostgreSQL
            with pytest.raises(IntegrityError):
                db.execute(write)
        return
    loose = create_engine(db.url)  # no foreign-key pragma: SQLite's default
    try:
        with loose.begin() as conn:
            for write in writes:
                conn.execute(write)
    finally:
        loose.dispose()
    assert _crossing(db) == [("r", "la1"), ("u", "la1"), ("u", "la2")]
    before = db.identity()
    out = db.ok("upgrade", "head")
    after = db.identity()
    assert db.memberships() == {"r": {"la1"}, "u": {"la1", "la1b"}}
    assert _crossing(db) == []
    for pair in (("r", "la1"), ("u", "la1")):
        assert before[pair][-2:] == ("org-a", "ws-a2"), pair
        assert after[pair][-2:] == ("org-a", "ws-a1"), pair
        assert after[pair][-4] != before[pair][-4], pair
    assert after[("u", "la1b")] == before[("u", "la1b")]
    for campaign_id, location, reason in (
        ("r", "la1", "row_foreign_workspace"),
        ("u", "la1", "row_foreign_workspace"),
        ("u", "la2", "location_foreign_workspace"),
    ):
        line = (
            f"cross-tenant row deleted campaign={campaign_id} workspace=ws-a1 "
            f"location={location} reason={reason}"
        )
        assert line in out, (line, out)


# --------------------------------------------------------------------------- #
# Transactional failure handling
# --------------------------------------------------------------------------- #
_PG_POISON = (
    "CREATE FUNCTION sn_test_poison() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
    " RAISE EXCEPTION 'injected mid-run failure: memberships now %',"
    " (SELECT count(*) FROM campaign_locations); END $$",
    "CREATE TRIGGER sn_test_poison BEFORE UPDATE OF location_ids ON campaigns"
    " FOR EACH ROW EXECUTE FUNCTION sn_test_poison()",
)
_PG_CURE = ("DROP TRIGGER sn_test_poison ON campaigns", "DROP FUNCTION sn_test_poison()")
_SQLITE_POISON = (
    "CREATE TRIGGER sn_test_poison BEFORE UPDATE OF location_ids ON campaigns"
    " BEGIN SELECT RAISE(ABORT, 'injected mid-run failure'); END",
)
_SQLITE_CURE = ("DROP TRIGGER sn_test_poison",)


def test_a_failure_after_deletes_and_inserts_leaves_everything_as_it_was(db: Db) -> None:
    """The repair deletes, inserts, then rewrites the mirrors; the test database refuses the
    first mirror rewrite. On PostgreSQL the error carries the membership count the transaction
    had reached (proof the deletes and inserts ran); after it nothing persists and
    ``alembic_version`` is still CF-3's. A clean re-run then completes. The same holds,
    measured, on SQLite."""
    _state_world(db)
    for statement in _PG_POISON if db.pg else _SQLITE_POISON:
        db.execute(sa.text(statement))
    before = db.snapshot()
    failed = db.alembic("upgrade", "head")
    out = failed.stdout + failed.stderr
    assert failed.returncode != 0
    assert "injected mid-run failure" in out, out
    if db.pg:
        # 4 inserted and 7 deleted (2 S2, 5 S8) before the refusal: three rows fewer than before.
        assert f"memberships now {len(before[0]) - 3}" in out, out
    assert db.snapshot() == before  # memberships (row identity), JSON, version
    assert db.version() == [CF3]
    for statement in _PG_CURE if db.pg else _SQLITE_CURE:
        db.execute(sa.text(statement))
    db.ok("upgrade", "head")
    assert db.version() == [REPAIR]
    for campaign_id in before[1]:
        members, _ = EXPECTED[campaign_id]
        assert db.memberships().get(campaign_id, set()) == members


# --------------------------------------------------------------------------- #
# The lock (PostgreSQL only)
# --------------------------------------------------------------------------- #
_LOCK_STATEMENT = "LOCK TABLE campaigns, campaign_locations IN SHARE ROW EXCLUSIVE MODE"


def _from_another_session(conn, statement: str) -> str:
    """``"waited"`` when ``statement`` had to wait for a lock (``lock_timeout`` cancelled it,
    SQLSTATE 55P03), else ``"proceeded"``; rolled back either way. Only a lock wait can give
    ``"waited"``: a slow statement that waits for no lock is never cancelled by
    ``lock_timeout``."""
    tx = conn.begin()
    try:
        conn.execute(sa.text("SET LOCAL lock_timeout = '500ms'"))
        conn.execute(sa.text(statement))
        return "proceeded"
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        return "waited"
    finally:
        tx.rollback()


def test_on_postgresql_the_repair_locks_both_tables_against_writers_not_readers(
    tmp_path,
) -> None:
    """The revision's first statement on PostgreSQL is its ``LOCK TABLE``. While a real run's
    transaction is still open, after it has written, its session holds SHARE ROW EXCLUSIVE on
    both tables. From another session, a read and a ``FOR NO KEY UPDATE`` row lock proceed; an
    INSERT and a DELETE on ``campaign_locations`` and an UPDATE of ``campaigns`` wait, each on
    rows the repair never touched (so the table lock makes them wait, not a row lock); and a
    second lock in the same mode waits, so two runs serialize. No outcome depends on timing:
    the repair's transaction stays open until every probe has finished. (W21-REV-5DW1 r3 F-2,
    after the reviewer's probe L1.)"""
    with _database("postgresql", tmp_path) as db:
        _state_world(db)
        statements: list[str] = []
        with db.engine.connect() as repair, db.engine.connect() as other:
            event.listen(
                repair,
                "before_cursor_execute",
                lambda _conn, _cursor, statement, *_rest: statements.append(statement),
            )
            tx = repair.begin()
            try:
                report = _module().reconcile_campaign_locations(repair)
                assert report.changes() > 0  # it wrote: the lock is held with work in flight
                pid = repair.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
                held = {
                    (row.relname, row.mode)
                    for row in other.execute(
                        sa.text(
                            "SELECT c.relname, l.mode FROM pg_locks l"
                            " JOIN pg_class c ON c.oid = l.relation"
                            " WHERE l.pid = :pid AND l.granted"
                            " AND c.relname IN ('campaigns', 'campaign_locations')"
                        ),
                        {"pid": pid},
                    )
                }
                other.rollback()
                observed = {
                    "first_statement": statements[0],
                    "share_row_exclusive_held_on": sorted(
                        table for table, mode in held if mode == "ShareRowExclusiveLock"
                    ),
                    "select_memberships": _from_another_session(
                        other, "SELECT count(*) FROM campaign_locations"
                    ),
                    "row_lock_untouched_campaign": _from_another_session(
                        other,
                        "SELECT id FROM campaigns WHERE id = 'zb-other-tenant' FOR NO KEY UPDATE",
                    ),
                    "insert_membership_untouched_campaign": _from_another_session(
                        other,
                        "INSERT INTO campaign_locations (id, organization_id, workspace_id,"
                        " campaign_id, location_id, created_at) VALUES ('lock-probe', 'org-a',"
                        " 'ws-a1', 's0-empty', 'la1c', now())",
                    ),
                    "delete_membership_untouched_campaign": _from_another_session(
                        other,
                        "DELETE FROM campaign_locations WHERE campaign_id = 'zb-other-tenant'",
                    ),
                    "update_untouched_campaign": _from_another_session(
                        other, "UPDATE campaigns SET name = name WHERE id = 'zb-other-tenant'"
                    ),
                    "second_lock_same_mode": _from_another_session(other, _LOCK_STATEMENT),
                }
            finally:
                tx.rollback()
    assert observed == {
        "first_statement": _LOCK_STATEMENT,
        "share_row_exclusive_held_on": ["campaign_locations", "campaigns"],
        "select_memberships": "proceeded",
        "row_lock_untouched_campaign": "proceeded",
        "insert_membership_untouched_campaign": "waited",
        "delete_membership_untouched_campaign": "waited",
        "update_untouched_campaign": "waited",
        "second_lock_same_mode": "waited",
    }


# --------------------------------------------------------------------------- #
# Safe repeat execution
# --------------------------------------------------------------------------- #
def _module():
    spec = importlib.util.spec_from_file_location("repair_revision", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_second_run_changes_nothing(repaired: Repaired) -> None:
    db = repaired.db
    with db.engine.begin() as conn:
        report = _module().reconcile_campaign_locations(conn)
    assert report.changes() == 0
    assert (
        report.inserted,
        report.deleted,
        report.cross_tenant,
        report.mirrored,
        report.dropped,
    ) == ([], [], [], [], [])
    # Only what the repair leaves as it is by design is reported again.
    assert len(report.unreadable) == (4 if db.pg else 5)
    assert report.deactivated_kept == [("s7-deactivated", "la1-off")]
    assert sorted(report.consistent) == sorted(
        c for c in repaired.campaigns if not c.startswith("s6-")
    )
    assert db.snapshot() == repaired.after


def test_upgrade_noop_downgrade_upgrade_changes_nothing(repaired: Repaired) -> None:
    db = repaired.db
    db.ok("downgrade", "-1")
    assert db.version() == [CF3]
    assert db.snapshot()[:2] == repaired.after[:2]  # the downgrade wrote nothing
    again = db.ok("upgrade", "head")
    assert db.version() == [REPAIR]
    assert db.snapshot() == repaired.after
    assert (
        f"{PREFIX}: {len(repaired.campaigns)} campaign(s) read, 0 membership(s) inserted, "
        "0 deleted, 0 mirror(s) rewritten, 0 reference(s) dropped" in again
    ), again


def test_an_empty_database_goes_from_base_to_head_with_no_report(db: Db) -> None:
    out = db.ok("upgrade", "head")
    assert db.version() == [REPAIR]
    assert PREFIX not in out, out
    assert db.ok("check")
