"""W21 ADDR: the structured-business-address revision ``6f8a80cf1785``, on both engines.

The operator's instruction of 2026-11-12 authorizes "the limited CF-3 migration-freeze exception
needed for this package’s address storage and validation". Through the real Alembic CLI (the
target-bound downgrade guard included), on a disposable SQLite file and, when
``TEST_POSTGRES_URL`` is set, a PostgreSQL database of the test's own:

* **M-1** the upgrade adds exactly the ten columns to ``geo_coverage_rules``, all nullable with
  no default, plus the confirming account's key (``ON DELETE SET NULL``), its index and the two
  CHECKs; ``alembic check`` then reports no drift between the models and the migrations;
* **M-2 / M-3** nothing is back-filled and nothing is rewritten: every row of every table,
  including a free-text business address, captured brief, draft and review snapshots and every
  ``jurisdiction_code``, is identical after the upgrade, and the new columns read NULL; one
  guarded step down removes exactly the ten columns and every row of every table is again
  identical to what it was before the upgrade;
* **M-4** a single head, on 5D-W1's repair;
* **M-5** the CHECKs refuse a partial confirmation binding and a malformed code on both engines,
  and their frozen literals equal their Python authorities;
* **M-6** deleting the confirming account sets only its reference NULL and keeps the binding;
* **M-7** the revision is DDL on ``geo_coverage_rules`` only: no data statement, no other
  table (the exception is this package's, not Q-3's).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.tests._auth2_support import pg_database, require_postgres, sqlite_file_engine

API_DIR = Path(__file__).resolve().parents[2]
HEAD = "6f8a80cf1785"
D1_REPAIR = "d5f616c0ec97"
CF3 = "c3f8e1a6d2b9"
MIGRATION = next((API_DIR / "alembic" / "versions").glob(f"*-{HEAD}_*.py"))
TABLE = "geo_coverage_rules"
ADDRESS = {
    "business_address_street",
    "business_address_locality",
    "business_address_region",
    "business_address_postcode",
    "business_address_country",
}
BINDING = {
    "locality_confirmed_locality",
    "locality_confirmed_city",
    "locality_confirmed_jurisdiction",
    "locality_confirmed_at",
    "locality_confirmed_by_user_id",
}
NEW = ADDRESS | BINDING


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

    def alembic(self, *args: str) -> str:
        confirm: list[str] = []
        if args[0] == "downgrade":
            token = subprocess.run(
                [sys.executable, "-m", "app.db.migrate", "downgrade-confirmation", args[1]],
                cwd=API_DIR, env=self._env(), capture_output=True, text=True,
            )
            assert token.returncode == 0, token.stdout + token.stderr
            confirm = ["-x", f"confirm={token.stdout.strip()}"]
        done = subprocess.run(
            [sys.executable, "-m", "alembic", *confirm, *args],
            cwd=API_DIR, env=self._env(), capture_output=True, text=True,
        )
        assert done.returncode == 0, (args, done.stdout + done.stderr)
        return done.stdout + done.stderr

    def version(self) -> list[str]:
        with self.engine.connect() as conn:
            return list(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars())

    def inspect(self) -> sa.Inspector:
        self.engine.dispose()  # a fresh view of the schema the CLI just changed
        return sa.inspect(self.engine)

    def columns(self, table: str) -> dict[str, dict]:
        return {c["name"]: c for c in self.inspect().get_columns(table)}

    def dump(self) -> dict[str, list[tuple]]:
        """Every row of every table (alembic's own excepted), ordered, all columns."""
        out: dict[str, list[tuple]] = {}
        meta = sa.MetaData()
        meta.reflect(self.engine)
        with self.engine.connect() as conn:
            for name, table in sorted(meta.tables.items()):
                if name == "alembic_version":
                    continue
                cols = sorted(table.c, key=lambda c: c.name)
                rows = conn.execute(sa.select(*cols)).all()
                out[name] = sorted(
                    (tuple((c.name, _plain(v)) for c, v in zip(cols, row, strict=True))
                     for row in rows),
                    key=repr,
                )
        return out

    def execute(self, statement, params=None) -> None:
        with self.engine.begin() as conn:
            conn.execute(statement, params or {})


def _plain(value):
    return value.isoformat() if isinstance(value, datetime) else value


@contextmanager
def _database(kind: str, directory: Path) -> Iterator[Db]:
    if kind == "sqlite":
        path = directory / "addr.db"
        engine = sqlite_file_engine(path)
        try:
            yield Db(f"sqlite:///{path}", engine)
        finally:
            engine.dispose()
        return
    require_postgres()
    with pg_database("sn_addr") as url:  # pragma: no cover - gated on PostgreSQL
        engine = create_engine(url, future=True)
        try:
            yield Db(url, engine)
        finally:
            engine.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path) -> Iterator[Db]:
    with _database(request.param, tmp_path) as database:
        yield database


