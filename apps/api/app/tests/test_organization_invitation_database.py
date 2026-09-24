"""6B-3A: the ``organization_invitations`` migration, its database invariants, and concurrency.

Three parts, all about what the database itself guarantees (P6-AUTH-1).

**Migration.** The single additive migration ``3dc124a7dfd7`` (down ``98289430a3ec``) through
the real Alembic CLI, as ``test_workspace_capability_override_migration.py`` does for its
predecessor: upgrade creates the table, its organization index, the token-digest uniqueness
constraint, the role CHECK and the *partial* unique pending index with its ``WHERE``; ``check``
reports no drift; the head is single; downgrade one step drops only the new table and keeps
business data; re-upgrade restores it.

**Invariants on the migrated schema** (not on ``create_all``): the closed role vocabulary, one
digest per token, at most one open invitation per (organization, email) with accepted and
revoked history kept, organization deletion cascading, and inviter/acceptor deletion nulling
the reference.

**Concurrency.** The one-time claim, the pending-uniqueness index and the organization-row
lock only behave faithfully on PostgreSQL -- SQLite serializes writers and compiles
``FOR UPDATE`` away. Workers run on their own sessions, call the production service exactly as
the route does (service, then ``commit``), are released together by a barrier, and repeat each
race for several rounds so a lucky interleaving cannot pass: two acceptances of one invitation,
two invited registrations with one token, duplicate creation, two OWNERs demoting or removing
each other, the lock observably blocking a second mutation, and the partial index under
contention.

Everything runs on SQLite. The ``TEST_POSTGRES_URL``-gated tests repeat the lifecycle and the
invariants on PostgreSQL and run the races; CI sets that variable. Each PostgreSQL test works in
a database of its own, created for it and dropped afterwards (``_pg_database``), so no run
collides with another or with the tests that use the shared database. A dedicated database
rather than a schema: Alembic takes the URL through ``configparser``, and the downgrade guard
refuses a connection ``options`` parameter by design, so a ``search_path`` cannot be passed.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.auth.dependencies import OrganizationContext
from app.core.enums import Role
from app.core.errors import SignalNestError
from app.db.models import Base
from app.organizations import invitations as inv
from app.organizations import members
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
)

API_DIR = Path(__file__).resolve().parents[2]
PREV = "98289430a3ec"
HEAD = "3dc124a7dfd7"
TABLE = "organization_invitations"
PENDING_INDEX = "uq_organization_invitations_pending"
ORG_INDEX = "ix_organization_invitations_organization_id"

_PG = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL test",
)


# --------------------------------------------------------------------------- #
# Alembic plumbing
# --------------------------------------------------------------------------- #
def _downgrade_confirmation(url: str, args: tuple[str, ...]) -> list[str]:
    """Mint the downgrade guard's confirmation for this database (as the sibling tests do)."""
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


@contextmanager
def _pg_database() -> Iterator[str]:  # pragma: no cover - gated on live PG
    """A PostgreSQL database of this test's own: created here, dropped on the way out."""
    base = make_url(os.environ["TEST_POSTGRES_URL"])
    name = f"sn_inv_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


# --------------------------------------------------------------------------- #
# SQLite: migration lifecycle
# --------------------------------------------------------------------------- #
def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _master(db_path: Path, kind: str) -> dict[str, str | None]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type = ?", (kind,))
        return dict(rows.fetchall())
    finally:
        con.close()


@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "migration.db"


@pytest.fixture()
def migrated(db_path) -> Path:
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    con = _connect(db_path)
    try:
        con.executemany(
            "INSERT INTO organizations (id, name, slug) VALUES (?, ?, ?)",
            [("org-1", "One", "one"), ("org-2", "Two", "two")],
        )
        con.executemany(
            "INSERT INTO users (id, email, full_name, hashed_password, is_active, is_operator)"
            " VALUES (?, ?, ?, 'x', 1, 0)",
            [
                ("u-inviter", "inviter@example.com", "I"),
                ("u-acceptor", "acceptor@example.com", "A"),
            ],
        )
        con.commit()
    finally:
        con.close()
    return db_path


