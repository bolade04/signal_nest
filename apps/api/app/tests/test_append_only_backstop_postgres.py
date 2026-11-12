"""D-N4 item 37: the PostgreSQL append-only backstop (Alembic ``d0a8bce8de6a``).

The operator's ruling (2026-11-12): "add the PostgreSQL backstop, retaining the application
guard. Cover applicable destructive SQL paths, including TRUNCATE. Keep the sanctioned
privacy-writer exception transaction-scoped and test that it cannot leak through pooled
connections. Document that this does not protect against a deliberate table owner."

What this module pins, on a database migrated by Alembic (never ``create_all``) unless it says
otherwise:

* **The catalog.** Exactly the tables the application guard covers carry the two triggers (a
  row-level ``BEFORE UPDATE OR DELETE`` and a statement-level ``BEFORE TRUNCATE``), enabled;
  no other table carries them. Statically, the revision's literal table list is the guard's set,
  and the revision and :mod:`app.privacy.writer` agree on the setting and the SQLSTATE.
* **Every guarded table refuses each destructive verb** (UPDATE, DELETE, TRUNCATE), with
  SQLSTATE ``SNA01``, and the row is unchanged afterwards.
* **Every path is refused**, not only the ORM ones the application guard knows: raw SQL on a
  bare connection and through a ``Session``; Core DML on the table (which the application guard
  lets through by design); an ORM statement executed on a bare ``Connection`` (which it never
  sees); a data-modifying CTE, as text and as Core; ``MERGE`` (UPDATE and DELETE actions); an
  upsert that hits a stored row; a no-net-change UPDATE; ``TRUNCATE <parent> CASCADE``; and the
  referential action (``ON DELETE CASCADE`` / ``SET NULL``) of deleting a parent row: an
  opportunity (as the source-takedown purge issues it), a campaign and a user. A plain INSERT,
  and ``ON CONFLICT DO NOTHING``, are not refused; an UPDATE that matches no row changes nothing
  and is not refused.
* **The application guard stays the first line**: an ORM update through a ``Session`` raises
  :class:`~app.db.append_only.AppendOnlyViolation` and no UPDATE reaches the database.
* **The sanctioned privacy writers still work** against the triggers, end to end: organization
  deletion and account deletion through the API, invitee erasure through the CLI, and the
  retention purge (the sweep, and the job's workspace batch). The helpers alone, without the
  mark, are refused in the same world (the negative control).
* **The exception is transaction-scoped**: it holds inside the marked transaction; it ends at
  COMMIT, at ROLLBACK and at a ROLLBACK TO a savepoint taken before the mark; a value that names
  another transaction (a stale id, ``'on'``) grants nothing.
* **It cannot leak through a pooled connection.** On a one-connection pool, after a real
  privacy-writer transaction has committed (and, separately, rolled back), the same backend
  (``pg_backend_pid()``) refuses a plain UPDATE, DELETE and TRUNCATE. The negative control shows
  that the probe can see a leak: a session-level value does survive into the next checkout, and
  even then the write is refused, because the value names an earlier transaction.
* **The migration proof**: below the revision there is no trigger and a write goes through;
  ``upgrade`` adds the backstop; a guarded ``downgrade -1`` removes the triggers and the
  function and no row; re-``upgrade`` restores it; ``alembic check`` stays clean.
* **SQLite keeps the application guard only**: the revision does nothing there.

Gate: :func:`~app.tests._auth2_support.require_postgres` on every PostgreSQL test (FAILS in CI
when ``TEST_POSTGRES_URL`` is unset; skips only on a developer machine). The static and SQLite
tests run everywhere.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import (
    Text,
    cast,
    create_engine,
    event,
    func,
    insert,
    literal_column,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.core.actor_snapshot import ERASED_ACTOR_SNAPSHOT, actor_snapshot
from app.core.config import Settings
from app.creative.models import ContentReview, CreativeDraft
from app.db.append_only import AppendOnlyViolation, is_append_only
from app.db.models import Base
from app.exports.models import Export
from app.organizations.models import User
from app.privacy import cli, erasure, retention
from app.privacy.inventory import RetentionClass
from app.privacy.verification import verify_organization_erased, verify_subject_erased
from app.privacy.writer import APPEND_ONLY_SQLSTATE, PRIVACY_WRITER_SETTING, mark_privacy_writer
from app.tests._auth2_support import pg_database, require_postgres
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    EMAIL,
    INVITEE,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    WORKSPACES,
    WS_A,
    WS_B,
    PrivacyEnv,
    _built,
)
from app.tests.test_cf1_append_only_bulk_paths import _seed
from app.tests.test_postgres_migration_proof import _alembic, _confirmation, _version

API_DIR = Path(__file__).resolve().parents[2]
#: This revision, and the one below it (5D-W1's campaign-location data repair).
REVISION = "d0a8bce8de6a"
BELOW = "d5f616c0ec97"
REVISION_FILE = (
    API_DIR
    / "alembic"
    / "versions"
    / ("20261112_1412-d0a8bce8de6a_append_only_backstop_triggers.py")
)
FUNCTION = "signalnest_append_only_backstop"
ROW_TRIGGER = "append_only_backstop_row"
TRUNCATE_TRIGGER = "append_only_backstop_truncate"
#: pg_trigger.tgtype bits: ROW 1, BEFORE 2, DELETE 8, UPDATE 16, TRUNCATE 32.
ROW_TGTYPE = 1 | 2 | 8 | 16
TRUNCATE_TGTYPE = 2 | 32

MODELS = (RecommendationBrief, Approval, AuditLog, CreativeDraft, ContentReview, Export)
TABLES = tuple(m.__tablename__ for m in MODELS)
BY_TABLE = {m.__tablename__: m for m in MODELS}
#: One literal UPDATE per table that really changes a column (a value of the column's type).
UPDATES = {
    "recommendation_briefs": "UPDATE recommendation_briefs SET summary = 'changed' WHERE id = :id",
    "approvals": "UPDATE approvals SET note = 'changed' WHERE id = :id",
    "audit_logs": "UPDATE audit_logs SET reason = 'changed' WHERE id = :id",
    "creative_drafts": "UPDATE creative_drafts SET body = 'changed' WHERE id = :id",
    "content_reviews": (
        "UPDATE content_reviews SET warnings = CAST('[\"changed\"]' AS json) WHERE id = :id"
    ),
    "exports": "UPDATE exports SET exported_by_snapshot = 'changed' WHERE id = :id",
}
DELETES = {t: f"DELETE FROM {t} WHERE id = :id" for t in TABLES}
TRUNCATES = {t: f"TRUNCATE TABLE {t} CASCADE" for t in TABLES}
VERBS = {"UPDATE": UPDATES, "DELETE": DELETES, "TRUNCATE": TRUNCATES}


def _guarded_tables() -> set[str]:
    """The application guard's set: every mapped class decorated with ``@append_only``."""
    return {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if is_append_only(mapper.class_)
    }