def _only(row: tuple, drop: set[str]) -> tuple:
    return tuple((k, v) for k, v in row if k not in drop)


#: A stored rule as a customer left it before this revision: every column set, a town-only
#: free-text address and a captured (CF-3) jurisdiction code.
RULE = {
    "id": "geo-1", "organization_id": "org-1", "workspace_id": "ws-1", "location_id": "loc-1",
    "coverage_type": "radius", "business_address": "Montgomery, 1 Main St, United States",
    "center_latitude": 61.2, "center_longitude": -149.9, "radius_miles": 25,
    "country": "United States", "state": "Alaska", "included_markets": ["Anchorage"],
    "excluded_markets": ["Juneau"], "online_global": False, "jurisdiction_code": "US-AK",
}


def _seed(db: Db) -> None:
    """At the repair (before this revision): a tenant, a location with a captured code, its
    rule (written with the pre-revision columns only), and a captured brief, draft and review."""
    from app.brands.models import Brand
    from app.briefs.models import RecommendationBrief
    from app.creative.models import ContentReview, CreativeDraft
    from app.intelligence.records import SignalIntelligenceRecord
    from app.locations.models import BusinessLocation
    from app.opportunities.models import Opportunity
    from app.organizations.models import Organization, User, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    scope = {"organization_id": "org-1", "workspace_id": "ws-1"}
    with Session(db.engine) as s:
        s.add(Organization(id="org-1", name="o", slug="o"))
        s.flush()
        s.add(Workspace(id="ws-1", organization_id="org-1", name="w", slug="w"))
        for uid in ("u-author", "u-confirmer"):
            s.add(User(id=uid, email=f"{uid}@example.com", full_name=uid, hashed_password="x"))
        s.flush()
        s.add(Brand(id="br-1", name="b", **scope))
        s.flush()
        s.add(BusinessLocation(id="loc-1", brand_id="br-1", name="Anchorage", city="Anchorage",
                               state_province="Alaska", country="United States",
                               jurisdiction_code="US-AK", **scope))
        s.commit()
    rule = sa.table(TABLE, *(
        sa.column(k, sa.JSON) if k.endswith("_markets") else sa.column(k) for k in RULE
    ))
    db.execute(rule.insert().values(RULE))
    with Session(db.engine) as s:
        s.add(ScoutRequest(id="sr-1", brand_id="br-1", location_id="loc-1", name="s", **scope))
        s.flush()
        req = {**scope, "scout_request_id": "sr-1"}
        s.add(RawSignal(id="raw-1", source_type="rss_news", content="c", **req))
        s.flush()
        s.add(NormalizedSignal(id="ns-1", raw_signal_id="raw-1", source_type="rss_news",
                               excerpt="e", content_hash="0" * 64, **req))
        s.flush()
        s.add(Opportunity(id="opp-1", brand_id="br-1", location_id="loc-1", title="t",
                          classification="emerging", decision="monitor", opportunity_score=50,
                          resolved_market="Anchorage, AK", audience_fit="a", **req))
        s.flush()
        s.add(SignalIntelligenceRecord(
            id="sir-1", normalized_signal_id="ns-1", opportunity_id="opp-1",
            analysis_version="3c", scoring_version="3c.1", fingerprint="f" * 64,
            enricher="deterministic", accepted=True, classification="emerging", **req))
        s.flush()
        s.add(RecommendationBrief(
            id="brief-1", opportunity_id="opp-1", intelligence_record_id="sir-1",
            location_id="loc-1", brief_version=1, analysis_version="3c", scoring_version="3c.1",
            prompt_name="explain_opportunity", prompt_version="1.2.0", model_identifier="m",
            input_hash="0" * 64, is_simulated=True, created_by="u-author",
            created_by_snapshot="u-author", summary="s", rationale="r",
            recommended_actions=["a"], suggested_angles=["b"], risk_notes=["c"],
            evidence_refs=[], jurisdiction_code="US-AK", **scope))
        s.flush()
        s.add(CreativeDraft(
            id="draft-1", brief_id="brief-1", opportunity_id="opp-1", location_id="loc-1",
            draft_version=1, channel="email", headline="h", body="b", call_to_action="c",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="m",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, created_by="u-author", created_by_snapshot="u-author",
            jurisdiction_code="US-AK", **scope))
        s.flush()
        s.add(ContentReview(
            id="review-1", draft_id="draft-1", draft_version=1, risk_level="low",
            is_blocked=False, findings=[], warnings=[], blocked_claim_ids=[],
            required_disclaimers=[], disclaimers_resolved=True, passed=True,
            jurisdiction_code="US", **scope))
        s.commit()