def _insert(
    con,
    id_: str,
    *,
    org: str = "org-1",
    email: str = "a@example.com",
    role: str = "viewer",
    token_hash: str | None = None,
    accepted_at: str | None = None,
    revoked_at: str | None = None,
    invited_by: str | None = "u-inviter",
    accepted_by: str | None = None,
) -> None:
    con.execute(
        "INSERT INTO organization_invitations (id, organization_id, email, role, token_hash,"
        " invited_by_user_id, expires_at, accepted_at, accepted_by_user_id, revoked_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '2099-01-01 00:00:00.000000', ?, ?, ?)",
        (
            id_,
            org,
            email,
            role,
            token_hash or id_.rjust(64, "0"),
            invited_by,
            accepted_at,
            accepted_by,
            revoked_at,
        ),
    )


def test_upgrade_creates_table_indexes_and_constraints(migrated) -> None:
    assert TABLE in _master(migrated, "table")
    indexes = _master(migrated, "index")
    assert ORG_INDEX in indexes
    pending_sql = " ".join((indexes[PENDING_INDEX] or "").split())
    assert pending_sql.upper().startswith("CREATE UNIQUE INDEX")
    assert "(organization_id, email)" in pending_sql
    assert pending_sql.endswith("WHERE accepted_at IS NULL AND revoked_at IS NULL")
    table_sql = " ".join(_master(migrated, "table")[TABLE].split())
    for fragment in (
        "CONSTRAINT uq_organization_invitations_token_hash UNIQUE (token_hash)",
        "CONSTRAINT ck_organization_invitations_role CHECK",
        "FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE",
        "FOREIGN KEY(invited_by_user_id) REFERENCES users (id) ON DELETE SET NULL",
        "FOREIGN KEY(accepted_by_user_id) REFERENCES users (id) ON DELETE SET NULL",
    ):
        assert fragment in table_sql, fragment