def _revision_module():
    spec = importlib.util.spec_from_file_location("backstop_revision", REVISION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refusal(exc: DBAPIError, verb: str | None = None, table: str | None = None) -> None:
    """``exc`` is the backstop's refusal (SQLSTATE and fixed message), naming ``verb`` on
    ``table`` when given."""
    orig = exc.orig
    assert getattr(orig, "sqlstate", None) == APPEND_ONLY_SQLSTATE, repr(orig)
    message = orig.diag.message_primary
    assert message.startswith("append-only: "), message
    assert message.endswith(" is refused outside a sanctioned privacy-writer transaction")
    if verb is not None and table is not None:
        assert message == (
            f"append-only: {verb} on {table} is refused outside a sanctioned privacy-writer"
            " transaction"
        ), message
    elif verb is not None:
        assert message.startswith(f"append-only: {verb} on "), message


@contextmanager
def _refused(verb: str | None = None, table: str | None = None) -> Iterator[None]:
    with pytest.raises(DBAPIError) as caught:
        yield
    _refusal(caught.value, verb, table)


def _stored(world: PrivacyEnv, ids: dict[type, str]) -> dict[str, list[dict]]:
    """The committed rows of ``ids`` (one per guarded table), read through the witness."""
    return {m.__tablename__: world.rows(m, m.__table__.c.id == ids[m]) for m in MODELS}


@contextmanager
def _migrated_world(prefix: str) -> Iterator[PrivacyEnv]:
    """The P6-PRIV-1 world in a PostgreSQL database of its own whose schema comes from Alembic
    (``upgrade head``). ``_built``'s ``create_all`` then creates nothing: every table exists."""
    require_postgres()
    with pg_database(prefix) as url:
        _alembic(url, "upgrade", "head")
        assert _version(url) == [REVISION]
        with _built(create_engine(url, future=True), create_engine(url, future=True)) as env:
            assert _backstop(env.engine) == _expected_catalog()
            yield env


def _backstop(engine) -> set[tuple[str, str, int, str]]:
    """(table, trigger, tgtype, tgenabled) of every trigger that runs the backstop function."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, t.tgname, t.tgtype, t.tgenabled FROM pg_trigger t"
                " JOIN pg_class c ON c.oid = t.tgrelid"
                " WHERE NOT t.tgisinternal AND t.tgfoid = to_regprocedure(:fn)"
            ),
            {"fn": f"{FUNCTION}()"},
        ).all()
    return {(r[0], r[1], int(r[2]), r[3]) for r in rows}


def _expected_catalog() -> set[tuple[str, str, int, str]]:
    return {(t, ROW_TRIGGER, ROW_TGTYPE, "O") for t in TABLES} | {
        (t, TRUNCATE_TRIGGER, TRUNCATE_TGTYPE, "O") for t in TABLES
    }


def _function_exists(engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT to_regprocedure(:fn) IS NOT NULL"), {"fn": f"{FUNCTION}()"}
        ).scalar_one()


@pytest.fixture(scope="module")
def world() -> Iterator[PrivacyEnv]:
    with _migrated_world("sn_bs") as env:
        yield env


@pytest.fixture
def fresh() -> Iterator[PrivacyEnv]:
    """A migrated world of this test's own (the privacy writers change it)."""
    with _migrated_world("sn_bsw") as env:
        yield env


# --------------------------------------------------------------------------- #
# Static: the revision, the guard and the writer agree (no database)
# --------------------------------------------------------------------------- #
def test_the_revision_covers_exactly_the_guarded_tables() -> None:
    revision = _revision_module()
    assert revision.revision == REVISION and revision.down_revision == BELOW
    assert set(revision.GUARDED_TABLES) == _guarded_tables() == set(TABLES)
    assert len(revision.GUARDED_TABLES) == len(set(revision.GUARDED_TABLES))
    for table in revision.GUARDED_TABLES:
        assert (
            revision.CREATE_TRIGGERS.count(
                f"CREATE TRIGGER {ROW_TRIGGER} BEFORE UPDATE OR DELETE ON {table}"
                f" FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
            )
            == 1
        ), table
        assert (
            revision.CREATE_TRIGGERS.count(
                f"CREATE TRIGGER {TRUNCATE_TRIGGER} BEFORE TRUNCATE ON {table}"
                f" FOR EACH STATEMENT EXECUTE FUNCTION {FUNCTION}()"
            )
            == 1
        ), table
        assert revision.DROP_TRIGGERS.count(f"DROP TRIGGER {ROW_TRIGGER} ON {table}") == 1
        assert revision.DROP_TRIGGERS.count(f"DROP TRIGGER {TRUNCATE_TRIGGER} ON {table}") == 1
    assert len(revision.CREATE_TRIGGERS) == len(revision.DROP_TRIGGERS) == 2 * len(TABLES)
    assert revision.DROP_FUNCTION == f"DROP FUNCTION {FUNCTION}()"


def test_the_revision_and_the_writer_agree_on_the_setting_and_the_sqlstate() -> None:
    revision = _revision_module()
    assert revision.PRIVACY_WRITER_SETTING == PRIVACY_WRITER_SETTING == "signalnest.privacy_writer"
    assert revision.APPEND_ONLY_SQLSTATE == APPEND_ONLY_SQLSTATE == "SNA01"
    body = revision.CREATE_FUNCTION
    assert f"pg_catalog.current_setting('{PRIVACY_WRITER_SETTING}', true)" in body
    assert "CAST(pg_catalog.pg_current_xact_id() AS text)" in body
    assert f"ERRCODE = '{APPEND_ONLY_SQLSTATE}'" in body
    assert f"CREATE FUNCTION {FUNCTION}() RETURNS trigger" in body


#: The only production functions that may mark a transaction (module, function).
SANCTIONED_WRITERS = {
    ("app/privacy/erasure.py", "erase_organization"),
    ("app/privacy/erasure.py", "delete_account"),
    ("app/privacy/erasure.py", "erase_invitee_email"),
    ("app/privacy/retention.py", "purge_batch"),
}


def _production_modules() -> Iterator[tuple[str, ast.Module, str]]:
    root = API_DIR / "app"
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(API_DIR).as_posix()
        if rel.startswith("app/tests/"):
            continue
        source = path.read_text(encoding="utf-8")
        yield rel, ast.parse(source), source


def test_only_the_four_sanctioned_writers_mark_their_transaction() -> None:
    callers: set[tuple[str, str]] = set()
    for rel, tree, _ in _production_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = getattr(inner.func, "id", None) or getattr(inner.func, "attr", None)
                    if name == "mark_privacy_writer":
                        callers.add((rel, node.name))
    assert callers == SANCTIONED_WRITERS


def test_only_the_writer_module_names_the_setting() -> None:
    """The setting is named (as its literal or its constant) by :mod:`app.privacy.writer` only,
    so nothing else can set the mark; and no production statement disables or drops a trigger
    or sets ``session_replication_role`` (which would skip the backstop; docstrings may name
    them). ``set_config`` itself is used
    elsewhere for other, transaction-scoped settings (``app/jobs/blocked_signal.py``)."""
    naming, bypassing = set(), set()
    for rel, tree, source in _production_modules():
        if PRIVACY_WRITER_SETTING in source:
            naming.add(rel)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if getattr(node, "id", None) == "PRIVACY_WRITER_SETTING":
                naming.add(rel)
            if getattr(node, "attr", None) == "PRIVACY_WRITER_SETTING":
                naming.add(rel)
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings  # documentation may name the bypasses
            ):
                lowered = " ".join(node.value.lower().split())
                if any(
                    phrase in lowered
                    for phrase in ("session_replication_role", "disable trigger", "drop trigger")
                ):
                    bypassing.add(rel)
    assert naming == {"app/privacy/writer.py"}
    assert bypassing == set()


