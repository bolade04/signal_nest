"""P6-AUTH-4: the mandatory PostgreSQL suite (POSTGRES-EVIDENCE-PLAN-02D).

Every PostgreSQL-layer test of the canonical map lives in this ONE PostgreSQL-only file: the
PostgreSQL halves of the UNIT+PG rows (T-02p, T-06p, T-09p, T-13p, T-18p, T-21p, T-22p,
T-23p, T-32p) and the PostgreSQL-only T-14, T-17, T-20, T-33, D-01 and D-02. Where a row's
two halves assert the same thing, the PostgreSQL half runs the SAME test body as the SQLite
half, against a PostgreSQL database of its own.

Fail-closed. No ``skipif``, no ``pytestmark``, no parametrize value carrying a mark: a
module-level autouse fixture and the ``pg_env`` fixture both call ``require_postgres()``
first, so with ``TEST_POSTGRES_URL`` unset every test here FAILS in CI (``CI=true``) and is
skipped only on a developer machine.

Concurrency is forced, never timed: a request is held at a named point -- after
authentication and before its write, or after its write and before its COMMIT -- by events
the test controls, so no outcome depends on the scheduler. Clock tests shift only the
APPLICATION clock (``app_clock_skewed``); PostgreSQL's ``now()`` is untouched and decides
every session lifetime. jose keeps its own, unshifted clock, so a token minted on a skewed
application clock still decodes (disclosed, D-02).
"""

from __future__ import annotations

import dataclasses
import math
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.auth import account_tokens
from app.auth import dependencies as auth_dependencies
from app.auth import routes as auth_routes
from app.auth import service as auth_service
from app.auth.models import AuthSession
from app.core import security
from app.core.security import decode_access_token
from app.db import clock as db_clock
from app.main import app
from app.organizations.models import User
from app.tests import test_auth_sessions_api as unit
from app.tests import test_password_reset_api as reset_unit
from app.tests._auth2_support import (
    ALICE,
    BOB,
    CAROL,
    EMAIL,
    INVITATION_ACCEPT,
    LOGOUT,
    LOGOUT_ALL,
    OLD_PASSWORD,
    SKEW_60,
    Env,
    app_clock_skewed,
    as_utc,
    claims,
    confirm_reset,
    environment,
    get_me,
    invite,
    login,
    mint_reset,
    open_session,
    pg_database,
    post,
    require_postgres,
)

API_DIR = Path(__file__).resolve().parents[2]
UNAUTHORIZED = unit.UNAUTHORIZED
LIFETIME = unit.LIFETIME
SKEWS = {"AHEAD": SKEW_60, "BEHIND": -SKEW_60}
_refused = unit._refused


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def pg_env() -> Iterator[Env]:
    require_postgres()
    with environment(None) as e:
        yield e


def _elsewhere(env: Env) -> Env:
    """The same environment through a client of its own, for a request on another thread."""
    return dataclasses.replace(env, client=TestClient(app))


def _set_expiry(env: Env, sid: str, interval: str) -> None:
    """Move a session's absolute expiry relative to the DATABASE clock: ``now() + interval``."""
    stmt = (
        update(AuthSession)
        .where(AuthSession.id == sid)
        .values(expires_at=text(f"now() + interval '{interval}'"))
    )
    assert env.witness.write(stmt) == 1


class _Hold:
    """Parks ONE marked request's transaction after its writes, just before its COMMIT.

    The request marks itself (a wrapper around a function only that route calls); the
    session ``before_commit`` hook then holds that thread until ``release`` is set. Every
    other commit passes straight through.
    """

    def __init__(self) -> None:
        self.thread: int | None = None
        self.reached = threading.Event()
        self.release = threading.Event()

    def mark(self) -> None:
        self.thread = threading.get_ident()

    def before_commit(self, session) -> None:
        if self.thread is not None and threading.get_ident() == self.thread:
            self.thread = None
            self.reached.set()
            assert self.release.wait(20), "the held transaction was never released"


@contextmanager
def _holding(monkeypatch, owner, name: str) -> Iterator[_Hold]:
    """Hold the request that calls ``owner.<name>`` before its COMMIT."""
    hold = _Hold()
    real = getattr(owner, name)

    def marking(*args, **kwargs):
        hold.mark()
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, marking)
    event.listen(Session, "before_commit", hold.before_commit)
    try:
        yield hold
    finally:
        hold.release.set()
        event.remove(Session, "before_commit", hold.before_commit)


