"""6B-4A: the account-token migration, its database invariants, and concurrency (P6-AUTH-2).

**Migration.** The single additive revision on top of ``3dc124a7dfd7`` (found by its parent,
not hard-coded, and required to be the only child and the single head) runs through the real
Alembic CLI: upgrade creates ``password_reset_tokens`` and ``email_verification_tokens`` with
exactly the contracted columns (no raw token, IP, user agent or tenant column), the digest
uniqueness constraints, the user indexes, the *partial* unique open-token indexes and the
``ON DELETE CASCADE`` user foreign keys, and adds ``users.email_verified_at`` (nullable) and
``users.auth_epoch`` (NOT NULL, server default 0). ``check`` reports no drift. Existing users
are not backfilled (FD-6). Downgrade one step restores the previous schema exactly -- every
table, every index, every ``users`` column -- keeps the business data, and re-upgrade
restores the tranche.

**Invariants on the migrated schema** (not on ``create_all``): one open token per user per
purpose with history kept, one row per digest, cascade on user deletion, no orphan tokens.

**Concurrency** (PostgreSQL only -- SQLite serializes writers and compiles ``FOR UPDATE``
away). Workers run the production service on their own sessions exactly as the routes do
(service, then commit), released together by a barrier, for several rounds: concurrent reset
requests mint exactly one token; concurrent confirms of one reset token have exactly one
winner, change the password once and move the epoch once; two open tokens of one account
(the open index removed to allow them) confirmed at once still yield one winner; the same for
verification requests and confirms; and the partial index holds under contention. Each
PostgreSQL test works in a database of its own, created and dropped around it.

**The database clock** (PostgreSQL only). The application host's clock is shifted a day
ahead or behind while PostgreSQL's ``now()`` is left alone, and each test reads ``SELECT
now()`` first in the service's own transaction -- the very reading the service takes. Minted
tokens are stamped ``created_at``/``updated_at`` with it and expire one TTL after it; the
cooldown and the daily-cap window are measured on it; and it, not the host, decides whether a
token has expired when it is claimed. Each test also asserts that the application's clock
would have decided otherwise, so a service reading the host's clock fails.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.auth import account_tokens
from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.core.config import get_settings
from app.core.errors import SignalNestError
from app.core.security import hash_password, verify_password
from app.db.models import Base
from app.organizations.models import User
from app.tests._auth2_support import (
    RESET_COLUMNS,
    RESET_OPEN_INDEX,
    VERIFICATION_COLUMNS,
    VERIFICATION_OPEN_INDEX,
    pg_database,
    reset_digest,
)

API_DIR = Path(__file__).resolve().parents[2]
PREV = "3dc124a7dfd7"
RESET, VERIFY = "password_reset_tokens", "email_verification_tokens"
TABLES = {RESET: RESET_COLUMNS, VERIFY: VERIFICATION_COLUMNS}
OPEN_INDEX = {RESET: RESET_OPEN_INDEX, VERIFY: VERIFICATION_OPEN_INDEX}
OPEN_PREDICATE = "WHERE used_at IS NULL AND revoked_at IS NULL"

_PG = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL test",
)


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))


def _this_revision() -> str:
    children = [r for r in _script().walk_revisions() if r.down_revision == PREV]
    assert len(children) == 1, [r.revision for r in children]
    return children[0].revision


THIS = _this_revision()
#: The code head: one additive revision later (P6-AUTH-4 sessions), the single head.
CODE_HEAD = "87198ab57b59"


# --------------------------------------------------------------------------- #
# Alembic plumbing (as the sibling migration tests do)
# --------------------------------------------------------------------------- #
def _downgrade_confirmation(url: str, args: tuple[str, ...]) -> list[str]:
    if not args or args[0] != "downgrade":
        return []
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
    if len(heads) != 1:
        return []
    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    resolved, chain = resolve_downgrade(script, heads[0], args[1])
    token = confirmation_token(
        database_url=url,
        live_identity=identity,
        source_revision=heads[0],
        requested_target=args[1],
        resolved_destination=resolved,
        chain_identity=chain_identity(script, chain),
    )
    return ["-x", f"confirm={token}"]


def _alembic_url(url: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *_downgrade_confirmation(url, args), *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return _alembic_url(f"sqlite:///{db_path}", *args)


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _master(db_path: Path, kind: str) -> dict[str, str | None]:
    con = sqlite3.connect(db_path)
    try:
        return dict(con.execute("SELECT name, sql FROM sqlite_master WHERE type = ?", (kind,)))
    finally:
        con.close()


def _columns(con: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """name -> (type, notnull, default, pk)."""
    return {r[1]: (r[2], r[3], r[4], r[5]) for r in con.execute(f"PRAGMA table_info({table})")}


def _schema(db_path: Path) -> dict:
    """Every table's columns and every table's indexes (name, unique, partial)."""
    con = sqlite3.connect(db_path)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            t: (
                _columns(con, t),
                # SQLite names a constraint's implicit index after the table it was
                # created under; a batch-recreated table renames it, so compare the rest.
                sorted(
                    (r[1], r[2], r[4])
                    for r in con.execute(f"PRAGMA index_list({t})")
                    if not r[1].startswith("sqlite_autoindex")
                ),
            )
            for t in sorted(tables)
            if t != "alembic_version"
        }
    finally:
        con.close()


@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "auth2-migration.db"


@pytest.fixture()
def migrated(db_path) -> Path:
    _ok(_alembic(db_path, "upgrade", "head"))
    con = _connect(db_path)
    try:
        con.executemany(
            "INSERT INTO users (id, email, full_name, hashed_password, is_active, is_operator)"
            " VALUES (?, ?, ?, 'x', 1, 0)",
            [("u-1", "one@example.com", "One"), ("u-2", "two@example.com", "Two")],
        )
        con.commit()
    finally:
        con.close()
    return db_path


def _insert(con, table: str, id_: str, *, user: str = "u-1", **kw) -> None:
    values = {
        "id": id_,
        "user_id": user,
        "token_hash": id_.rjust(64, "0"),
        "expires_at": "2099-01-01 00:00:00.000000",
        "used_at": None,
        "revoked_at": None,
    } | kw
    if table == VERIFY:
        values.setdefault("email_snapshot", "one@example.com")
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))


# --------------------------------------------------------------------------- #
# SQLite: migration lifecycle
# --------------------------------------------------------------------------- #
def test_this_revision_follows_invitations_and_precedes_the_single_head() -> None:
    script = _script()
    assert [r.revision for r in script.get_revisions("heads")] == [CODE_HEAD]
    assert script.get_revision(CODE_HEAD).down_revision == THIS
    revision = script.get_revision(THIS)
    assert revision.down_revision == PREV
    assert Path(revision.path).name.endswith("_add_password_reset_and_email_verification.py")


def test_cli_reports_one_head(db_path) -> None:
    result = _alembic(db_path, "heads")
    _ok(result)
    heads = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1 and CODE_HEAD in heads[0], result.stdout


def test_upgrade_creates_exactly_the_contracted_tables(migrated) -> None:
    tables, indexes = _master(migrated, "table"), _master(migrated, "index")
    con = _connect(migrated)
    try:
        for table, columns in TABLES.items():
            assert set(_columns(con, table)) == columns, table
            sql = " ".join((tables[table] or "").split())
            for fragment in (
                f"CONSTRAINT uq_{table}_token_hash UNIQUE (token_hash)",
                "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE",
            ):
                assert fragment in sql, (table, fragment)
            assert f"ix_{table}_user_id" in indexes
            open_sql = " ".join((indexes[OPEN_INDEX[table]] or "").split())
            assert open_sql.upper().startswith("CREATE UNIQUE INDEX"), open_sql
            assert f"ON {table} (user_id)" in open_sql and open_sql.endswith(OPEN_PREDICATE)
            notnull = {name: meta[1] for name, meta in _columns(con, table).items()}
            assert notnull["token_hash"] == notnull["expires_at"] == notnull["user_id"] == 1
            assert notnull["used_at"] == notnull["revoked_at"] == 0
        assert _columns(con, VERIFY)["email_snapshot"][1] == 1
        users = _columns(con, "users")
        assert users["email_verified_at"][1] == 0
        assert users["auth_epoch"][1] == 1 and users["auth_epoch"][2] in ("0", "'0'")
    finally:
        con.close()


def test_upgrade_leaves_no_model_drift(migrated) -> None:
    _ok(_alembic(migrated, "check"))


def test_existing_users_are_not_backfilled(db_path) -> None:
    """FD-6: an account from before the migration is unverified, at epoch 0."""
    _ok(_alembic(db_path, "upgrade", PREV))
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO users (id, email, full_name, hashed_password, is_active, is_operator)"
            " VALUES ('u-old', 'old@example.com', 'Old', 'x', 1, 0)"
        )
        con.commit()
    finally:
        con.close()
    _ok(_alembic(db_path, "upgrade", "head"))
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT email_verified_at, auth_epoch FROM users WHERE id = 'u-old'"
        ).fetchone()
        assert row == (None, 0)
        assert con.execute(f"SELECT count(*) FROM {RESET}").fetchone() == (0,)
        assert con.execute(f"SELECT count(*) FROM {VERIFY}").fetchone() == (0,)
    finally:
        con.close()


def test_downgrade_restores_the_previous_schema_exactly(db_path) -> None:
    _ok(_alembic(db_path, "upgrade", PREV))
    before = _schema(db_path)
    con = _connect(db_path)
    try:
        con.execute("INSERT INTO organizations (id, name, slug) VALUES ('org-1', 'One', 'one')")
        con.execute(
            "INSERT INTO users (id, email, full_name, hashed_password, is_active, is_operator)"
            " VALUES ('u-1', 'one@example.com', 'One', 'hash-1', 1, 0)"
        )
        con.commit()
    finally:
        con.close()
    _ok(_alembic(db_path, "upgrade", "head"))
    con = _connect(db_path)
    try:
        _insert(con, RESET, "r-1")
        _insert(con, VERIFY, "v-1")
        con.execute("UPDATE users SET auth_epoch = 3 WHERE id = 'u-1'")
        con.commit()
    finally:
        con.close()
    _ok(_alembic(db_path, "downgrade", PREV))
    assert _schema(db_path) == before
    con = sqlite3.connect(db_path)
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchall() == [(PREV,)]
        assert con.execute("SELECT id, email, hashed_password FROM users").fetchall() == [
            ("u-1", "one@example.com", "hash-1")
        ]
        assert con.execute("SELECT id FROM organizations").fetchall() == [("org-1",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()
    _ok(_alembic(db_path, "upgrade", "head"))
    assert set(TABLES) <= set(_master(db_path, "table"))
    _ok(_alembic(db_path, "check"))


# --------------------------------------------------------------------------- #
# SQLite: invariants on the migrated schema
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table", list(TABLES))
def test_one_open_token_per_user_with_history_kept(migrated, table: str) -> None:
    con = _connect(migrated)
    try:
        _insert(con, table, "t-1")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert(con, table, "t-2")
        con.execute(f"UPDATE {table} SET used_at = '2026-01-01 00:00:00' WHERE id = 't-1'")
        _insert(con, table, "t-3")
        con.execute(f"UPDATE {table} SET revoked_at = '2026-01-01 00:00:00' WHERE id = 't-3'")
        _insert(con, table, "t-4")
        _insert(con, table, "t-5", user="u-2")  # another user's open token is independent
        con.commit()
        open_rows = con.execute(
            f"SELECT id FROM {table} WHERE used_at IS NULL AND revoked_at IS NULL ORDER BY id"
        ).fetchall()
        assert open_rows == [("t-4",), ("t-5",)]
    finally:
        con.close()


@pytest.mark.parametrize("table", list(TABLES))
def test_one_row_per_digest(migrated, table: str) -> None:
    con = _connect(migrated)
    try:
        _insert(con, table, "t-1", token_hash="a" * 64)
        with pytest.raises(sqlite3.IntegrityError, match="token_hash"):
            _insert(con, table, "t-2", user="u-2", token_hash="a" * 64)
    finally:
        con.close()


@pytest.mark.parametrize("table", list(TABLES))
def test_tokens_die_with_their_user_and_need_one(migrated, table: str) -> None:
    con = _connect(migrated)
    try:
        _insert(con, table, "t-1", used_at="2026-01-01 00:00:00")
        _insert(con, table, "t-2")
        _insert(con, table, "t-3", user="u-2")
        con.commit()
        con.execute("DELETE FROM users WHERE id = 'u-1'")
        con.commit()
        assert con.execute(f"SELECT id FROM {table}").fetchall() == [("t-3",)]
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert(con, table, "t-4", user="u-missing")
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# PostgreSQL: lifecycle and invariants
# --------------------------------------------------------------------------- #
@_PG
def test_postgres_lifecycle() -> None:  # pragma: no cover - gated on live PG
    with pg_database("sn_a2m") as url:
        _ok(_alembic_url(url, "upgrade", PREV))
        engine = create_engine(url, future=True)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, hashed_password, is_active,"
                        " is_operator) VALUES ('u-old', 'old@example.com', 'Old', 'h', true,"
                        " false)"
                    )
                )
            before_columns = {c["name"] for c in inspect(engine).get_columns("users")}
            _ok(_alembic_url(url, "upgrade", "head"))
            _ok(_alembic_url(url, "check"))
            inspector = inspect(engine)
            for table, columns in TABLES.items():
                assert {c["name"] for c in inspector.get_columns(table)} == columns
            with engine.connect() as conn:
                for table, index in OPEN_INDEX.items():
                    indexdef = conn.execute(
                        text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": index}
                    ).scalar_one()
                    assert indexdef.startswith("CREATE UNIQUE INDEX"), indexdef
                    assert f"ON public.{table} USING btree (user_id)" in indexdef, indexdef
                    assert indexdef.endswith("WHERE ((used_at IS NULL) AND (revoked_at IS NULL))")
                epoch = conn.execute(
                    text(
                        "SELECT is_nullable, column_default FROM information_schema.columns"
                        " WHERE table_name = 'users' AND column_name = 'auth_epoch'"
                    )
                ).one()
                assert epoch[0] == "NO" and epoch[1] == "0"
                assert conn.execute(
                    text("SELECT email_verified_at, auth_epoch FROM users WHERE id = 'u-old'")
                ).one() == (None, 0)
            _ok(_alembic_url(url, "downgrade", PREV))
            inspector = inspect(engine)
            assert not set(TABLES) & set(inspector.get_table_names())
            assert {c["name"] for c in inspector.get_columns("users")} == before_columns
            with engine.connect() as conn:
                assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 1
            _ok(_alembic_url(url, "upgrade", "head"))
            assert set(TABLES) <= set(inspect(engine).get_table_names())
        finally:
            engine.dispose()


@_PG
def test_postgres_invariants() -> None:  # pragma: no cover - gated on live PG
    with pg_database("sn_a2i") as url:
        _ok(_alembic_url(url, "upgrade", "head"))
        engine = create_engine(url, future=True)

        def insert(conn, table, id_, user="u-1", th=None):
            snapshot = ", email_snapshot" if table == VERIFY else ""
            snapshot_value = ", 'one@example.com'" if table == VERIFY else ""
            conn.execute(
                text(
                    f"INSERT INTO {table} (id, user_id, token_hash, expires_at{snapshot})"
                    f" VALUES (:id, :u, :th, now() + interval '1 hour'{snapshot_value})"
                ),
                {"id": id_, "u": user, "th": th or id_.rjust(64, "0")},
            )

        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, hashed_password, is_active,"
                        " is_operator) VALUES ('u-1', 'one@example.com', 'One', 'h', true,"
                        " false), ('u-2', 'two@example.com', 'Two', 'h', true, false)"
                    )
                )
            for table in TABLES:
                with engine.begin() as conn:
                    insert(conn, table, "t-1")
                refused = {
                    "second-open": {},
                    "duplicate-digest": {"user": "u-2", "th": "t-1".rjust(64, "0")},
                    "no-user": {"user": "u-missing"},
                }
                for id_, kw in refused.items():
                    with pytest.raises(IntegrityError), engine.begin() as conn:
                        insert(conn, table, id_, **kw)
                with engine.begin() as conn:
                    conn.execute(text(f"UPDATE {table} SET revoked_at = now() WHERE id = 't-1'"))
                    insert(conn, table, "t-2")  # history kept
                    insert(conn, table, "t-3", user="u-2")
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE id = 'u-1'"))
                for table in TABLES:
                    left = conn.execute(text(f"SELECT id FROM {table}")).scalars().all()
                    assert left == ["t-3"], (table, left)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# PostgreSQL: concurrency
# --------------------------------------------------------------------------- #
ROUNDS = 5
WORKERS = 4
PASSWORD_HASH = hash_password("seeded-password-1")


@contextmanager
def _pg_factory() -> Iterator[sessionmaker]:  # pragma: no cover - gated on live PG
    with pg_database("sn_a2c") as url:
        engine = create_engine(
            url,
            future=True,
            pool_size=WORKERS + 2,
            # Bound every lock wait so a regression fails instead of hanging CI.
            connect_args={"options": "-c lock_timeout=15000"},
        )
        try:
            Base.metadata.create_all(engine)
            yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
        finally:
            engine.dispose()


def _new_user(factory) -> tuple[str, str]:  # pragma: no cover
    user_id = uuid.uuid4().hex
    email = f"racer-{user_id[:8]}@example.com"
    with factory() as s:
        s.add(User(id=user_id, email=email, full_name="Racer", hashed_password=PASSWORD_HASH))
        s.commit()
    return user_id, email


def _race(factory, calls: list[Callable]) -> list[str]:  # pragma: no cover - gated on live PG
    """Run each ``call(session)`` on its own session, released together; commit on success.

    Returns ``"ok"``, ``"none"`` (a request that minted nothing), a ``SignalNestError`` code,
    or ``"raised:<class>"`` for anything else -- which every test below treats as a failure.
    """
    barrier = threading.Barrier(len(calls), timeout=20)

    def run(call):
        s = factory()
        try:
            barrier.wait()
            result = call(s)
            s.commit()
            return "none" if result is None and getattr(call, "returns", False) else "ok"
        except SignalNestError as exc:
            s.rollback()
            return exc.code
        except Exception as exc:
            s.rollback()
            return f"raised:{type(exc).__name__}"
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def _returning(fn: Callable) -> Callable:
    fn.returns = True
    return fn


def _count(factory, model, user_id: str, *, open_only: bool = False) -> int:  # pragma: no cover
    stmt = select(func.count()).select_from(model).where(model.user_id == user_id)
    if open_only:
        stmt = stmt.where(model.used_at.is_(None), model.revoked_at.is_(None))
    with factory() as s:
        return s.scalar(stmt)


def _user(factory, user_id: str) -> User:  # pragma: no cover
    with factory() as s:
        return s.get(User, user_id)


@_PG
def test_concurrent_reset_requests_mint_exactly_one_token() -> None:  # pragma: no cover
    with _pg_factory() as factory:
        for _ in range(ROUNDS):
            user_id, email = _new_user(factory)

            @_returning
            def request(s, email=email):
                return account_tokens.request_password_reset(s, email=email)

            outcomes = _race(factory, [request] * WORKERS)
            # Every caller gets a normal outcome (the route answers 204 for each) ...
            assert set(outcomes) <= {"ok", "none"}, outcomes
            # ... and the burst mints one token: the rest fall inside its cooldown.
            assert outcomes.count("ok") == 1, outcomes
            assert _count(factory, PasswordResetToken, user_id) == 1
            assert _count(factory, PasswordResetToken, user_id, open_only=True) == 1


@_PG
def test_concurrent_confirms_of_one_reset_token_have_one_winner() -> None:  # pragma: no cover
    with _pg_factory() as factory:
        for _ in range(ROUNDS):
            user_id, email = _new_user(factory)
            with factory() as s:
                issued = account_tokens.request_password_reset(s, email=email)
                s.commit()
            passwords = [f"racing-password-{i}" for i in range(WORKERS)]

            def confirmer(password, raw=issued.raw_token):
                def confirm(s):
                    account_tokens.confirm_password_reset(s, token=raw, new_password=password)

                return confirm

            outcomes = _race(factory, [confirmer(p) for p in passwords])
            assert outcomes.count("ok") == 1, outcomes
            assert set(outcomes) - {"ok"} == {"password_reset_invalid"}, outcomes
            winner = passwords[outcomes.index("ok")]
            user = _user(factory, user_id)
            assert user.auth_epoch == 1
            assert verify_password(winner, user.hashed_password)
            assert [p for p in passwords if verify_password(p, user.hashed_password)] == [winner]


@_PG
def test_two_open_tokens_of_one_account_still_have_one_winner() -> None:  # pragma: no cover
    """The user-row lock and the revoke-the-rest step, isolated from the open index."""
    with _pg_factory() as factory:
        with factory() as s:
            s.execute(text(f"DROP INDEX {RESET_OPEN_INDEX}"))
            s.commit()
        for _ in range(ROUNDS):
            user_id, _email = _new_user(factory)
            raws = [f"two-open-{uuid.uuid4().hex}" for _ in range(2)]
            with factory() as s:
                for raw in raws:
                    s.execute(
                        PasswordResetToken.__table__.insert().values(
                            id=uuid.uuid4().hex,
                            user_id=user_id,
                            token_hash=reset_digest(raw),
                            expires_at=text("now() + interval '1 hour'"),
                        )
                    )
                s.commit()
            assert _count(factory, PasswordResetToken, user_id, open_only=True) == 2

            def confirmer(raw):
                def confirm(s):
                    account_tokens.confirm_password_reset(
                        s, token=raw, new_password=f"pw-{raw[-8:]}"
                    )

                return confirm

            outcomes = _race(factory, [confirmer(r) for r in raws])
            assert sorted(outcomes) == ["ok", "password_reset_invalid"], outcomes
            assert _user(factory, user_id).auth_epoch == 1
            assert _count(factory, PasswordResetToken, user_id, open_only=True) == 0


@_PG
def test_concurrent_verification_requests_mint_exactly_one_token() -> None:  # pragma: no cover
    with _pg_factory() as factory:
        for _ in range(ROUNDS):
            user_id, _email = _new_user(factory)

            @_returning
            def request(s, user_id=user_id):
                return account_tokens.request_email_verification(s, user=s.get(User, user_id))

            outcomes = _race(factory, [request] * WORKERS)
            assert set(outcomes) <= {"ok", "none"}, outcomes
            assert outcomes.count("ok") == 1, outcomes
            assert _count(factory, EmailVerificationToken, user_id) == 1


@_PG
def test_concurrent_verification_confirms_have_one_winner() -> None:  # pragma: no cover
    with _pg_factory() as factory:
        for _ in range(ROUNDS):
            user_id, _email = _new_user(factory)
            with factory() as s:
                issued = account_tokens.request_email_verification(s, user=s.get(User, user_id))
                s.commit()

            def confirm(s, raw=issued.raw_token, user_id=user_id):
                account_tokens.confirm_email_verification(s, user=s.get(User, user_id), token=raw)

            outcomes = _race(factory, [confirm] * WORKERS)
            assert outcomes.count("ok") == 1, outcomes
            assert set(outcomes) - {"ok"} == {"email_verification_invalid"}, outcomes
            assert _user(factory, user_id).email_verified_at is not None
            assert _count(factory, EmailVerificationToken, user_id, open_only=True) == 0


@_PG
@pytest.mark.parametrize("model", [PasswordResetToken, EmailVerificationToken])
def test_the_open_index_holds_under_contention(model) -> None:  # pragma: no cover
    with _pg_factory() as factory:
        user_id, email = _new_user(factory)
        engine = factory.kw["bind"]
        barrier = threading.Barrier(2, timeout=20)

        def insert(i):
            values = {
                "id": uuid.uuid4().hex,
                "user_id": user_id,
                "token_hash": str(i) * 64,
                "expires_at": text("now() + interval '1 hour'"),
            }
            if model is EmailVerificationToken:
                values["email_snapshot"] = email
            with engine.connect() as conn:
                trans = conn.begin()
                barrier.wait()
                try:
                    conn.execute(model.__table__.insert().values(**values))
                    trans.commit()
                    return "ok"
                except IntegrityError:
                    trans.rollback()
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, [1, 2])) == ["conflict", "ok"]


# --------------------------------------------------------------------------- #
# PostgreSQL: the database clock
# --------------------------------------------------------------------------- #
SKEW = timedelta(days=1)
DAY = timedelta(hours=24)


@contextmanager
def _app_clock_skewed(offset: timedelta) -> Iterator[None]:  # pragma: no cover
    """Shift the application host's clock by ``offset``; PostgreSQL's ``now()`` is untouched.

    Every loaded ``app`` module outside the tests that imported the ``datetime`` class sees a
    subclass whose ``now()`` is the real reading plus ``offset``.
    """
    real = datetime

    class Shifted(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + offset

    with pytest.MonkeyPatch.context() as mp:
        for name, module in list(sys.modules.items()):
            if (
                name.startswith("app.")
                and not name.startswith("app.tests.")
                and getattr(module, "datetime", None) is real
            ):
                mp.setattr(module, "datetime", Shifted)
        assert account_tokens.datetime is Shifted  # the service's module sees the skew
        yield


@_PG
@pytest.mark.parametrize("offset", [SKEW, -SKEW], ids=["app-ahead", "app-behind"])
@pytest.mark.parametrize("purpose", ["reset", "verification"])
def test_minted_tokens_are_stamped_by_the_database_clock(
    purpose, offset
) -> None:  # pragma: no cover
    settings = get_settings()
    if purpose == "reset":
        model = PasswordResetToken
        ttl = timedelta(minutes=settings.password_reset_token_ttl_minutes)
        assert ttl == timedelta(minutes=60)
    else:
        model = EmailVerificationToken
        ttl = timedelta(hours=settings.email_verification_token_ttl_hours)
        assert ttl == timedelta(hours=48)
    with _pg_factory() as factory:
        user_id, email = _new_user(factory)
        with _app_clock_skewed(offset), factory() as s:
            t_db = s.scalar(text("SELECT now()"))  # the transaction's start: the service's reading
            app_now = account_tokens.datetime.now(UTC)
            if purpose == "reset":
                issued = account_tokens.request_password_reset(s, email=email)
            else:
                issued = account_tokens.request_email_verification(s, user=s.get(User, user_id))
            s.commit()
        # The witness is live: the service's module read a clock a day away from the database's.
        assert abs(app_now - (t_db + offset)) < timedelta(minutes=1)
        assert issued is not None
        with factory() as s:
            row = s.get(model, issued.token_id)
        assert row.created_at == row.updated_at == t_db
        assert row.expires_at == t_db + ttl
        assert issued.expires_at == t_db + ttl


@_PG
def test_the_cooldown_is_measured_on_the_database_clock() -> None:  # pragma: no cover
    cooldown = timedelta(seconds=get_settings().auth_mail_cooldown_seconds)
    with _pg_factory() as factory:
        user_id, email = _new_user(factory)
        with factory() as s:
            first = account_tokens.request_password_reset(s, email=email)
            s.commit()
        assert first is not None
        with _app_clock_skewed(SKEW), factory() as s:
            t_db = s.scalar(text("SELECT now()"))
            app_now = account_tokens.datetime.now(UTC)
            again = account_tokens.request_password_reset(s, email=email)
            s.commit()
        with factory() as s:
            row = s.get(PasswordResetToken, first.token_id)
        # On the application's clock the cooldown ran out long ago; on the database's it has not.
        assert row.created_at < app_now - cooldown
        assert row.created_at > t_db - cooldown
        assert again is None
        assert _count(factory, PasswordResetToken, user_id) == 1
        assert row.used_at is None and row.revoked_at is None


@_PG
def test_the_daily_cap_window_is_measured_on_the_database_clock() -> None:  # pragma: no cover
    settings = get_settings()
    cap = settings.auth_mail_daily_cap
    cooldown = timedelta(seconds=settings.auth_mail_cooldown_seconds)
    # Seeded an hour apart, every token is inside the database's window and past the cooldown.
    assert cap < 24 and cooldown < timedelta(hours=1)
    with _pg_factory() as factory:
        user_id, email = _new_user(factory)
        with factory() as s:
            for hours in range(1, cap + 1):
                newest = hours == 1
                token_id = uuid.uuid4().hex
                minted = text(f"now() - interval '{hours} hours'")
                s.execute(
                    PasswordResetToken.__table__.insert().values(
                        id=token_id,
                        user_id=user_id,
                        token_hash=reset_digest(f"cap-{token_id}"),
                        created_at=minted,
                        updated_at=minted,
                        # Only the newest is open, as the open index requires.
                        expires_at=text("now() + interval '1 hour'") if newest else minted,
                        revoked_at=None if newest else minted,
                    )
                )
                if newest:
                    open_id = token_id
            s.commit()
        with factory() as s:
            seeded = s.scalars(
                select(PasswordResetToken.created_at).where(PasswordResetToken.user_id == user_id)
            ).all()
        assert len(seeded) == cap
        with _app_clock_skewed(SKEW), factory() as s:
            t_db = s.scalar(text("SELECT now()"))
            app_now = account_tokens.datetime.now(UTC)
            refused = account_tokens.request_password_reset(s, email=email)
            s.commit()
        # On the application's clock every seeded token has left the window; on the database's
        # none has, and none is inside the cooldown, so only the cap can refuse.
        assert all(created <= app_now - DAY for created in seeded)
        assert all(t_db - DAY < created < t_db - cooldown for created in seeded)
        assert refused is None
        assert _count(factory, PasswordResetToken, user_id) == cap
        with factory() as s:
            still_open = s.get(PasswordResetToken, open_id)
        assert still_open.used_at is None and still_open.revoked_at is None


@_PG
@pytest.mark.parametrize(
    ("expires_at", "offset", "expected"),
    [
        ("now() - interval '1 hour'", -SKEW, "password_reset_invalid"),
        ("now() + interval '1 hour'", SKEW, "ok"),
    ],
    ids=["expired-on-db", "live-on-db"],
)
def test_claim_expiry_is_decided_on_the_database_clock(
    expires_at, offset, expected
) -> None:  # pragma: no cover
    new_password = "clock-password-2"
    with _pg_factory() as factory:
        user_id, _email = _new_user(factory)
        raw, token_id = f"clock-{uuid.uuid4().hex}", uuid.uuid4().hex
        with factory() as s:
            s.execute(
                PasswordResetToken.__table__.insert().values(
                    id=token_id,
                    user_id=user_id,
                    token_hash=reset_digest(raw),
                    expires_at=text(expires_at),
                )
            )
            s.commit()
        with _app_clock_skewed(offset), factory() as s:
            t_db = s.scalar(text("SELECT now()"))
            app_now = account_tokens.datetime.now(UTC)
            try:
                account_tokens.confirm_password_reset(s, token=raw, new_password=new_password)
                s.commit()
                outcome = "ok"
            except SignalNestError as exc:
                s.rollback()
                outcome = exc.code
        with factory() as s:
            row = s.get(PasswordResetToken, token_id)
        user = _user(factory, user_id)
        # The application's clock would have decided the other way.
        assert (row.expires_at > app_now) is (expected != "ok")
        assert (row.expires_at > t_db) is (expected == "ok")
        assert outcome == expected
        if expected == "ok":
            assert row.used_at == t_db
            assert user.auth_epoch == 1
            assert verify_password(new_password, user.hashed_password)
        else:
            assert row.used_at is None
            assert user.auth_epoch == 0
            assert verify_password("seeded-password-1", user.hashed_password)  # _new_user's