# --------------------------------------------------------------------------- #
# SQLite (local mode): the application guard only
# --------------------------------------------------------------------------- #
def test_sqlite_keeps_the_application_guard_only(tmp_path) -> None:
    db_file = tmp_path / "backstop.db"
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_file}"
    env.pop("SN_MIGRATION_MODE", None)
    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert up.returncode == 0, up.stdout + up.stderr
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchall() == [(REVISION,)]
        triggers = conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        assert triggers.fetchall() == []
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(AuditLog.__table__).values(
                    id="sq-log",
                    organization_id="org",
                    action="auth.register",
                    context={},
                    reason="original",
                )
            )
        with Session(engine) as s:
            mark_privacy_writer(s)  # nothing to mark on SQLite: no statement, no transaction
            assert not s.in_transaction()
            log = s.get(AuditLog, "sq-log")
            log.reason = "changed"
            with pytest.raises(AppendOnlyViolation):
                s.flush()  # the application guard, the only control on SQLite
            s.rollback()
        with engine.begin() as conn:  # no database-level control: raw SQL goes through
            conn.execute(text("UPDATE audit_logs SET reason = 'raw' WHERE id = 'sq-log'"))
            assert (
                conn.execute(text("SELECT reason FROM audit_logs WHERE id = 'sq-log'")).scalar_one()
                == "raw"
            )
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# PostgreSQL: the catalog
# --------------------------------------------------------------------------- #
def test_the_backstop_is_on_exactly_the_guarded_tables(world) -> None:
    assert {t for t, *_ in _backstop(world.engine)} == _guarded_tables()
    assert _backstop(world.engine) == _expected_catalog()
    assert _function_exists(world.engine)