def _assert_skewed(shifted: type[datetime]) -> None:
    """The builder's clock is shifted; each AUTH4 database-clock module binds the shifted
    class or no ``datetime`` class at all (a module that binds none is unaffected)."""
    assert security.datetime is shifted
    for module in (auth_service, auth_dependencies, db_clock):
        assert getattr(module, "datetime", shifted) is shifted


# --------------------------------------------------------------------------- #
# The PostgreSQL halves of the UNIT+PG rows: the SQLite halves' own bodies
# --------------------------------------------------------------------------- #
def test_a_reset_ends_every_session_token_without_revoking_a_row(pg_env):  # T-02p
    reset_unit.TestConfirm().test_a_reset_ends_every_session_token_without_revoking_a_row(pg_env)


def test_logout_ends_only_its_own_session(pg_env):  # T-06p
    unit.TestLogout().test_logout_ends_only_its_own_session(pg_env)


def test_logout_all_ends_every_session_of_the_account_only(pg_env):  # T-13p
    unit.TestLogoutAll().test_logout_all_ends_every_session_of_the_account_only(pg_env)


def test_accept_reissues_in_the_same_session_and_deadline(pg_env):  # T-18p
    unit.TestReissue().test_accept_reissues_in_the_same_session_and_deadline(pg_env)


@pytest.mark.parametrize("end_with", ["original", "reissued"])
def test_a_stolen_bearer_cannot_fork_a_session_through_accept(pg_env, end_with):  # T-21p
    unit.TestReissue().test_a_stolen_bearer_cannot_fork_a_session_through_accept(pg_env, end_with)


def test_each_boundary_stamps_the_database_clock_and_a_720_minute_deadline(pg_env):  # T-22p
    """``db_t0``/``db_t1`` are ``clock_timestamp()`` read on the witness's own connection."""
    boundaries = unit.TestSessionBoundaries()
    boundaries.test_each_boundary_stamps_the_database_clock_and_a_720_minute_deadline(pg_env)


@pytest.mark.parametrize("boundary", unit.BOUNDARIES)
def test_the_session_is_committed_before_the_token_is_returned(boundary):  # T-23p
    require_postgres()
    with environment(None, teardown_commit=False) as env:
        r = unit._boundary(env, boundary)()
        assert r.status_code in (200, 201), r.text
        sid = claims(r.json()["access_token"])["sid"]
        assert env.witness.session_row(sid)["revoked_at"] is None
        assert get_me(env, r.json()["access_token"]).status_code == 200


@pytest.mark.parametrize("boundary", unit.BOUNDARIES)
def test_a_failed_commit_returns_no_token(pg_env, monkeypatch, boundary):  # T-23p
    unit.TestSessionBoundaries().test_a_failed_commit_returns_no_token(
        pg_env, monkeypatch, boundary
    )


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def _count_revocation_writes(env: Env) -> Callable[[], int]:
    """Record every row an UPDATE actually changes in ``auth_sessions.revoked_at``.

    An AFTER UPDATE ... FOR EACH ROW trigger fires once per changed row and never for an
    UPDATE whose WHERE matched nothing, so the count is the number of real writes.
    """
    with env.witness.engine.begin() as conn:
        conn.execute(text("CREATE TABLE auth4_revocation_writes (session_id text)"))
        conn.execute(
            text(
                "CREATE FUNCTION auth4_record_revocation() RETURNS trigger LANGUAGE plpgsql"
                " AS $$ BEGIN INSERT INTO auth4_revocation_writes VALUES (NEW.id);"
                " RETURN NEW; END $$"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER auth4_revocation_writes AFTER UPDATE OF revoked_at"
                " ON auth_sessions FOR EACH ROW EXECUTE FUNCTION auth4_record_revocation()"
            )
        )

    def count() -> int:
        with env.witness.engine.connect() as conn:
            query = text("SELECT count(*) FROM auth4_revocation_writes")
            return conn.execute(query).scalar_one()

    return count