def _at_repair_seeded(db: Db) -> dict[str, list[tuple]]:
    db.alembic("upgrade", D1_REPAIR)
    assert db.version() == [D1_REPAIR]
    _seed(db)
    return db.dump()


# --------------------------------------------------------------------------- #
# M-4 / M-7: the chain and the revision's scope
# --------------------------------------------------------------------------- #
def test_the_revision_is_the_single_head_on_the_repair() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert [r.revision for r in script.get_revisions("heads")] == [HEAD]
    assert script.get_revision(HEAD).down_revision == D1_REPAIR
    assert script.get_revision(D1_REPAIR).down_revision == CF3


def test_the_revision_is_ddl_on_geo_coverage_rules_only() -> None:
    """M-7: no data statement, and every operation names ``geo_coverage_rules``."""
    source = MIGRATION.read_text()
    code = source.split('"""', 2)[2]  # the statements, without the module docstring
    for forbidden in ("op.execute", "bulk_insert", "sa.table(", ".insert(", ".update(",
                      ".delete(", "UPDATE ", "INSERT ", "DELETE ", "op.create_table",
                      "op.drop_table", "server_default"):
        assert forbidden not in code, forbidden
    tree = ast.parse(source)
    tables: set[str] = set()
    calls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "op":
                calls += 1
                if node.func.attr in ("batch_alter_table",):
                    tables.add(ast.literal_eval(node.args[0]) if isinstance(
                        node.args[0], ast.Constant) else node.args[0].id)
                elif node.func.attr == "create_index":
                    tables.add(node.args[1].id)
                elif node.func.attr == "drop_index":
                    tables.add(next(k.value.id for k in node.keywords if k.arg == "table_name"))
                else:
                    pytest.fail(f"unexpected operation op.{node.func.attr}")
    assert calls == 4
    module = {}
    exec(compile(tree, str(MIGRATION), "exec"), module)  # noqa: S102 - our own revision
    assert {module[name] if name in module else name for name in tables} == {TABLE}


def test_the_frozen_check_literals_equal_their_python_authorities() -> None:
    """M-5 (text): the revision's CHECK literals are the model's."""
    from app.jurisdiction.codes import jurisdiction_code_check_sql
    from app.locations.models import LOCALITY_CONFIRMATION_COMPLETE_SQL

    module: dict = {}
    exec(compile(MIGRATION.read_text(), str(MIGRATION), "exec"), module)  # noqa: S102
    assert module["_JURISDICTION_SHAPE"] == jurisdiction_code_check_sql(
        "locality_confirmed_jurisdiction", nullable=True
    )
    assert module["_COMPLETE"] == LOCALITY_CONFIRMATION_COMPLETE_SQL


# --------------------------------------------------------------------------- #
# M-1 / M-2 / M-3: upgrade, no back-fill, nothing rewritten; downgrade restores
# --------------------------------------------------------------------------- #
def test_upgrade_adds_exactly_the_ten_nullable_columns_and_rewrites_nothing(db: Db) -> None:
    before = _at_repair_seeded(db)
    columns_before = db.columns(TABLE)

    db.alembic("upgrade", "head")
    assert db.version() == [HEAD]
    columns_after = db.columns(TABLE)
    assert set(columns_after) - set(columns_before) == NEW
    assert set(columns_before) <= set(columns_after)
    for name in NEW:
        assert columns_after[name]["nullable"] is True, name
        assert columns_after[name]["default"] is None, name
    inspector = db.inspect()
    fks = [fk for fk in inspector.get_foreign_keys(TABLE)
           if fk["constrained_columns"] == ["locality_confirmed_by_user_id"]]
    assert len(fks) == 1
    assert fks[0]["referred_table"] == "users" and fks[0]["referred_columns"] == ["id"]
    assert (fks[0].get("options") or {}).get("ondelete", "").upper() == "SET NULL"
    assert "ix_geo_coverage_rules_locality_confirmed_by_user_id" in {
        i["name"] for i in inspector.get_indexes(TABLE)
    }
    assert {
        "ck_geo_coverage_rule_jurisdiction_code",  # CF-3's, kept through the batch rebuild
        "ck_geo_coverage_rule_locality_confirmed_jurisdiction",
        "ck_geo_coverage_rule_locality_confirmation_complete",
    } <= {c["name"] for c in inspector.get_check_constraints(TABLE)}

    after = db.dump()
    assert set(after) == set(before)
    for table, rows in before.items():
        if table == TABLE:
            assert [_only(r, NEW) for r in after[table]] == rows
            for row in after[table]:  # no back-fill: every new column NULL
                assert {k: v for k, v in row if k in NEW} == dict.fromkeys(NEW)
        else:
            assert after[table] == rows, table
    # The captured snapshots, by name (they are in the dump too).
    for table, code in (("recommendation_briefs", "US-AK"), ("creative_drafts", "US-AK"),
                        ("content_reviews", "US"), ("business_locations", "US-AK"),
                        (TABLE, "US-AK")):
        assert [dict(r)["jurisdiction_code"] for r in after[table]] == [code], table
    assert [dict(r)["business_address"] for r in after[TABLE]] == [RULE["business_address"]]
    assert "No new upgrade operations detected." in db.alembic("check")


