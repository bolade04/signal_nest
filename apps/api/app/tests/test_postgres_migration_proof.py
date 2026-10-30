"""P6-CI-4: the real Alembic chain exercised on disposable PostgreSQL.

The CI migration gate ("Migrations and API contract") drives the chain on SQLite; the
AUTH4 database tests prove one revision's round trip on PostgreSQL. This module adds
the missing limbs on PostgreSQL, each against a database of its own (created and
dropped by :func:`pg_database`), never against a shared one:

* the **full chain**: empty database -> head (every revision), the schema-drift check
  (``alembic check``), a downgrade to **base** under the target-bound confirmation
  guard (the token is minted the way CI mints it, by ``app.db.migrate
  downgrade-confirmation``), and the re-upgrade — with the catalog asserted at head
  (every mapped table present) and at base (nothing but the version table);
* the **migration actor** (``python -m app.db.migrate``: the bare fail-closed
  upgrade-and-verify path and ``check``) against PostgreSQL;
* an **interrupted / failed migration**, in two forms that prove different things:

  1. *a tracked revision failing partway* — the real sessions revision
     (``87198ab57b59``, ``auth_sessions``, four below head) is run against a database
     that already holds a relation with its index's name, so ``CREATE TABLE`` succeeds
     and ``CREATE INDEX`` fails inside the same revision. This proves that PostgreSQL's
     transactional DDL (the revision's statements run inside one transaction, which
     rolls back as a whole) leaves **no partial object** of the failed revision and
     keeps ``alembic_version`` at the previous revision, and that the supported
     recovery (remove the cause, re-run the same upgrade) reaches head. The revision
     file is not edited. (``transaction_per_migration`` bounds each revision's
     transaction; for a single failing revision the atomicity shown here is the
     database's, as the adversarial review's toggle of that setting confirmed.)
  2. *a controlled failure fixture* — a disposable copy of the script tree gains one
     synthetic revision on top of head whose body creates a table and then raises.
     This proves the same atomicity for a crash **inside a migration body** that is
     not one of the tracked revisions, and that the retry after the fixture is
     repaired applies it exactly once; the fixture never enters the repository tree.

Seed idempotency is **not applicable** on PostgreSQL by design: ``app.db.seed`` refuses
every non-SQLite target (the seed target guard). The last test demonstrates that
refusal on the migrated PostgreSQL schema and that it writes nothing — the
PostgreSQL counterpart of the SQLite seed steps is a proven refusal, not a seed.

Gate: :func:`require_postgres` — with ``TEST_POSTGRES_URL`` unset these tests FAIL in
CI (``CI=true``) and skip only on a developer machine. An unreachable database fails
them (the connection error propagates). Migrated schemas come from Alembic only;
``metadata.create_all`` is never used here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.db.base import Base
from app.tests._auth2_support import pg_database, require_postgres

API_DIR = Path(__file__).resolve().parents[2]
#: The code head (D-25 cooperative-pause candidate, PROPOSED: adds
#: ``scout_requests.run_fence``).
HEAD = "b5e0c7a3d2f1"
#: One below head (D-LLM-1 option B: adds the ``llm_budget_periods`` and
#: ``llm_budget_reservations`` tables).
BELOW_HEAD = "8005724d811a"
#: Two below head (P6-DATA-1/PRIV-4: adds ``source_takedowns``).
TAKEDOWNS = "da25533622b2"
#: Two below head (P6-LLM-4: adds the ``llm_usage_events`` table).
USAGE = "47028898175f"
#: Three below head (P6-LLM-5: adds ``opportunities.explanation_source``).
EXPLANATION = "6ad2ff77f691"
#: The P6-AUTH-4 sessions revision (``auth_sessions``), the tracked revision that M2(a)
#: breaks midway.
SESSIONS = "87198ab57b59"
PREV = "a452ee007cc2"
BASE_REVISION = "9a7c614699d8"
FIXTURE_REVISION = "ffffffffffff"


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def pg_url() -> Iterator[str]:
    with pg_database("sn_ci4") as url:
        yield url


# --------------------------------------------------------------------------- #
# Helpers (subprocess: the chain runs exactly as CI runs it, in a clean process)
# --------------------------------------------------------------------------- #
def _run(args: list[str], url: str, *, ini: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env.pop("SN_MIGRATION_MODE", None)
    cmd = [sys.executable, "-m", "alembic"]
    if ini is not None:
        cmd += ["-c", str(ini)]
    return subprocess.run(cmd + args, cwd=API_DIR, env=env, capture_output=True, text=True)


def _alembic(url: str, *args: str, ini: Path | None = None) -> None:
    result = _run(list(args), url, ini=ini)
    assert result.returncode == 0, (args, result.stdout + result.stderr)


def _confirmation(url: str, target: str, *, ini: Path | None = None) -> str:
    """The downgrade confirmation the way CI obtains it: ``app.db.migrate``.

    With a disposable script tree the token must be bound to THAT tree's chain, so the
    fixture path computes it directly from the guard with the copied ScriptDirectory.
    """
    if ini is None:
        env = dict(os.environ)
        env["DATABASE_URL"] = url
        result = subprocess.run(
            [sys.executable, "-m", "app.db.migrate", "downgrade-confirmation", target],
            cwd=API_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.db.downgrade_guard import (
        chain_identity,
        confirmation_token,
        live_identity,
        resolve_downgrade,
    )

    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            heads = MigrationContext.configure(conn).get_current_heads()
            identity = live_identity(conn, url)
    finally:
        engine.dispose()
    assert len(heads) == 1
    script = ScriptDirectory.from_config(Config(str(ini)))
    resolved, chain = resolve_downgrade(script, heads[0], target)
    return confirmation_token(
        database_url=url,
        live_identity=identity,
        source_revision=heads[0],
        requested_target=target,
        resolved_destination=resolved,
        chain_identity=chain_identity(script, chain),
    )


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            )
            return {r[0] for r in rows}
    finally:
        engine.dispose()


def _version(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            present = text("SELECT to_regclass('alembic_version') IS NOT NULL")
            if not conn.execute(present).scalar_one():
                return []
            return list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    finally:
        engine.dispose()


def _columns(url: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            return {r[0] for r in rows}
    finally:
        engine.dispose()


def _relations(url: str) -> set[str]:
    """Every relation (table or index) in ``public`` — the partial-DDL witness."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'i')"
                )
            )
            return {r[0] for r in rows}
    finally:
        engine.dispose()