# --------------------------------------------------------------------------- #
# PostgreSQL: every guarded table refuses each destructive verb
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("verb", list(VERBS))
@pytest.mark.parametrize("table", TABLES)
def test_each_guarded_table_refuses_each_destructive_verb(world, table, verb) -> None:
    ids = _seed(world)
    before = _stored(world, ids)
    with world.engine.connect() as conn:  # raw SQL on a bare connection
        with _refused(verb, table):
            conn.execute(text(VERBS[verb][table]), {"id": ids[BY_TABLE[table]]})
        conn.rollback()
    assert _stored(world, ids) == before
    assert all(len(rows) == 1 for rows in before.values())


# --------------------------------------------------------------------------- #
# PostgreSQL: every path, not only the ORM forms the application guard knows
# --------------------------------------------------------------------------- #
def _session_paths(ids):
    log, appr = ids[AuditLog], ids[Approval]
    table = AuditLog.__table__
    return {
        "raw SQL through a Session": lambda s: s.execute(text(UPDATES["audit_logs"]), {"id": log}),
        "Core UPDATE on the table": lambda s: s.execute(
            update(table).where(table.c.id == log).values(reason="changed")
        ),
        "Core DELETE on the table": lambda s: s.execute(table.delete().where(table.c.id == log)),
        "data-modifying CTE (text)": lambda s: s.execute(
            text(
                "WITH d AS (DELETE FROM audit_logs WHERE id = :id RETURNING id)"
                " SELECT count(*) FROM d"
            ),
            {"id": log},
        ).all(),
        "data-modifying CTE (Core)": lambda s: s.execute(
            select(literal_column("1")).add_cte(
                update(table)
                .where(table.c.id == log)
                .values(reason="changed")
                .returning(table.c.id)
                .cte("u")
            )
        ).all(),
        "MERGE … UPDATE": lambda s: s.execute(
            text(
                "MERGE INTO approvals a USING (SELECT CAST(:id AS varchar) AS id) src"
                " ON a.id = src.id WHEN MATCHED THEN UPDATE SET note = 'changed'"
            ),
            {"id": appr},
        ),
        "MERGE … DELETE": lambda s: s.execute(
            text(
                "MERGE INTO approvals a USING (SELECT CAST(:id AS varchar) AS id) src"
                " ON a.id = src.id WHEN MATCHED THEN DELETE"
            ),
            {"id": appr},
        ),
        "upsert hitting a stored row": lambda s: s.execute(
            pg_insert(table)
            .values(id=log, organization_id=ORG_A, action="auth.register", context={})
            .on_conflict_do_update(index_elements=["id"], set_={"reason": "changed"})
        ),
        "no-net-change UPDATE": lambda s: s.execute(
            text("UPDATE audit_logs SET reason = reason WHERE id = :id"), {"id": log}
        ),
    }


PATHS = list(_session_paths({AuditLog: "x", Approval: "y"}))
#: Brief versions for briefs this module adds on the seeded opportunity (unique per workspace).
_versions = itertools.count(5000)


@pytest.mark.parametrize("path", PATHS)
def test_every_session_path_is_refused(world, path) -> None:
    ids = _seed(world)
    before = _stored(world, ids)
    with world.session() as s:
        with _refused():
            _session_paths(ids)[path](s)
        s.rollback()
    assert _stored(world, ids) == before


def test_an_orm_statement_on_a_bare_connection_is_refused(world) -> None:
    """The application guard listens on the ``Session``; a bare ``Connection`` never reaches it,
    so before the backstop this ORM statement changed the row."""
    ids = _seed(world)
    before = _stored(world, ids)
    with world.engine.connect() as conn:
        with _refused("UPDATE", "approvals"):
            conn.execute(update(Approval).where(Approval.id == ids[Approval]).values(note="x"))
        conn.rollback()
    assert _stored(world, ids) == before


def test_truncating_a_parent_with_cascade_is_refused(world) -> None:
    _seed(world)
    before = world.snapshot()
    with world.engine.connect() as conn:
        with _refused("TRUNCATE"):
            conn.execute(text("TRUNCATE TABLE organizations CASCADE"))
        conn.rollback()
    assert world.snapshot() == before


def test_a_parent_deletion_cascading_into_guarded_rows_is_refused(world) -> None:
    """The referential actions: deleting an opportunity (its feedback first, as the
    source-takedown purge does, ``app/connectors/takedown.py``) cascades into its briefs and
    drafts; deleting a campaign nulls ``campaign_id`` on a brief; deleting a user nulls the
    actor columns. Each is a write to a guarded row, refused outside a privacy-writer
    transaction, and nothing changes."""
    ids = _seed(world)
    brief_id = f"{ids[RecommendationBrief]}-c"
    with world.engine.begin() as conn:
        conn.execute(
            insert(User.__table__).values(
                id="bs-user",
                email="bs-user@backstop.example",
                full_name="BS",
                hashed_password="x",
            )
        )
        conn.execute(
            insert(RecommendationBrief.__table__).values(
                id=brief_id,
                organization_id=ORG_A,
                workspace_id=WS_A,
                opportunity_id="a1-opp",
                intelligence_record_id="a1-intel",
                campaign_id="a1-campaign",
                brief_version=next(_versions),
                analysis_version="1",
                scoring_version="1",
                prompt_name="recommendation_brief",
                prompt_version="1.0.0",
                model_identifier="mock",
                input_hash="0" * 64,
                is_simulated=True,
                created_by="bs-user",
                created_by_snapshot="BS",
                summary="s",
                rationale="r",
                recommended_actions=[],
                suggested_angles=[],
                risk_notes=[],
                evidence_refs=[],
            )
        )
    before = world.snapshot()
    probes = (
        (
            "DELETE FROM opportunity_feedback WHERE opportunity_id = 'a1-opp';"
            " DELETE FROM opportunities WHERE id = 'a1-opp'",
            "DELETE",
        ),
        ("DELETE FROM campaigns WHERE id = 'a1-campaign'", "UPDATE"),
        ("DELETE FROM users WHERE id = 'bs-user'", "UPDATE"),
    )
    for statement, verb in probes:
        with world.engine.connect() as conn:
            with _refused(verb):
                for part in statement.split(";"):
                    conn.execute(text(part))
            conn.rollback()
        assert world.snapshot() == before, statement