def test_a_concurrent_double_logout_revokes_once(pg_env, monkeypatch):  # T-09p
    env = pg_env
    token, sid = open_session(env.request_engine, ALICE)
    assert get_me(env, token).status_code == 200
    writes = _count_revocation_writes(env)
    arrived = threading.Barrier(3, timeout=20)
    gates = [threading.Event(), threading.Event()]
    stamps: list[datetime] = []
    lock = threading.Lock()
    real = auth_routes.database_now

    def gated(db):
        # Reached after authentication and before the revoking UPDATE: when both requests
        # stand here, both are authenticated and neither has written or committed.
        stamp = real(db)
        with lock:
            slot = len(stamps)
            stamps.append(stamp)
        arrived.wait()
        assert gates[slot].wait(20)
        return stamp

    monkeypatch.setattr(auth_routes, "database_now", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(post, _elsewhere(env), LOGOUT, None, token=token) for _ in range(2)]
        arrived.wait()
        gates[0].set()
        done, _ = wait(futures, timeout=20, return_when=FIRST_COMPLETED)
        assert len(done) == 1  # the first request finished; the second is still held
        gates[1].set()
        outcomes = sorted(f.result(timeout=20).status_code for f in futures)
    assert set(outcomes) <= {204, 401} and 204 in outcomes, outcomes
    assert writes() == 1
    assert as_utc(env.witness.session_row(sid)["revoked_at"]) == stamps[0]


def test_logout_all_ends_reissues_made_while_it_was_uncommitted(pg_env, monkeypatch):  # T-14
    """Forced ordering: logout-all runs its UPDATE and waits, uncommitted, while a /me and an
    accept pass authentication; it then commits, and what they returned is refused."""
    env = pg_env
    inv = invite(env, EMAIL[BOB])
    first, _ = open_session(env.request_engine, BOB)
    second, _ = open_session(env.request_engine, BOB)
    for token in (first, second):
        assert get_me(env, token).status_code == 200
    with (
        _holding(monkeypatch, auth_routes, "database_now") as hold,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        ending = pool.submit(post, _elsewhere(env), LOGOUT_ALL, None, token=first)
        assert hold.reached.wait(20)  # its UPDATE has run; its COMMIT has not
        assert all(row["revoked_at"] is None for row in env.witness.sessions(BOB))
        me = get_me(env, second)
        accepted = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=second)
        assert (me.status_code, accepted.status_code) == (200, 200)  # both authenticated
        hold.release.set()
        assert ending.result(timeout=20).status_code == 204
    returned = [me.json()["access_token"], accepted.json()["access_token"]]
    for token in (*returned, first, second):
        assert _refused(get_me(env, token)) == UNAUTHORIZED
    fresh = login(env, EMAIL[BOB], OLD_PASSWORD)  # a session committed after the logout-all
    assert fresh.status_code == 200
    assert get_me(env, fresh.json()["access_token"]).status_code == 200


@pytest.mark.parametrize("held", ["logout-all", "reset"])
def test_logout_all_and_a_password_reset_both_take_effect(pg_env, monkeypatch, held):  # T-33
    """Each order forced: one request holds its COMMIT while the other runs to the end."""
    env = pg_env
    tokens = [open_session(env.request_engine, ALICE)[0] for _ in range(2)]
    for token in tokens:
        assert get_me(env, token).status_code == 200
    raw = mint_reset(env, ALICE)
    if held == "logout-all":
        owner, name = auth_routes, "database_now"
        start = lambda other: post(other, LOGOUT_ALL, None, token=tokens[0])  # noqa: E731
        finish = lambda: confirm_reset(env, raw)  # noqa: E731
    else:
        owner, name = account_tokens, "confirm_password_reset"
        start = lambda other: confirm_reset(other, raw)  # noqa: E731
        # Authenticates against the committed (old) epoch while the reset is uncommitted.
        finish = lambda: post(env, LOGOUT_ALL, None, token=tokens[0])  # noqa: E731
    with _holding(monkeypatch, owner, name) as hold, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(start, _elsewhere(env))
        assert hold.reached.wait(20)
        second = finish()
        hold.release.set()
        outcomes = (first.result(timeout=20).status_code, second.status_code)
    assert outcomes == (204, 204)
    assert [get_me(env, token).status_code for token in tokens] == [401, 401]
    assert env.witness.user(ALICE)["auth_epoch"] == 1
    assert all(row["revoked_at"] is not None for row in env.witness.sessions(ALICE))


# --------------------------------------------------------------------------- #
# The database deadline
# --------------------------------------------------------------------------- #
def test_me_is_capped_by_and_refused_at_the_database_deadline(pg_env):  # T-17
    env = pg_env
    token, sid = open_session(env.request_engine, ALICE)
    _set_expiry(env, sid, "30 minutes")
    r = get_me(env, token)
    assert r.status_code == 200
    reissued = claims(r.json()["access_token"])
    deadline = unit._deadline(env, sid)
    # The min() form, then the branch it selects: iat + TTL lies beyond the deadline.
    assert reissued["exp"] == min(reissued["iat"] + unit._ttl_s(), deadline) == deadline
    _set_expiry(env, sid, "-1 second")
    assert decode_access_token(token) is not None  # the JWT layer alone would accept it
    r = get_me(env, token)
    assert _refused(r) == UNAUTHORIZED and "access_token" not in r.text