def test_upgrade_leaves_no_model_drift(migrated) -> None:
    result = _alembic(migrated, "check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_single_head_is_this_migration(db_path) -> None:
    result = _alembic(db_path, "heads")
    assert result.returncode == 0, result.stderr
    heads = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert len(heads) == 1 and HEAD in heads[0], result.stdout


def test_revision_chain() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert script.get_revision(HEAD).down_revision == PREV
    assert [r.revision for r in script.get_revisions("heads")] == [HEAD]


def test_check_literal_matches_the_role_vocabulary() -> None:
    """The model derives the CHECK from ``Role``; the migration carries it literally."""
    [path] = (API_DIR / "alembic" / "versions").glob(f"*{HEAD}*.py")
    values = ", ".join(f"'{v}'" for v in sorted(r.value for r in Role))
    assert f"role IN ({values})" in path.read_text(encoding="utf-8")


def test_downgrade_is_surgical_and_reupgrade_restores(migrated) -> None:
    con = _connect(migrated)
    try:
        _insert(con, "inv-1")
        con.commit()
    finally:
        con.close()
    result = _alembic(migrated, "downgrade", PREV)
    assert result.returncode == 0, result.stderr
    tables, indexes = _master(migrated, "table"), _master(migrated, "index")
    assert TABLE not in tables and PENDING_INDEX not in indexes and ORG_INDEX not in indexes
    assert {
        "organizations",
        "users",
        "organization_members",
        "workspace_capability_overrides",
    } <= set(tables)
    con = sqlite3.connect(migrated)
    try:
        assert con.execute("SELECT count(*) FROM organizations").fetchone() == (2,)
        assert con.execute("SELECT version_num FROM alembic_version").fetchall() == [(PREV,)]
    finally:
        con.close()
    assert _alembic(migrated, "upgrade", "head").returncode == 0
    assert TABLE in _master(migrated, "table") and PENDING_INDEX in _master(migrated, "index")
    assert _alembic(migrated, "check").returncode == 0


# --------------------------------------------------------------------------- #
# SQLite: invariants on the migrated schema
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", [r.value for r in Role])
def test_check_admits_each_role(migrated, role) -> None:
    """The CHECK is the closed vocabulary; that OWNER is never *invited* is the service's rule."""
    con = _connect(migrated)
    try:
        _insert(con, f"inv-{role[:3]}", email=f"{role}@example.com", role=role)
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize("role", ["superuser", "OWNER", "", "member"])
def test_check_rejects_outside_the_vocabulary(migrated, role) -> None:
    con = _connect(migrated)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(con, "inv-bad", role=role)
    finally:
        con.close()


def test_token_hash_is_unique(migrated) -> None:
    con = _connect(migrated)
    try:
        _insert(con, "inv-1", email="one@example.com", token_hash="a" * 64)
        with pytest.raises(sqlite3.IntegrityError, match="token_hash"):
            _insert(con, "inv-2", email="two@example.com", token_hash="a" * 64)
    finally:
        con.close()


def test_partial_unique_pending_index(migrated) -> None:
    con = _connect(migrated)
    try:
        _insert(con, "inv-1")
        # A second OPEN row for the same (organization, email) is refused ...
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            _insert(con, "inv-2")
        # ... but history is kept: once accepted or revoked, a new open row is allowed.
        con.execute(
            "UPDATE organization_invitations SET accepted_at = '2026-01-01 00:00:00'"
            " WHERE id = 'inv-1'"
        )
        _insert(con, "inv-3")
        con.execute(
            "UPDATE organization_invitations SET revoked_at = '2026-01-01 00:00:00'"
            " WHERE id = 'inv-3'"
        )
        _insert(con, "inv-4")
        # Same email in another organization; and a case variant (exact-match semantics).
        _insert(con, "inv-5", org="org-2")
        _insert(con, "inv-6", email="A@example.com")
        con.commit()
        open_rows = con.execute(
            "SELECT id FROM organization_invitations"
            " WHERE accepted_at IS NULL AND revoked_at IS NULL ORDER BY id"
        ).fetchall()
        assert open_rows == [("inv-4",), ("inv-5",), ("inv-6",)]
    finally:
        con.close()


def test_foreign_key_actions(migrated) -> None:
    con = _connect(migrated)
    try:
        _insert(con, "inv-1", accepted_at="2026-01-01 00:00:00", accepted_by="u-acceptor")
        _insert(con, "inv-2", org="org-2", email="b@example.com")
        con.commit()
        con.execute("DELETE FROM users WHERE id IN ('u-inviter', 'u-acceptor')")
        con.commit()
        assert con.execute(
            "SELECT id, invited_by_user_id, accepted_by_user_id FROM organization_invitations"
            " ORDER BY id"
        ).fetchall() == [("inv-1", None, None), ("inv-2", None, None)]
        con.execute("DELETE FROM organizations WHERE id = 'org-1'")
        con.commit()
        assert con.execute("SELECT id FROM organization_invitations").fetchall() == [("inv-2",)]
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert(con, "inv-3", org="org-missing")
    finally:
        con.close()


def test_organization_lock_compiles_to_for_update_on_postgresql() -> None:
    """The lock the concurrency tests below rely on, proven without a live database."""
    sql = str(members._organization_lock_select("org").compile(dialect=postgresql.dialect()))
    assert sql.rstrip().endswith("FOR UPDATE"), sql


# --------------------------------------------------------------------------- #
# PostgreSQL: lifecycle and invariants
# --------------------------------------------------------------------------- #
@_PG
def test_postgres_lifecycle() -> None:  # pragma: no cover - gated on live PG
    with _pg_database() as url:
        assert _alembic_url(url, "upgrade", "head").returncode == 0
        check = _alembic_url(url, "check")
        assert check.returncode == 0, check.stdout + check.stderr
        engine = create_engine(url, future=True)
        try:
            with engine.connect() as conn:
                indexdef = conn.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                    {"n": PENDING_INDEX},
                ).scalar_one()
            assert "CREATE UNIQUE INDEX" in indexdef
            assert indexdef.endswith("WHERE ((accepted_at IS NULL) AND (revoked_at IS NULL))")
            downgrade = _alembic_url(url, "downgrade", PREV)
            assert downgrade.returncode == 0, downgrade.stderr
            assert TABLE not in inspect(engine).get_table_names()
            assert "organizations" in inspect(engine).get_table_names()
            assert _alembic_url(url, "upgrade", "head").returncode == 0
            assert TABLE in inspect(engine).get_table_names()
        finally:
            engine.dispose()