def test_inserts_and_unmatched_updates_are_not_refused(world) -> None:
    """Append-only means insert-only: a plain INSERT and ``ON CONFLICT DO NOTHING`` go
    through; a statement that matches no row changes nothing and fires no row trigger."""
    ids = _seed(world)  # the seed itself is plain INSERTs through Core
    table = AuditLog.__table__
    before = _stored(world, ids)
    with world.session() as s:
        s.execute(
            pg_insert(table)
            .values(id=ids[AuditLog], organization_id=ORG_A, action="x", context={})
            .on_conflict_do_nothing(index_elements=["id"])
        )
        assert (
            s.execute(text("UPDATE audit_logs SET reason = 'x' WHERE id = 'no-such-row'")).rowcount
            == 0
        )
        assert s.execute(text("DELETE FROM approvals WHERE false")).rowcount == 0
        s.commit()
    assert _stored(world, ids) == before


def test_the_application_guard_stays_the_first_line(world) -> None:
    """An ORM update through a ``Session`` is refused by the application guard before any
    UPDATE is sent: the database never sees it, so the error is not the trigger's."""
    ids = _seed(world)
    sent: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        sent.append(statement)

    event.listen(world.engine, "before_cursor_execute", record)
    try:
        with world.session() as s:
            log = s.get(AuditLog, ids[AuditLog])
            log.reason = "changed"
            with pytest.raises(AppendOnlyViolation):
                s.flush()
            s.rollback()
            with pytest.raises(AppendOnlyViolation):
                s.execute(update(AuditLog).where(AuditLog.id == ids[AuditLog]).values(reason="y"))
            s.rollback()
    finally:
        event.remove(world.engine, "before_cursor_execute", record)
    assert not [q for q in sent if q.lstrip().upper().startswith("UPDATE")], sent


# --------------------------------------------------------------------------- #
# PostgreSQL: the exception is transaction-scoped
# --------------------------------------------------------------------------- #
def _setting(s: Session) -> str | None:
    return s.execute(select(func.current_setting(PRIVACY_WRITER_SETTING, True))).scalar_one()


def test_the_exception_holds_inside_the_marked_transaction_only(world) -> None:
    ids = _seed(world)
    before = _stored(world, ids)
    with world.session() as s:
        mark_privacy_writer(s)
        xact = s.execute(select(cast(func.pg_current_xact_id(), Text))).scalar_one()
        assert _setting(s) == xact  # the value names this transaction
        for table in TABLES:  # inside the marked transaction every verb goes through
            s.execute(text(UPDATES[table]), {"id": ids[BY_TABLE[table]]})
        s.execute(text("TRUNCATE TABLE audit_logs"))
        s.rollback()  # ... and ROLLBACK ends it
        assert _setting(s) in ("", None)
        with _refused("UPDATE", "approvals"):
            s.execute(text(UPDATES["approvals"]), {"id": ids[Approval]})
        s.rollback()
    assert _stored(world, ids) == before


def test_the_exception_ends_at_commit(world) -> None:
    ids = _seed(world)
    with world.session() as s:
        mark_privacy_writer(s)
        s.execute(text(UPDATES["audit_logs"]), {"id": ids[AuditLog]})
        s.commit()
        assert _setting(s) in ("", None)  # a new transaction: the value is gone
        with _refused("DELETE", "audit_logs"):
            s.execute(text(DELETES["audit_logs"]), {"id": ids[AuditLog]})
        s.rollback()
    assert world.rows(AuditLog, AuditLog.id == ids[AuditLog])[0]["reason"] == "changed"


def test_the_exception_ends_at_a_rollback_to_a_savepoint_taken_before_it(world) -> None:
    ids = _seed(world)
    before = _stored(world, ids)
    with world.session() as s:
        s.execute(select(1))
        savepoint = s.begin_nested()
        mark_privacy_writer(s)
        s.execute(text(UPDATES["exports"]), {"id": ids[Export]})
        savepoint.rollback()
        assert _setting(s) in ("", None)
        with _refused("UPDATE", "exports"):
            s.execute(text(UPDATES["exports"]), {"id": ids[Export]})
        s.rollback()
    assert _stored(world, ids) == before


@pytest.mark.parametrize("value", ["on", "true", "1", "stale"])
def test_a_value_naming_another_transaction_grants_nothing(world, value) -> None:
    ids = _seed(world)
    before = _stored(world, ids)
    with world.session() as s:
        if value == "stale":  # an earlier transaction's id, as a leaked value would carry
            mark_privacy_writer(s)
            value = _setting(s)
            s.rollback()
        s.execute(select(func.set_config(PRIVACY_WRITER_SETTING, value, True)))
        assert _setting(s) == value
        with _refused("DELETE", "approvals"):
            s.execute(text(DELETES["approvals"]), {"id": ids[Approval]})
        s.rollback()
    assert _stored(world, ids) == before