def test_accept_on_an_expired_session_changes_nothing(pg_env):  # T-20
    env = pg_env
    inv = invite(env, EMAIL[BOB])
    token, sid = open_session(env.request_engine, BOB)
    assert get_me(env, token).status_code == 200
    _set_expiry(env, sid, "-1 second")
    assert decode_access_token(token) is not None
    before = env.witness.snapshot()
    r = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=token)
    assert _refused(r) == UNAUTHORIZED and "access_token" not in r.text
    assert env.witness.snapshot() == before  # no membership; the invitation still pending


def test_a_forked_bearer_shares_the_database_deadline(pg_env):  # T-21p (the deadline half)
    env = pg_env
    victim, sid = open_session(env.request_engine, BOB)
    inv = invite(env, EMAIL[BOB])
    r = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=victim)
    assert r.status_code == 200, r.text
    forked = r.json()["access_token"]
    for token in (victim, forked):
        assert get_me(env, token).status_code == 200
    _set_expiry(env, sid, "-1 second")
    for token in (victim, forked):
        assert decode_access_token(token) is not None
        assert _refused(get_me(env, token)) == UNAUTHORIZED


# --------------------------------------------------------------------------- #
# The application clock skewed by 60 minutes either way
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("boundary", unit.BOUNDARIES)
@pytest.mark.parametrize("skew", list(SKEWS))
def test_a_fresh_session_is_stamped_on_the_database_clock(pg_env, skew, boundary):  # D-01
    env = pg_env
    send = unit._boundary(env, boundary)  # its prerequisites are made on the real clock
    with app_clock_skewed(SKEWS[skew]) as shifted:
        _assert_skewed(shifted)
        # Service level: the service's own transaction reads now() and stamps exactly it.
        with sessionmaker(bind=env.request_engine, expire_on_commit=False)() as s:
            t_db = as_utc(s.scalar(select(func.now())))
            opened = auth_service.create_session(s, s.get(User, CAROL))
            s.commit()
        stored = env.witness.session_row(opened.id)
        assert as_utc(stored["created_at"]) == t_db
        assert as_utc(stored["expires_at"]) == t_db + LIFETIME
        # Route level.
        db_t0, app_t0 = env.witness.db_now(), shifted.now(UTC)
        r = send()
        db_t1, app_t1 = env.witness.db_now(), shifted.now(UTC)
    assert r.status_code in (200, 201), r.text
    access_token = r.json()["access_token"]
    token = claims(access_token)
    stored = env.witness.session_row(token["sid"])
    created, expires = as_utc(stored["created_at"]), as_utc(stored["expires_at"])
    assert db_t0 <= created <= db_t1
    assert expires - created == LIFETIME
    assert math.floor(app_t0.timestamp()) <= token["iat"] <= math.floor(app_t1.timestamp())
    assert token["exp"] == min(token["iat"] + unit._ttl_s(), math.floor(expires.timestamp()))
    assert abs(created - app_t0) > timedelta(minutes=55)  # an app-clock stamp would fail
    assert decode_access_token(access_token) is not None


def _reissue(env: Env, endpoint: str) -> tuple[str, str, Callable]:
    """BOB's live session and a re-issue request on it: ``/auth/me`` or invitation accept
    (its invitation made now, on the real clock)."""
    if endpoint == "me":
        token, sid = open_session(env.request_engine, BOB)
        return token, sid, lambda: get_me(env, token)
    inv = invite(env, EMAIL[BOB])
    token, sid = open_session(env.request_engine, BOB)
    body = {"token": inv["token"]}
    return token, sid, lambda: post(env, INVITATION_ACCEPT, body, token=token)