@_PG
def test_postgres_invariants() -> None:  # pragma: no cover - gated on live PG
    with _pg_database() as url:
        assert _alembic_url(url, "upgrade", "head").returncode == 0
        engine = create_engine(url, future=True)

        def insert(conn, id_, **kw):
            params = {
                "id": id_,
                "org": "org-1",
                "email": "a@example.com",
                "role": "viewer",
                "th": id_.rjust(64, "0"),
                "inv": "u-inviter",
                "acc_by": None,
            } | kw
            conn.execute(
                text(
                    "INSERT INTO organization_invitations (id, organization_id, email, role,"
                    " token_hash, invited_by_user_id, expires_at, accepted_by_user_id) VALUES"
                    " (:id, :org, :email, :role, :th, :inv, now() + interval '1 day', :acc_by)"
                ),
                params,
            )

        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO organizations (id, name, slug)"
                        " VALUES ('org-1', 'One', 'one'), ('org-2', 'Two', 'two')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, hashed_password, is_active,"
                        " is_operator) VALUES"
                        " ('u-inviter', 'i@example.com', 'I', 'x', true, false),"
                        " ('u-acceptor', 'a2@example.com', 'A', 'x', true, false)"
                    )
                )
                insert(conn, "inv-1")
            refused = {
                "inv-dup-open": {},
                "inv-bad-role": {"email": "r@example.com", "role": "superuser"},
                "inv-dup-hash": {"email": "h@example.com", "th": "inv-1".rjust(64, "0")},
                "inv-no-org": {"email": "o@example.com", "org": "org-missing"},
            }
            for id_, kw in refused.items():
                with pytest.raises(IntegrityError), engine.begin() as conn:
                    insert(conn, id_, **kw)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE organization_invitations SET revoked_at = now() WHERE id = 'inv-1'"
                    )
                )
                insert(conn, "inv-2")  # history kept: a new open row after revocation
                insert(conn, "inv-3", org="org-2")
                insert(conn, "inv-4", email="A@example.com")
                insert(conn, "inv-5", email="acc@example.com", acc_by="u-acceptor")
                conn.execute(text("DELETE FROM users WHERE id IN ('u-inviter', 'u-acceptor')"))
                referencing = conn.execute(
                    text(
                        "SELECT count(*) FROM organization_invitations"
                        " WHERE invited_by_user_id IS NOT NULL OR accepted_by_user_id IS NOT NULL"
                    )
                ).scalar_one()
                assert referencing == 0
                conn.execute(text("DELETE FROM organizations WHERE id = 'org-1'"))
                left = conn.execute(text("SELECT id FROM organization_invitations")).scalars()
                assert left.all() == ["inv-3"]
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# PostgreSQL: concurrency
# --------------------------------------------------------------------------- #
ORG = "org-cc"
ROUNDS = 5
WORKERS = 4


@contextmanager
def _pg_factory() -> Iterator[sessionmaker]:  # pragma: no cover - gated on live PG
    with _pg_database() as url:
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


def _seed(factory, *, others=()) -> None:  # pragma: no cover - gated on live PG
    owners = ("u-owner-1", "u-owner-2")
    with factory() as s:
        s.add(Organization(id=ORG, name="Concurrent", slug="concurrent"))
        s.flush()
        for user_id in (*owners, *[u for u, _ in others]):
            s.add(
                User(
                    id=user_id,
                    email=f"{user_id}@example.com",
                    full_name=user_id,
                    hashed_password="x",
                )
            )
        s.flush()
        for user_id in owners:
            s.add(OrganizationMember(organization_id=ORG, user_id=user_id, role="owner"))
        for user_id, role in others:
            s.add(OrganizationMember(organization_id=ORG, user_id=user_id, role=role))
        s.commit()


def _ctx(s, user_id: str, role: Role) -> OrganizationContext:  # pragma: no cover
    return OrganizationContext(
        user=s.get(User, user_id), organization=s.get(Organization, ORG), role=role
    )