# --------------------------------------------------------------------------- #
# PostgreSQL: the exception cannot leak through a pooled connection
# --------------------------------------------------------------------------- #
@contextmanager
def _one_connection_pool(world: PrivacyEnv) -> Iterator[sessionmaker]:
    pooled = create_engine(
        world.engine.url, poolclass=QueuePool, pool_size=1, max_overflow=0, future=True
    )
    try:
        yield sessionmaker(bind=pooled, autoflush=False, expire_on_commit=False, future=True)
    finally:
        pooled.dispose()


def _backend(s: Session) -> int:
    return s.execute(select(func.pg_backend_pid())).scalar_one()


def _plain_writes_are_refused(factory: sessionmaker, pid: int, ids: dict[type, str]) -> None:
    """On a new checkout of the one pooled connection (the same backend), a plain UPDATE,
    DELETE and TRUNCATE are each refused."""
    for verb, table in (
        ("UPDATE", "approvals"),
        ("DELETE", "exports"),
        ("TRUNCATE", "content_reviews"),
    ):
        with factory() as s:
            assert _backend(s) == pid  # the very connection the privacy writer used
            assert _setting(s) in ("", None)
            with _refused(verb, table):
                s.execute(text(VERBS[verb][table]), {"id": ids[BY_TABLE[table]]})
            s.rollback()


@pytest.mark.parametrize("ending", ["commit", "rollback"])
def test_the_exception_cannot_leak_through_a_pooled_connection(world, ending) -> None:
    ids = _seed(world)
    with world.engine.begin() as conn:  # a value naming the invitee, for the writer to clear
        conn.execute(
            insert(AuditLog.__table__).values(
                id=f"{ids[AuditLog]}-inv",
                organization_id=ORG_A,
                action="x",
                new_state={"email": INVITEE},
                context={},
            )
        )
    before = _stored(world, ids)
    with _one_connection_pool(world) as factory:
        with factory() as s:
            pid = _backend(s)
            counts = erasure.erase_invitee_email(s, email=INVITEE)  # a real privacy writer
            assert counts.anonymised["audit_logs"] >= 1
            assert _setting(s) not in ("", None)  # marked, inside its transaction
            if ending == "commit":
                s.commit()
            else:
                s.rollback()
        _plain_writes_are_refused(factory, pid, ids)
    assert _stored(world, ids) == before
    anonymised = world.rows(AuditLog, AuditLog.id == f"{ids[AuditLog]}-inv")[0]["new_state"]
    assert anonymised == ({"email": "[erased]"} if ending == "commit" else {"email": INVITEE})


def test_the_pool_probe_can_see_a_leak_and_the_transaction_binding_still_refuses(world) -> None:
    """Negative control: a **session-level** value (``set_config(…, false)``, what a writer
    without ``is_local`` would leave) does survive into the next checkout of the pooled
    connection, so the probe above would see a leak. The write is still refused, because the
    value names an earlier transaction."""
    ids = _seed(world)
    before = _stored(world, ids)
    with _one_connection_pool(world) as factory:
        with factory() as s:
            pid = _backend(s)
            s.execute(
                select(
                    func.set_config(
                        PRIVACY_WRITER_SETTING, cast(func.pg_current_xact_id(), Text), False
                    )
                )
            )
            leaked = _setting(s)
            s.commit()
        with factory() as s:
            assert _backend(s) == pid
            assert _setting(s) == leaked  # it leaked ...
            with _refused("UPDATE", "approvals"):  # ... and grants nothing
                s.execute(text(UPDATES["approvals"]), {"id": ids[Approval]})
            s.rollback()
            s.execute(text("RESET signalnest.privacy_writer"))
            s.commit()
    assert _stored(world, ids) == before


# --------------------------------------------------------------------------- #
# PostgreSQL: the sanctioned privacy writers still work against the triggers
# --------------------------------------------------------------------------- #
def _snapshot_of(user_id: str) -> str:
    return actor_snapshot(user_id=user_id, full_name=f"Name {user_id}", email=EMAIL[user_id])