MAPPED_TABLES = frozenset(Base.metadata.tables)


def _logged(out: str, key: str, value: str) -> bool:
    """A structured log field in either emitted form: console ``key=value`` (development)
    or JSON ``"key": "value"`` (every other environment, including CI's ``test``)."""
    return f"{key}={value}" in out or f'"{key}": "{value}"' in out


# --------------------------------------------------------------------------- #
# M1 — the full chain: empty -> head, drift check, downgrade base, re-upgrade
# --------------------------------------------------------------------------- #
def test_full_chain_round_trips_on_postgresql_under_the_confirmation_guard(pg_url):
    """Empty -> head, drift check, one guarded step down, base under the guard, head again.

    The confirmation semantics asserted here are the guard's own: a token is bound to
    the live source revision, so the token that authorized ``-1`` from head is refused
    for the next ``-1`` from the revision below; ``base`` needs a token minted at that
    revision. After the full round trip (head again) the inputs of the original
    ``base`` token are all restored, so that token would be valid again — the guard
    module documents this re-arm as its known residual. It is shown here by
    recomputation (the two tokens compare equal), never by executing it.
    """
    assert _version(pg_url) == []  # a database of this test's own, empty

    _alembic(pg_url, "upgrade", "head")
    assert _version(pg_url) == [HEAD]
    assert _tables(pg_url) == MAPPED_TABLES | {"alembic_version"}  # no missing, no extra table
    _alembic(pg_url, "check")  # schema drift: the migrated schema matches the models

    # An unconfirmed downgrade is refused before any DDL (the guard, on PostgreSQL).
    refused = _run(["downgrade", "base"], pg_url)
    assert refused.returncode != 0
    assert "confirm" in (refused.stdout + refused.stderr)
    assert _version(pg_url) == [HEAD] and "llm_budget_reservations" in _tables(pg_url)
    assert "llm_budget_periods" in _tables(pg_url) and "source_takedowns" in _tables(pg_url)
    # D-9d: the suppression record carries no actor, reason or reference column.
    assert _columns(pg_url, "source_takedowns") == {
        "id", "source_key", "taken_down_at", "purge_summary", "repeat_purges",
        "created_at", "updated_at",
    }
    # D-9e: the operator's private reason lives in a separate case record (no retention
    # column: no duration is approved yet).
    assert _columns(pg_url, "source_takedown_cases") == {
        "id", "takedown_id", "source_key", "reason", "external_reference",
        "actor_user_id", "created_at",
    }
    assert "llm_usage_events" in _tables(pg_url)
    assert "explanation_source" in _columns(pg_url, "opportunities")
    assert "run_fence" in _columns(pg_url, "scout_requests")

    # One guarded step down undoes exactly the head revision; the spent token does not
    # authorize the next step.
    base_token_at_head = _confirmation(pg_url, "base")
    step_token = _confirmation(pg_url, "-1")
    _alembic(pg_url, "-x", f"confirm={step_token}", "downgrade", "-1")
    assert _version(pg_url) == [BELOW_HEAD]
    assert "run_fence" not in _columns(pg_url, "scout_requests")
    assert "scout_requests" in _tables(pg_url)
    # The revisions below head are untouched (the ledger tables of the revision below).
    assert "llm_budget_periods" in _tables(pg_url)
    assert "llm_budget_reservations" in _tables(pg_url)
    assert "source_takedowns" in _tables(pg_url)
    assert "source_takedown_cases" in _tables(pg_url)
    # The revisions below head are untouched.
    assert "llm_usage_events" in _tables(pg_url)
    assert "explanation_source" in _columns(pg_url, "opportunities")
    assert "auth_sessions" in _tables(pg_url)
    spent = _run(["-x", f"confirm={step_token}", "downgrade", "-1"], pg_url)
    assert spent.returncode != 0 and _version(pg_url) == [BELOW_HEAD]
    # The base token minted at head is likewise refused from the moved source.
    stale = _run(["-x", f"confirm={base_token_at_head}", "downgrade", "base"], pg_url)
    assert stale.returncode != 0 and _version(pg_url) == [BELOW_HEAD]

    # Base under the guard: minted at the live revision, every table destroyed.
    token = _confirmation(pg_url, "base")
    _alembic(pg_url, "-x", f"confirm={token}", "downgrade", "base")
    assert _version(pg_url) == []  # at base the version table is empty ...
    assert _tables(pg_url) == {"alembic_version"}  # ... and every mapped table is gone

    _alembic(pg_url, "upgrade", "head")
    assert _version(pg_url) == [HEAD]
    assert _tables(pg_url) == MAPPED_TABLES | {"alembic_version"}
    _alembic(pg_url, "check")
    # The documented residual, by recomputation only: the round trip restored every
    # input of the first base token, so it is valid again (see app.db.downgrade_guard).
    assert _confirmation(pg_url, "base") == base_token_at_head