def _race(factory, calls: list[Callable]) -> list[str]:  # pragma: no cover - gated on live PG
    """Run each ``call(session)`` on its own session, released together; commit on success."""
    barrier = threading.Barrier(len(calls), timeout=20)

    def run(call):
        s = factory()
        try:
            barrier.wait()
            call(s)
            s.commit()
            return "ok"
        except SignalNestError as exc:
            s.rollback()
            return exc.code
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def _scalar(factory, stmt):  # pragma: no cover
    with factory() as s:
        return s.scalar(stmt)


def _create_token(factory, email: str, inviter: str = "u-owner-1") -> str:  # pragma: no cover
    with factory() as s:
        _, token = inv.create_invitation(
            s, ctx=_ctx(s, inviter, Role.OWNER), email=email, role=Role.VIEWER
        )
        s.commit()
    return token


@_PG
def test_simultaneous_acceptances_yield_one_membership() -> None:  # pragma: no cover
    with _pg_factory() as factory:
        _seed(factory, others=[])
        for round_ in range(ROUNDS):
            guest = f"u-guest-{round_}"
            with factory() as s:
                s.add(
                    User(id=guest, email=f"{guest}@example.com", full_name="G", hashed_password="x")
                )
                s.commit()
            token = _create_token(factory, f"{guest}@example.com")

            def accept(s, token=token, guest=guest):
                inv.accept_invitation_existing_user(s, user=s.get(User, guest), token=token)

            outcomes = _race(factory, [accept] * WORKERS)
            assert outcomes.count("ok") == 1, outcomes
            assert set(outcomes) - {"ok"} <= {
                "invitation_already_member",
                "invitation_already_used",
            }, outcomes
            memberships = select(func.count()).select_from(OrganizationMember)
            assert _scalar(factory, memberships.where(OrganizationMember.user_id == guest)) == 1
            with factory() as s:
                row = s.scalar(
                    select(OrganizationInvitation).where(
                        OrganizationInvitation.token_hash == inv.hash_invitation_token(token)
                    )
                )
                assert row.accepted_by_user_id == guest and row.accepted_at is not None
                accepted = (
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.action == "organization_invitation.accepted",
                        AuditLog.entity_id == row.id,
                    )
                )
                assert s.scalar(accepted) == 1


@_PG
def test_simultaneous_invited_registrations_yield_one_account() -> None:  # pragma: no cover
    from app.auth.service import create_user

    with _pg_factory() as factory:
        _seed(factory)
        for round_ in range(ROUNDS):
            email = f"new-{round_}@example.com"
            token = _create_token(factory, email)

            def register(s, token=token):
                inv.accept_invitation_new_user(
                    s,
                    token=token,
                    create_user=lambda e, s=s: create_user(
                        s, email=e, full_name="New", password="password-123"
                    ),
                )

            outcomes = _race(factory, [register] * WORKERS)
            assert outcomes.count("ok") == 1, outcomes
            assert set(outcomes) - {"ok"} <= {
                "invitation_already_used",
                "invitation_account_exists",
            }, outcomes
            users = select(func.count()).select_from(User).where(User.email == email)
            assert _scalar(factory, users) == 1
            joined = (
                select(func.count())
                .select_from(OrganizationMember)
                .join(User, User.id == OrganizationMember.user_id)
                .where(User.email == email)
            )
            assert _scalar(factory, joined) == 1
            assert _scalar(factory, select(func.count()).select_from(Organization)) == 1


@_PG
def test_simultaneous_duplicate_creation_leaves_one_pending_row() -> None:  # pragma: no cover
    with _pg_factory() as factory:
        _seed(factory, others=[("u-admin", "admin")])
        for round_ in range(ROUNDS):
            email = f"dup-{round_}@example.com"

            def create(s, email=email, actor=("u-owner-1", Role.OWNER)):
                inv.create_invitation(
                    s, ctx=_ctx(s, actor[0], actor[1]), email=email, role=Role.VIEWER
                )

            def create_as_admin(s, email=email):
                create(s, email, ("u-admin", Role.ADMIN))

            outcomes = _race(factory, [create, create, create_as_admin, create])
            assert outcomes.count("ok") == 1, outcomes
            assert set(outcomes) - {"ok"} == {"invitation_pending_exists"}, outcomes
            rows = select(func.count()).select_from(OrganizationInvitation)
            assert _scalar(factory, rows.where(OrganizationInvitation.email == email)) == 1