def _phase5_rows_by(env: PrivacyEnv, author: str, *, tag: str) -> dict[type, str]:
    """One row per guarded table in organization A authored by ``author`` (Core inserts)."""
    ids = {
        RecommendationBrief: f"{tag}-brief",
        Approval: f"{tag}-appr",
        AuditLog: f"{tag}-log",
        CreativeDraft: f"{tag}-draft",
        ContentReview: f"{tag}-review",
        Export: f"{tag}-export",
    }
    snapshot = _snapshot_of(author)
    with env.engine.begin() as conn:
        conn.execute(
            insert(RecommendationBrief.__table__).values(
                id=ids[RecommendationBrief],
                organization_id=ORG_A,
                workspace_id=WS_A,
                opportunity_id="a1-opp",
                intelligence_record_id="a1-intel",
                brief_version=1,
                analysis_version="1",
                scoring_version="1",
                prompt_name="recommendation_brief",
                prompt_version="1.0.0",
                model_identifier="mock",
                input_hash="0" * 64,
                is_simulated=True,
                created_by=author,
                created_by_snapshot=snapshot,
                summary="s",
                rationale="r",
                recommended_actions=[],
                suggested_angles=[],
                risk_notes=[],
                evidence_refs=[],
            )
        )
        conn.execute(
            insert(CreativeDraft.__table__).values(
                id=ids[CreativeDraft],
                organization_id=ORG_A,
                workspace_id=WS_A,
                brief_id=ids[RecommendationBrief],
                opportunity_id="a1-opp",
                draft_version=1,
                channel="email",
                body="b",
                prompt_name="generate_creative",
                prompt_version="1.0.0",
                model_identifier="mock",
                input_hash="0" * 64,
                is_simulated=True,
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd=0,
                created_by=author,
                created_by_snapshot=snapshot,
            )
        )
        conn.execute(
            insert(ContentReview.__table__).values(
                id=ids[ContentReview],
                organization_id=ORG_A,
                workspace_id=WS_A,
                draft_id=ids[CreativeDraft],
                draft_version=1,
                risk_level="low",
                is_blocked=False,
                findings=[],
                warnings=[],
                blocked_claim_ids=[],
                required_disclaimers=[],
                disclaimers_resolved=True,
                passed=True,
            )
        )
        conn.execute(
            insert(Approval.__table__).values(
                id=ids[Approval],
                organization_id=ORG_A,
                workspace_id=WS_A,
                subject_type="creative_draft",
                subject_id=ids[CreativeDraft],
                subject_version=1,
                draft_id=ids[CreativeDraft],
                decision="approved",
                decision_seq=1,
                note=f"Checked with {EMAIL[author]}",
                decided_by=author,
                decided_by_snapshot=snapshot,
                decided_role="reviewer",
                is_simulated=False,
            )
        )
        conn.execute(
            insert(Export.__table__).values(
                id=ids[Export],
                organization_id=ORG_A,
                workspace_id=WS_A,
                draft_id=ids[CreativeDraft],
                draft_version=1,
                approval_id=ids[Approval],
                format="text/plain",
                jurisdiction_code="GB",
                exported_by=author,
                exported_by_snapshot=snapshot,
                is_simulated=False,
            )
        )
        conn.execute(
            insert(AuditLog.__table__).values(
                id=ids[AuditLog],
                organization_id=ORG_A,
                workspace_id=WS_A,
                actor_user_id=author,
                action="creative_draft.approved",
                entity_type="creative_draft",
                entity_id=ids[CreativeDraft],
                reason=f"Asked by {EMAIL[author]}",
                context={},
            )
        )
    return ids


def test_the_helpers_alone_are_refused_in_the_migrated_world(fresh) -> None:
    """Negative control for the writer tests below: in the same kind of world, the
    anonymisation and the walk without the mark are refused (the triggers are live)."""
    _phase5_rows_by(fresh, CAROL, tag="nc")
    before = fresh.snapshot()
    for helper in (
        lambda s: erasure.anonymise_organization_audit(s, organization_id=ORG_A),
        lambda s: erasure.anonymise_organization_approvals(s, organization_id=ORG_A),
        lambda s: erasure.anonymise_organization_exports(s, organization_id=ORG_A),
        lambda s: erasure.anonymise_phase5_records_for_subject(
            s, user_id=CAROL, email=EMAIL[CAROL]
        ),
        lambda s: erasure.delete_rows(s, RecommendationBrief.__table__, ["nc-brief"]),
        lambda s: erasure.delete_rows(s, Approval.__table__, ["nc-appr"]),
    ):
        with fresh.session() as s:
            with _refused():
                helper(s)
            s.rollback()
    assert fresh.snapshot() == before


def test_organization_deletion_works_against_the_triggers(fresh) -> None:
    ids = _phase5_rows_by(fresh, CAROL, tag="od")
    b_rows = fresh.snapshot(lambda table, row: row.get("organization_id") == ORG_B)

    r = fresh.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=fresh.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["counts"]["anonymised"]["approvals"] == 1
    assert receipt["counts"]["anonymised"]["exports"] == 1
    assert receipt["counts"]["anonymised"]["audit_logs"] >= 1
    for model in (RecommendationBrief, CreativeDraft, ContentReview):  # deleted with A
        assert receipt["counts"]["deleted"][model.__tablename__] == 1
        assert fresh.rows(model, model.__table__.c.id == ids[model]) == []
    approval = fresh.rows(Approval, Approval.id == ids[Approval])[0]  # retained (O-6)
    assert (approval["decided_by"], approval["note"], approval["draft_id"]) == (None, None, None)
    assert approval["decided_by_snapshot"] == ERASED_ACTOR_SNAPSHOT
    export = fresh.rows(Export, Export.id == ids[Export])[0]
    assert (export["exported_by"], export["draft_id"]) == (None, None)
    assert fresh.snapshot(lambda table, row: row.get("organization_id") == ORG_B) == b_rows
    with fresh.session() as s:
        report = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert report.verified, report.as_dict()