# --------------------------------------------------------------------------- #
# M3 — the migration actor against PostgreSQL
# --------------------------------------------------------------------------- #
def test_migration_actor_upgrade_and_verify_then_check_on_postgresql(pg_url):
    env = dict(os.environ)
    env["DATABASE_URL"] = pg_url
    env.pop("SN_MIGRATION_MODE", None)
    bare = subprocess.run(
        [sys.executable, "-m", "app.db.migrate"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    out = bare.stdout + bare.stderr
    assert bare.returncode == 0, out
    assert "migrate.upgrade_verify.done" in out
    assert _logged(out, "outcome", "success")
    assert _logged(out, "db_revision", HEAD) and _logged(out, "code_head", HEAD)
    assert _version(pg_url) == [HEAD]
    assert pg_url not in out and "signalnest@" not in out  # the DSN never reaches the logs

    check = subprocess.run(
        [sys.executable, "-m", "app.db.migrate", "check"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    out = check.stdout + check.stderr
    assert check.returncode == 0, out
    assert "migrate.check" in out and _logged(out, "outcome", "compatible")

    # Re-running the actor on an already-migrated database is a no-op success.
    again = subprocess.run(
        [sys.executable, "-m", "app.db.migrate"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert again.returncode == 0 and _version(pg_url) == [HEAD]


# --------------------------------------------------------------------------- #
# M2(a) — a TRACKED revision failing partway: no partial object, version unmoved
# --------------------------------------------------------------------------- #
def test_tracked_revision_that_fails_midway_leaves_no_partial_schema_and_retries(pg_url):
    """The sessions revision (four below head) creates ``auth_sessions`` and then its
    index; the index's name is occupied in advance, so the revision fails AFTER its first
    DDL statement and the upgrade to head stops there."""
    _alembic(pg_url, "upgrade", PREV)
    assert _version(pg_url) == [PREV]
    before = _relations(pg_url)
    assert "auth_sessions" not in before

    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE ix_auth_sessions_user_id (occupied int)"))
    finally:
        engine.dispose()

    failed = _run(["upgrade", "head"], pg_url)
    assert failed.returncode != 0
    assert "ix_auth_sessions_user_id" in failed.stdout + failed.stderr
    # Interrupted state: the table created by the revision's FIRST statement is gone
    # (the revision's transaction rolled back as a whole) and the version is unmoved.
    assert _version(pg_url) == [PREV]
    assert _relations(pg_url) == before | {"ix_auth_sessions_user_id"}
    assert "auth_sessions" not in _relations(pg_url)
    # (``alembic check`` is not run here: at PREV the models legitimately differ from the DB.)

    # Supported recovery: remove the cause, re-run the same upgrade; nothing is stamped.
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE ix_auth_sessions_user_id"))
    finally:
        engine.dispose()
    _alembic(pg_url, "upgrade", "head")
    assert _version(pg_url) == [HEAD]
    assert {"auth_sessions", "ix_auth_sessions_user_id"} <= _relations(pg_url)
    assert _tables(pg_url) == MAPPED_TABLES | {"alembic_version"}
    _alembic(pg_url, "check")


# --------------------------------------------------------------------------- #
# M2(b) — a CONTROLLED failure fixture in a disposable script tree
# --------------------------------------------------------------------------- #
_FIXTURE_TEMPLATE = '''"""ci45 controlled failure fixture (disposable; never in the repository)."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "{rev}"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("ci45_probe", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("INSERT INTO ci45_probe (id) VALUES (1)")
{crash}

def downgrade() -> None:
    op.drop_table("ci45_probe")
'''


def _disposable_tree(tmp_path: Path, *, crash: bool) -> Path:
    """A copy of the real script tree plus one synthetic revision; returns its ini."""
    tree = tmp_path / "alembic"
    if tree.exists():
        shutil.rmtree(tree)
    shutil.copytree(API_DIR / "alembic", tree, ignore=shutil.ignore_patterns("__pycache__"))
    body = _FIXTURE_TEMPLATE.format(
        rev=FIXTURE_REVISION,
        down=HEAD,
        crash=(
            '    op.execute("SELECT 1/0")  # the crash, after two statements ran\n'
            if crash
            else ""
        ),
    )
    (tree / "versions" / f"99999999_9999-{FIXTURE_REVISION}_ci45_fixture.py").write_text(body)
    ini = tmp_path / "alembic.ini"
    original = (API_DIR / "alembic.ini").read_text().splitlines()
    rewritten = []
    for line in original:
        if line.startswith("script_location"):
            rewritten.append(f"script_location = {tree}")
        elif line.startswith("prepend_sys_path"):
            rewritten.append(f"prepend_sys_path = {API_DIR}")
        else:
            rewritten.append(line)
    ini.write_text("\n".join(rewritten) + "\n")
    return ini


def test_controlled_fixture_revision_crash_is_atomic_and_the_repaired_retry_applies_once(
    pg_url, tmp_path
):
    _alembic(pg_url, "upgrade", "head")  # the real chain first, from the real tree
    at_head = _relations(pg_url)

    crashing = _disposable_tree(tmp_path, crash=True)
    failed = _run(["upgrade", "head"], pg_url, ini=crashing)
    assert failed.returncode != 0
    assert "division by zero" in failed.stdout + failed.stderr
    # Interrupted state: the fixture's table and its row are gone, the version is unmoved.
    assert _version(pg_url) == [HEAD]
    assert _relations(pg_url) == at_head
    assert "ci45_probe" not in _tables(pg_url)

    # Recovery: repair the fixture, retry — applied exactly once (one row, one version).
    repaired = _disposable_tree(tmp_path, crash=False)
    _alembic(pg_url, "upgrade", "head", ini=repaired)
    assert _version(pg_url) == [FIXTURE_REVISION]
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM ci45_probe")).scalar_one() == 1
    finally:
        engine.dispose()
    _alembic(pg_url, "upgrade", "head", ini=repaired)  # idempotent at head
    assert _version(pg_url) == [FIXTURE_REVISION]

    # The fixture steps back down under the guard, bound to the disposable chain.
    token = _confirmation(pg_url, HEAD, ini=repaired)
    _alembic(pg_url, "-x", f"confirm={token}", "downgrade", HEAD, ini=repaired)
    assert _version(pg_url) == [HEAD]
    assert _relations(pg_url) == at_head
    _alembic(pg_url, "check")  # back on the real tree: no drift


# --------------------------------------------------------------------------- #
# M4 — seed idempotency is NOT APPLICABLE on PostgreSQL: the guard refuses, writes nothing
# --------------------------------------------------------------------------- #
def test_seed_refuses_the_migrated_postgresql_database_and_writes_nothing(pg_url):
    _alembic(pg_url, "upgrade", "head")
    env = dict(os.environ)
    env["DATABASE_URL"] = pg_url
    env.pop("SN_MIGRATION_MODE", None)
    for args in ([], ["--reset"]):
        result = subprocess.run(
            [sys.executable, "-m", "app.db.seed", *args],
            cwd=API_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (args, result.stdout + result.stderr)
        assert "Demo seeding is restricted to development/test SQLite targets" in (
            result.stdout + result.stderr
        )
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM organizations")).scalar_one() == 0
    finally:
        engine.dispose()
    assert _version(pg_url) == [HEAD]