ENDPOINTS = ["me", "accept"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("skew", list(SKEWS))
def test_a_reissue_keeps_the_database_deadline(pg_env, skew, endpoint):  # D-02 (a)
    env = pg_env
    _, sid, reissue = _reissue(env, endpoint)
    _set_expiry(env, sid, "30 minutes")
    row = env.witness.session_row(sid)
    with app_clock_skewed(SKEWS[skew]) as shifted:
        _assert_skewed(shifted)
        r = reissue()
    assert r.status_code == 200, r.text
    token = claims(r.json()["access_token"])  # decodes on jose's own, unshifted clock
    deadline = math.floor(as_utc(row["expires_at"]).timestamp())
    assert token["sid"] == sid
    assert env.witness.session_row(sid) == row
    assert token["exp"] == min(token["iat"] + unit._ttl_s(), deadline) == deadline


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("skew", list(SKEWS))
def test_the_database_deadline_refuses_whatever_the_app_clock(pg_env, skew, endpoint):  # D-02 (b)
    env = pg_env
    token, sid, reissue = _reissue(env, endpoint)
    assert get_me(env, token).status_code == 200
    _set_expiry(env, sid, "-1 second")
    expires_at = as_utc(env.witness.session_row(sid)["expires_at"])
    assert decode_access_token(token) is not None  # the JWT layer accepts
    before = env.witness.snapshot()
    with app_clock_skewed(SKEWS[skew]) as shifted:
        _assert_skewed(shifted)
        if skew == "BEHIND":
            # An app-clock comparison would accept: only the database can refuse here.
            assert expires_at > shifted.now(UTC)
        r = reissue()
    assert _refused(r) == UNAUTHORIZED and "access_token" not in r.text
    assert env.witness.snapshot() == before  # accept: no membership, invitation pending


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_an_ahead_clock_does_not_end_a_live_session_early(pg_env, endpoint):  # D-02 (c)
    env = pg_env
    _, sid, reissue = _reissue(env, endpoint)
    _set_expiry(env, sid, "30 minutes")
    expires_at = as_utc(env.witness.session_row(sid)["expires_at"])
    with app_clock_skewed(SKEWS["AHEAD"]) as shifted:
        _assert_skewed(shifted)
        assert shifted.now(UTC) > expires_at  # an app-clock comparison would refuse
        r = reissue()
    assert r.status_code == 200, r.text
    assert claims(r.json()["access_token"])["sid"] == sid


# --------------------------------------------------------------------------- #
# T-32p: the migration on PostgreSQL, through the real Alembic CLI
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


def _alembic(url: str, *args: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *_downgrade_confirmation(url, args), *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (args, result.stdout + result.stderr)


def _catalog(url: str) -> dict:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            present = conn.execute(text("SELECT to_regclass('auth_sessions') IS NOT NULL"))
            if not present.scalar_one():
                return {}
            columns = conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default"
                    " FROM information_schema.columns WHERE table_name = 'auth_sessions'"
                )
            )
            rules = conn.execute(
                text(
                    "SELECT rc.delete_rule FROM information_schema.referential_constraints rc"
                    " JOIN information_schema.table_constraints tc"
                    " ON tc.constraint_name = rc.constraint_name"
                    " WHERE tc.table_name = 'auth_sessions'"
                )
            )
            indexes = conn.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'auth_sessions'")
            )
            return {
                "columns": {r[0]: tuple(r[1:]) for r in columns},
                "delete_rules": sorted(r[0] for r in rules),
                "indexes": dict(indexes.all()),
                "head": conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
            }
    finally:
        engine.dispose()


def test_the_migration_round_trips_on_postgresql():  # T-32p
    require_postgres()
    with pg_database("sn_a4m") as url:
        _alembic(url, "upgrade", "head")
        catalog = _catalog(url)
        assert catalog["head"] == unit.HEAD
        assert catalog["columns"] == {
            "id": ("character varying", "NO", None),
            "user_id": ("character varying", "NO", None),
            "created_at": ("timestamp with time zone", "NO", "CURRENT_TIMESTAMP"),
            "expires_at": ("timestamp with time zone", "NO", None),
            "revoked_at": ("timestamp with time zone", "YES", None),
        }
        assert catalog["delete_rules"] == ["CASCADE"]
        assert "(user_id)" in catalog["indexes"]["ix_auth_sessions_user_id"]
        _alembic(url, "check")
        # The cascade, on the migrated schema itself.
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, hashed_password, is_active,"
                        " is_operator) VALUES ('u-a4', 'a4@example.com', 'A4', 'h', true, false)"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO auth_sessions (id, user_id, expires_at)"
                        " VALUES (repeat('a', 32), 'u-a4', now() + interval '1 hour')"
                    )
                )
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM users WHERE id = 'u-a4'"))
                left = conn.execute(text("SELECT count(*) FROM auth_sessions")).scalar_one()
                assert left == 0
        finally:
            engine.dispose()
        _alembic(url, "downgrade", unit.PREV)
        assert _catalog(url) == {}
        _alembic(url, "upgrade", "head")
        assert _catalog(url)["head"] == unit.HEAD