def test_account_deletion_works_against_the_triggers(fresh) -> None:
    ids = _phase5_rows_by(fresh, CAROL, tag="ad")

    r = fresh.client.post(
        f"{API}/account/delete",
        headers=fresh.auth(CAROL),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    anonymised = r.json()["counts"]["anonymised"]
    for table in ("recommendation_briefs", "creative_drafts", "approvals", "exports"):
        assert anonymised[table] == 1, anonymised
    assert anonymised["audit_logs"] >= 1
    brief = fresh.rows(RecommendationBrief, RecommendationBrief.id == ids[RecommendationBrief])
    assert (brief[0]["created_by"], brief[0]["created_by_snapshot"]) == (
        None,
        ERASED_ACTOR_SNAPSHOT,
    )
    approval = fresh.rows(Approval, Approval.id == ids[Approval])[0]
    assert (approval["decided_by"], approval["decided_by_snapshot"], approval["note"]) == (
        None,
        ERASED_ACTOR_SNAPSHOT,
        None,
    )
    log = fresh.rows(AuditLog, AuditLog.id == ids[AuditLog])[0]
    assert (log["actor_user_id"], log["reason"]) == (None, None)
    with fresh.session() as s:
        report = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert report.verified, report.as_dict()


def test_invitee_erasure_through_the_cli_works_against_the_triggers(fresh, capsys) -> None:
    with fresh.engine.begin() as conn:
        conn.execute(
            insert(Approval.__table__).values(
                id="ie-appr",
                organization_id=ORG_B,
                workspace_id=WS_B,
                subject_type="recommendation_brief",
                subject_id="x",
                subject_version=1,
                decision="approved",
                decision_seq=1,
                note=f"Spoke to {INVITEE}",
                decided_by_snapshot=_snapshot_of(BOB),
                decided_role="reviewer",
                is_simulated=False,
            )
        )

    assert cli.main(["erase-email", "--email", INVITEE, "--confirm"]) == 0, capsys.readouterr()

    assert fresh.rows(Approval, Approval.id == "ie-appr")[0]["note"] is None
    for row in fresh.rows(AuditLog):
        assert INVITEE not in str(row), row


def test_the_retention_purge_works_against_the_triggers(fresh) -> None:
    """The sweep (platform class ``audit_logs``: audit records, approvals and exports) and
    the job's workspace batch (``opportunities``: the walk deletes the briefs, drafts and
    reviews that hang off an expired opportunity)."""
    ids = _phase5_rows_by(fresh, CAROL, tag="rp")
    old = datetime.now(UTC) - timedelta(days=400)
    with fresh.engine.begin() as conn:  # expired compliance rows, inserted as such
        conn.execute(
            insert(AuditLog.__table__).values(
                id="rp-old-log",
                organization_id=ORG_B,
                action="x",
                context={},
                created_at=old,
            )
        )
        conn.execute(
            insert(Approval.__table__).values(
                id="rp-old-appr",
                organization_id=ORG_B,
                workspace_id=WS_B,
                subject_type="recommendation_brief",
                subject_id="x",
                subject_version=1,
                decision="approved",
                decision_seq=1,
                decided_by_snapshot="x",
                decided_role="reviewer",
                is_simulated=False,
                created_at=old,
            )
        )
    settings = Settings(_env_file=None, retention_audit_logs_days=365)
    factory = sessionmaker(bind=fresh.engine, autoflush=False, expire_on_commit=False)

    (outcome,) = retention.sweep(factory, classes=(RetentionClass.AUDIT_LOGS,), settings=settings)

    assert outcome.status == "purged" and outcome.complete, outcome
    assert outcome.deleted == {"approvals": 1, "audit_logs": 1}
    assert fresh.rows(AuditLog, AuditLog.id == "rp-old-log") == []
    assert fresh.rows(Approval, Approval.id == "rp-old-appr") == []
    assert fresh.rows(Approval, Approval.id == ids[Approval])  # newer: kept

    with fresh.session() as s:  # the job's batch: one workspace, the opportunities class
        batch = retention.purge_batch(
            s,
            RetentionClass.OPPORTUNITIES,
            datetime.now(UTC) - timedelta(days=30),
            batch_size=500,
            workspace_id=WS_A,
        )
        s.commit()
    for model in (RecommendationBrief, CreativeDraft, ContentReview):
        assert batch.counts.deleted[model.__tablename__] == 1, batch.counts.as_dict()
        assert fresh.rows(model, model.__table__.c.id == ids[model]) == []
    approval = fresh.rows(Approval, Approval.id == ids[Approval])[0]
    assert approval["draft_id"] is None  # the walk's SET NULL, under the mark


# --------------------------------------------------------------------------- #
# PostgreSQL: the migration proof (upgrade, guarded downgrade, re-upgrade)
# --------------------------------------------------------------------------- #
def test_the_revision_upgrades_downgrades_and_upgrades_again_on_postgresql() -> None:
    require_postgres()
    with pg_database("sn_bsm") as url:
        engine = create_engine(url, future=True)
        try:
            _alembic(url, "upgrade", BELOW)
            assert _version(url) == [BELOW]
            assert _backstop(engine) == set() and not _function_exists(engine)
            with engine.begin() as conn:
                conn.execute(
                    insert(AuditLog.__table__).values(
                        id="mp-log",
                        organization_id="org",
                        action="x",
                        context={},
                        reason="a",
                    )
                )
                conn.execute(text(UPDATES["audit_logs"]), {"id": "mp-log"})  # no backstop yet

            _alembic(url, "upgrade", "head")
            assert _version(url) == [REVISION]
            assert _backstop(engine) == _expected_catalog() and _function_exists(engine)
            with engine.connect() as conn:
                with _refused("UPDATE", "audit_logs"):
                    conn.execute(text(UPDATES["audit_logs"]), {"id": "mp-log"})
                conn.rollback()
            _alembic(url, "check")

            unconfirmed = subprocess.run(
                [sys.executable, "-m", "alembic", "downgrade", "-1"],
                cwd=API_DIR,
                env={**os.environ, "DATABASE_URL": url},
                capture_output=True,
                text=True,
            )
            assert unconfirmed.returncode != 0 and _version(url) == [REVISION]
            token = _confirmation(url, "-1")
            _alembic(url, "-x", f"confirm={token}", "downgrade", "-1")
            assert _version(url) == [BELOW]
            assert _backstop(engine) == set() and not _function_exists(engine)
            with engine.begin() as conn:
                assert conn.execute(text("SELECT count(*) FROM audit_logs")).scalar_one() == 1
                conn.execute(text(DELETES["audit_logs"]), {"id": "mp-log"})  # no backstop

            _alembic(url, "upgrade", "head")
            assert _version(url) == [REVISION]
            assert _backstop(engine) == _expected_catalog()
            _alembic(url, "check")
        finally:
            engine.dispose()