@_PG
@pytest.mark.parametrize("mode", ["demote-demote", "remove-remove", "demote-remove"])
def test_two_owners_acting_on_each_other_leave_an_owner(mode) -> None:  # pragma: no cover
    first, second = mode.split("-")

    def act(kind, actor, target):
        def call(s):
            ctx = _ctx(s, actor, Role.OWNER)  # what the route resolved before the lock
            if kind == "demote":
                members.change_member_role(s, ctx=ctx, target_user_id=target, new_role=Role.ADMIN)
            else:
                members.remove_member(s, ctx=ctx, target_user_id=target)

        return call

    with _pg_factory() as factory:
        _seed(factory)
        for _ in range(ROUNDS):
            with factory() as s:
                s.execute(OrganizationMember.__table__.delete())
                for user_id in ("u-owner-1", "u-owner-2"):
                    s.add(OrganizationMember(organization_id=ORG, user_id=user_id, role="owner"))
                s.commit()
            outcomes = _race(
                factory,
                [act(first, "u-owner-1", "u-owner-2"), act(second, "u-owner-2", "u-owner-1")],
            )
            assert outcomes.count("ok") == 1, outcomes
            assert set(outcomes) - {"ok"} <= {"member_owner_required", "permission_denied"}, (
                outcomes
            )
            owners = (
                select(func.count())
                .select_from(OrganizationMember)
                .where(
                    OrganizationMember.organization_id == ORG, OrganizationMember.role == "owner"
                )
            )
            assert _scalar(factory, owners) == 1


@_PG
def test_the_organization_lock_blocks_a_second_mutation() -> None:  # pragma: no cover
    """Direct evidence of serialization: a held lock makes the second change wait."""
    with _pg_factory() as factory:
        _seed(factory, others=[("u-target", "viewer")])
        holder = factory()
        members.lock_organization(holder, ORG)  # held until commit
        done = threading.Event()

        def change():
            with factory() as s:
                members.change_member_role(
                    s,
                    ctx=_ctx(s, "u-owner-2", Role.OWNER),
                    target_user_id="u-target",
                    new_role=Role.MARKETER,
                )
                s.commit()
            done.set()

        worker = threading.Thread(target=change)
        worker.start()
        time.sleep(1.0)
        assert not done.is_set(), "the second mutation did not wait for the organization lock"
        holder.commit()
        holder.close()
        worker.join(timeout=15)
        assert done.is_set()
        target_role = select(OrganizationMember.role).where(
            OrganizationMember.user_id == "u-target"
        )
        assert _scalar(factory, target_role) == "marketer"


@_PG
def test_partial_unique_pending_index_under_contention() -> None:  # pragma: no cover
    with _pg_factory() as factory:
        _seed(factory)
        engine = factory.kw["bind"]
        barrier = threading.Barrier(2, timeout=20)
        statement = text(
            "INSERT INTO organization_invitations (id, organization_id, email, role, token_hash,"
            " invited_by_user_id, expires_at) VALUES (:id, :org, 'race@example.com', 'viewer',"
            " :th, 'u-owner-1', now() + interval '1 day')"
        )

        def insert(i):
            with engine.connect() as conn:
                trans = conn.begin()
                barrier.wait()
                try:
                    conn.execute(statement, {"id": f"race-{i}", "org": ORG, "th": str(i) * 64})
                    trans.commit()
                    return "ok"
                except IntegrityError:
                    trans.rollback()
                    return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(insert, [1, 2])) == ["conflict", "ok"]
        with engine.begin() as conn:
            conn.execute(text("UPDATE organization_invitations SET revoked_at = now()"))
            conn.execute(statement, {"id": "race-3", "org": ORG, "th": "3" * 64})