def test_one_guarded_step_down_removes_exactly_them_and_every_row_is_as_before(db: Db) -> None:
    before = _at_repair_seeded(db)
    columns_before = db.columns(TABLE)
    db.alembic("upgrade", "head")
    db.execute(sa.text(
        "UPDATE geo_coverage_rules SET business_address_street = '1 Main St',"
        " business_address_locality = 'Ikeja', business_address_region = 'Lagos',"
        " business_address_postcode = '100001', business_address_country = 'Nigeria',"
        " locality_confirmed_locality = 'Ikeja', locality_confirmed_city = 'Lagos',"
        " locality_confirmed_jurisdiction = 'NG-LA', locality_confirmed_at = :at,"
        " locality_confirmed_by_user_id = 'u-confirmer' WHERE id = 'geo-1'"
    ), {"at": datetime(2026, 11, 12, tzinfo=UTC)})

    db.alembic("downgrade", "-1")
    assert db.version() == [D1_REPAIR]
    assert db.columns(TABLE).keys() == columns_before.keys()
    assert db.dump() == before

    db.alembic("upgrade", "head")  # what was entered into the dropped columns is gone
    assert {k: v for k, v in db.dump()[TABLE][0] if k in NEW} == dict.fromkeys(NEW)


# --------------------------------------------------------------------------- #
# M-5 / M-6: the CHECKs and the account key, on the migrated database
# --------------------------------------------------------------------------- #
_SET = (
    "UPDATE geo_coverage_rules SET locality_confirmed_locality = :loc,"
    " locality_confirmed_city = :city, locality_confirmed_jurisdiction = :code,"
    " locality_confirmed_at = :at, locality_confirmed_by_user_id = :by WHERE id = 'geo-1'"
)
_AT = datetime(2026, 11, 12, tzinfo=UTC)
_FULL = {"loc": "Ikeja", "city": "Lagos", "code": "NG-LA", "at": _AT, "by": "u-confirmer"}


@pytest.mark.parametrize(
    "values",
    [
        {**_FULL, "code": None},
        {**_FULL, "loc": None},
        {**_FULL, "city": None},
        {**_FULL, "at": None},
        {"loc": None, "city": None, "code": "NG-LA", "at": None, "by": None},
        {**_FULL, "code": "UNRESOLVED"},
        {**_FULL, "code": "ng-la"},
        {**_FULL, "code": "NGLA"},
    ],
    ids=["no-code", "no-locality", "no-city", "no-instant", "code-alone", "sentinel",
         "lower-case", "no-hyphen"],
)
def test_the_checks_refuse_a_partial_binding_and_a_malformed_code(db: Db, values) -> None:
    _at_repair_seeded(db)
    db.alembic("upgrade", "head")
    # Refused by the database: the CHECK (IntegrityError), or, on PostgreSQL, the column's own
    # length for the 10-character sentinel (DataError) before the CHECK is evaluated.
    with pytest.raises((IntegrityError, DataError)):
        db.execute(sa.text(_SET), values)
    db.execute(sa.text(_SET), _FULL)  # positive control: a whole binding is accepted
    db.execute(sa.text(_SET), dict.fromkeys(_FULL))  # and a cleared one


def test_deleting_the_confirming_account_clears_only_its_reference(db: Db) -> None:
    _at_repair_seeded(db)
    db.alembic("upgrade", "head")
    db.execute(sa.text(_SET), _FULL)
    db.execute(sa.text("DELETE FROM users WHERE id = 'u-confirmer'"))
    row = dict(db.dump()[TABLE][0])
    assert row["locality_confirmed_by_user_id"] is None
    assert (row["locality_confirmed_locality"], row["locality_confirmed_city"],
            row["locality_confirmed_jurisdiction"]) == ("Ikeja", "Lagos", "NG-LA")
    assert row["locality_confirmed_at"] is not None
