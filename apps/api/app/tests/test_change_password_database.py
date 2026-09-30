"""P6-UI-017: the mandatory PostgreSQL suite for the password change.

U17-C01…C06, U17-R02 and U17-B08p -- the rows SQLite cannot prove (row locks, stale reads
under READ COMMITTED, the database clock) -- plus the PostgreSQL halves of the main request
tests, which run the SQLite test bodies of ``test_change_password_api.py`` unchanged against
a PostgreSQL database of their own.

Fail-closed, as the AUTH4 suite: no ``skipif``, no ``xfail``, no ``pytestmark``; a module
autouse fixture and the ``pg_env`` fixture both call ``require_postgres()`` first, so with
``TEST_POSTGRES_URL`` unset every test here FAILS in CI (``CI=true``) and is skipped only on a
developer machine.

Concurrency is forced, never timed. A request is parked at a named seam by events the test
controls -- before the user-row lock (after authentication and both bcrypt checks), before
opening a session, before minting a token -- or held after its writes just before its
COMMIT. That a request is blocked on a row lock is never inferred from it "not having
returned yet": it is observed in PostgreSQL itself (``pg_locks.granted = false``,
``pg_stat_activity.wait_event_type = 'Lock'``, ``pg_blocking_pids``), read in a spin with
no sleep and a bounded deadline. Every ``Event.wait`` is asserted to have returned True.

The controls run inside the test that they control: U17-C01 runs the guarded race and then
the same race with the under-lock re-check removed and with the lock removed, each patched
at a seam the change path is shown to call, and asserts the outcome the guard exists to
prevent (two successes, the epoch moved twice, the earlier password overwritten).
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.auth import account_tokens
from app.auth import routes as auth_routes
from app.auth import service as auth_service
from app.auth.models import AuthSession
from app.core import security
from app.core.security import decode_access_token, verify_password
from app.main import app
from app.organizations.models import User
from app.tests import test_change_password_api as unit
from app.tests._auth2_support import (
    ALICE,
    BOB,
    EMAIL,
    LOGOUT_ALL,
    NEW_PASSWORD,
    OLD_PASSWORD,
    SKEW_60,
    Env,
    app_clock_skewed,
    as_utc,
    claims,
    confirm_reset,
    environment,
    error,
    get_me,
    login,
    mint_reset,
    open_session,
    post,
    require_postgres,
)

#: Every wait in this module is bounded by this many seconds and asserted to succeed.
WAIT = 20.0

UNAUTHORIZED = unit.UNAUTHORIZED
RESET_INVALID = unit.RESET_INVALID
PASSWORD_A = "first-winner-pass-1"
PASSWORD_B = "second-racer-pass-2"
CHANGED_PASSWORD = "changed-pass-word-3"


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


def _backend_pid(db: Session) -> int:
    return db.execute(text("SELECT pg_backend_pid()")).scalar_one()


# --------------------------------------------------------------------------- #
# Choreography: parking points, a held COMMIT, and lock waits observed in PostgreSQL
# --------------------------------------------------------------------------- #
class _Park:
    """Parks the first ``parties`` callers of a wrapped function, in arrival order.

    Caller ``i`` records its thread and -- when the function takes the request's
    ``Session`` first -- that transaction's backend pid, sets ``arrived[i]`` and waits for
    ``go[i]``. Later callers pass straight through.
    """

    def __init__(self, parties: int) -> None:
        self.parties = parties
        self.arrived = [threading.Event() for _ in range(parties)]
        self.go = [threading.Event() for _ in range(parties)]
        self.threads: list[int | None] = [None] * parties
        self.pids: list[int | None] = [None] * parties
        self._next = 0
        self._lock = threading.Lock()

    def wrap(self, real: Callable, *, pid: bool) -> Callable:
        def parked(*args, **kwargs):
            with self._lock:
                i, self._next = self._next, self._next + 1
            if i < self.parties:
                self.threads[i] = threading.get_ident()
                if pid:
                    self.pids[i] = _backend_pid(args[0])
                self.arrived[i].set()
                assert self.go[i].wait(WAIT), f"parked caller {i} was never released"
            return real(*args, **kwargs)

        return parked

    def await_arrival(self, i: int) -> None:
        assert self.arrived[i].wait(WAIT), f"caller {i} never reached its parking point"

    def party(self) -> int | None:
        me = threading.get_ident()
        return self.threads.index(me) if me in self.threads else None


class _CommitHold:
    """Holds the first COMMIT issued on the armed thread -- after its writes -- until released."""

    def __init__(self) -> None:
        self.thread: int | None = None
        self.reached = threading.Event()
        self.release = threading.Event()

    def before_commit(self, session) -> None:
        if self.thread is not None and threading.get_ident() == self.thread:
            self.thread = None
            self.reached.set()
            assert self.release.wait(WAIT), "the held COMMIT was never released"

    def await_reached(self) -> None:
        assert self.reached.wait(WAIT), "the held request never reached its COMMIT"


class _Choreography:
    def __init__(self, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        self.parks: list[_Park] = []
        self.holds: list[_CommitHold] = []

    def park(self, owner, name: str, *, parties: int, pid: bool = True) -> _Park:
        park = _Park(parties)
        self.monkeypatch.setattr(owner, name, park.wrap(getattr(owner, name), pid=pid))
        self.parks.append(park)
        return park

    def hold(self) -> _CommitHold:
        hold = _CommitHold()
        event.listen(Session, "before_commit", hold.before_commit)
        self.holds.append(hold)
        return hold

    def hold_commit_of(self, owner, name: str) -> tuple[_CommitHold, list[int]]:
        """Hold the COMMIT of the request that calls ``owner.<name>``; also record its pid."""
        hold, pids = self.hold(), []
        real = getattr(owner, name)

        def marking(*args, **kwargs):
            hold.thread = threading.get_ident()
            pids.append(_backend_pid(args[0]))
            return real(*args, **kwargs)

        self.monkeypatch.setattr(owner, name, marking)
        return hold, pids

    def release_everything(self) -> None:
        for park in self.parks:
            for go in park.go:
                go.set()
        for hold in self.holds:
            hold.thread = None
            hold.release.set()
            event.remove(Session, "before_commit", hold.before_commit)


@contextmanager
def choreography(monkeypatch) -> Iterator[_Choreography]:
    c = _Choreography(monkeypatch)
    try:
        yield c
    finally:
        c.release_everything()


_LOCK_WAIT = text(
    "SELECT a.wait_event_type, a.query, pg_blocking_pids(a.pid) AS blockers,"
    " EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid = a.pid AND NOT l.granted) AS waiting"
    " FROM pg_stat_activity a WHERE a.pid = :pid"
)


def lock_wait(env: Env, pid: int) -> dict:
    """Spin -- no sleep, bounded -- until backend ``pid`` is observed waiting on a lock.

    Each probe ends its transaction so the next reads a fresh activity snapshot
    (``pg_stat_activity`` is cached per transaction; ``pg_locks`` is read live).
    """
    deadline = time.monotonic() + WAIT
    with env.witness.engine.connect() as conn:
        while time.monotonic() < deadline:
            row = conn.execute(_LOCK_WAIT, {"pid": pid}).mappings().one_or_none()
            conn.rollback()
            if row is not None and row["waiting"] and row["wait_event_type"] == "Lock":
                return dict(row)
    pytest.fail(f"backend {pid} was never observed waiting on a lock")


def change(env: Env, current: str, new: str, token: str):
    return unit.change(env, current, new, token=token)


# --------------------------------------------------------------------------- #
# U17-C01: two simultaneous changes
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Race:
    a: object  # the response of the request that locked first
    b: object  # the response of the request that locked second
    epoch: int
    hashed_password: str
    events: int
    b_rechecked_while_a_held: bool
    b_waited_on: str


def _race(env: Env, monkeypatch, *, without: str | None = None) -> Race:
    """Two changes of ALICE's password from two sessions, both parked after authentication
    and both bcrypt checks and before the lock; A is released first and held at its COMMIT
    (it holds the row), then B is released. ``without`` removes the re-check or the lock."""
    token_a, _ = open_session(env.request_engine, ALICE)
    token_b, _ = open_session(env.request_engine, ALICE)
    real_recheck = auth_service._unchanged_since_verified
    with choreography(monkeypatch) as c, unit.capture_logs() as records:
        if without == "lock":

            def unlocked(db, *criteria):  # the same read, without FOR UPDATE
                stmt = select(User).where(*criteria).execution_options(populate_existing=True)
                return db.execute(stmt).scalar_one_or_none()

            monkeypatch.setattr(auth_service, "_lock_user", unlocked)
        park = c.park(auth_service, "_lock_user", parties=2)
        rechecked = [threading.Event(), threading.Event()]

        def recheck(locked, **kwargs):
            party = park.party()
            if party is not None:
                rechecked[party].set()
            return True if without == "recheck" else real_recheck(locked, **kwargs)

        monkeypatch.setattr(auth_service, "_unchanged_since_verified", recheck)
        hold = c.hold()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(change, _elsewhere(env), OLD_PASSWORD, PASSWORD_A, token_a)
            park.await_arrival(0)
            second = pool.submit(change, _elsewhere(env), OLD_PASSWORD, PASSWORD_B, token_b)
            park.await_arrival(1)
            # Both authenticated at epoch 0 and passed both bcrypt checks; neither has locked.
            assert env.witness.user(ALICE)["auth_epoch"] == 0
            hold.thread = park.threads[0]
            park.go[0].set()
            hold.await_reached()  # A locked, re-checked and wrote; its COMMIT is held
            park.go[1].set()
            if without == "lock":
                # Not blocked by the read: B re-checks the COMMITTED row while A still holds.
                assert rechecked[1].wait(WAIT), "B never re-checked while A was held"
            waiting = lock_wait(env, park.pids[1])  # B now waits on A's row
            assert park.pids[0] in waiting["blockers"], waiting
            b_rechecked = rechecked[1].is_set()
            hold.release.set()
            a, b = first.result(timeout=WAIT), second.result(timeout=WAIT)
        assert rechecked[0].is_set(), "A never reached the re-check"
    user = env.witness.user(ALICE)
    return Race(
        a=a,
        b=b,
        epoch=user["auth_epoch"],
        hashed_password=user["hashed_password"],
        events=len(unit.changed_events(records)),
        b_rechecked_while_a_held=b_rechecked,
        b_waited_on=waiting["query"],
    )


def _assert_one_winner(race: Race) -> None:
    """The invariant: exactly one 204 and one 401; the epoch moved once; the final hash is
    the winner's password only; one security event."""
    assert (race.a.status_code, race.a.content) == (204, b""), race.a.text
    assert race.b.status_code == 401, (race.b.status_code, race.b.text)
    assert error(race.b) == UNAUTHORIZED
    assert race.epoch == 1
    assert verify_password(PASSWORD_A, race.hashed_password)
    assert not verify_password(PASSWORD_B, race.hashed_password)
    assert race.events == 1


def test_u17_c01_two_concurrent_changes_exactly_one_wins(monkeypatch):
    """U17-C01, with both controls run here: remove the re-check, then the lock."""
    require_postgres()
    with environment(None) as env, monkeypatch.context() as m:
        guarded = _race(env, m)
        _assert_one_winner(guarded)
        # B waited on the row lock itself, before it could re-check anything.
        assert "FOR UPDATE" in guarded.b_waited_on.upper()
        assert not guarded.b_rechecked_while_a_held
        assert login(env, EMAIL[ALICE], PASSWORD_A).status_code == 200
        assert login(env, EMAIL[ALICE], PASSWORD_B).status_code == 401

    # CONTROL 1 -- the re-check removed: B waits for the lock, then overwrites A.
    with environment(None) as env, monkeypatch.context() as m:
        stale = _race(env, m, without="recheck")
        assert "FOR UPDATE" in stale.b_waited_on.upper()
        assert (stale.a.status_code, stale.b.status_code) == (204, 204)
        assert stale.epoch == 2
        assert verify_password(PASSWORD_B, stale.hashed_password)  # A's change overwritten
        with pytest.raises(AssertionError):
            _assert_one_winner(stale)

    # CONTROL 2 -- the lock removed: B is not blocked by its read, passes the re-check on the
    # committed row, then waits only at its UPDATE and overwrites A.
    with environment(None) as env, monkeypatch.context() as m:
        unlocked = _race(env, m, without="lock")
        assert unlocked.b_rechecked_while_a_held
        assert unlocked.b_waited_on.lstrip().upper().startswith("UPDATE USERS")
        assert (unlocked.a.status_code, unlocked.b.status_code) == (204, 204)
        assert unlocked.epoch == 2
        assert verify_password(PASSWORD_B, unlocked.hashed_password)
        with pytest.raises(AssertionError):
            _assert_one_winner(unlocked)


# --------------------------------------------------------------------------- #
# U17-C02 / U17-R02: reset-confirm vs change, both commit orders forced
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("commits_first", ["reset", "change"])
def test_u17_c02_r02_reset_confirm_and_change_never_both_succeed(
    pg_env, monkeypatch, commits_first
):
    env = pg_env
    token, _ = open_session(env.request_engine, ALICE)
    raw = mint_reset(env, ALICE)
    with choreography(monkeypatch) as c, ThreadPoolExecutor(max_workers=2) as pool:
        if commits_first == "reset":
            # The reset locks, claims and writes, and is held at its COMMIT. The change then
            # authenticates against the committed epoch 0, passes both bcrypt checks, and
            # parks before its lock.
            hold, holder = c.hold_commit_of(account_tokens, "confirm_password_reset")
            park = c.park(auth_service, "_lock_user", parties=1)
            first = pool.submit(confirm_reset, _elsewhere(env), raw)
            hold.await_reached()
            second = pool.submit(change, _elsewhere(env), OLD_PASSWORD, CHANGED_PASSWORD, token)
            park.await_arrival(0)
        else:
            # The change is held at its COMMIT (row written, reset token revoked, both
            # uncommitted). The reset then finds its token still open and parks before its lock.
            hold, holder = c.hold_commit_of(auth_service, "change_password")
            park = c.park(account_tokens, "_lock_user", parties=1)
            first = pool.submit(change, _elsewhere(env), OLD_PASSWORD, CHANGED_PASSWORD, token)
            hold.await_reached()
            second = pool.submit(confirm_reset, _elsewhere(env), raw)
            park.await_arrival(0)
        park.go[0].set()
        waiting = lock_wait(env, park.pids[0])
        assert holder[0] in waiting["blockers"] and "FOR UPDATE" in waiting["query"].upper()
        hold.release.set()
        winner, loser = first.result(timeout=WAIT), second.result(timeout=WAIT)
    assert (winner.status_code, winner.content) == (204, b""), winner.text
    user = env.witness.user(ALICE)
    assert user["auth_epoch"] == 1  # exactly one credential change committed
    if commits_first == "reset":
        assert error(loser) == UNAUTHORIZED
        final, lost = NEW_PASSWORD, CHANGED_PASSWORD
    else:
        assert unit.code(loser) == RESET_INVALID  # the change revoked the token (FD-U17-3)
        final, lost = CHANGED_PASSWORD, NEW_PASSWORD
    assert verify_password(final, user["hashed_password"])
    assert not verify_password(lost, user["hashed_password"])
    assert not verify_password(OLD_PASSWORD, user["hashed_password"])
    assert login(env, EMAIL[ALICE], final).status_code == 200
    assert error(get_me(env, token)) == UNAUTHORIZED


# --------------------------------------------------------------------------- #
# U17-C03: an old-password sign-in that commits after the change
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("parked", ["before-its-session", "before-its-commit"])
def test_u17_c03_an_old_password_sign_in_committed_after_the_change_is_refused(
    pg_env, monkeypatch, parked
):
    """The sign-in verified the OLD password and has not committed when the change runs.

    ``before-its-session``: parked after ``verify_password``, before it inserts its session;
    the change completes. ``before-its-commit``: its session row is inserted and its COMMIT
    held; the change -- parked after bcrypt -- then waits on the user row (the sign-in's
    foreign-key share lock conflicts with ``FOR UPDATE``) until the sign-in commits, and
    completes after it. Either way the sign-in's token carries the old epoch and is refused.
    """
    env = pg_env
    token, _ = open_session(env.request_engine, ALICE)
    with choreography(monkeypatch) as c, ThreadPoolExecutor(max_workers=2) as pool:
        if parked == "before-its-session":
            park = c.park(auth_service, "create_session", parties=1)
            signing_in = pool.submit(login, _elsewhere(env), EMAIL[ALICE], OLD_PASSWORD)
            park.await_arrival(0)  # it verified the old password; it has no session yet
            changed = change(env, OLD_PASSWORD, CHANGED_PASSWORD, token)
            park.go[0].set()
        else:
            hold, holder = c.hold_commit_of(auth_service, "create_session")
            park = c.park(auth_service, "_lock_user", parties=1)
            signing_in = pool.submit(login, _elsewhere(env), EMAIL[ALICE], OLD_PASSWORD)
            hold.await_reached()  # its session row is written, uncommitted
            changing = pool.submit(change, _elsewhere(env), OLD_PASSWORD, CHANGED_PASSWORD, token)
            park.await_arrival(0)
            park.go[0].set()
            waiting = lock_wait(env, park.pids[0])
            assert holder[0] in waiting["blockers"] and "FOR UPDATE" in waiting["query"].upper()
            hold.release.set()
            changed = changing.result(timeout=WAIT)
        signed_in = signing_in.result(timeout=WAIT)
    assert (changed.status_code, changed.content) == (204, b""), changed.text
    assert signed_in.status_code == 200, signed_in.text
    minted = signed_in.json()["access_token"]
    assert claims(minted)["auth_epoch"] == 0  # the epoch it verified under
    assert env.witness.user(ALICE)["auth_epoch"] == 1
    row = env.witness.session_row(claims(minted)["sid"])
    assert row["revoked_at"] is None  # a committed, live row -- inert by the epoch alone
    assert error(get_me(env, minted)) == UNAUTHORIZED
    assert login(env, EMAIL[ALICE], OLD_PASSWORD).status_code == 401
    assert login(env, EMAIL[ALICE], CHANGED_PASSWORD).status_code == 200


# --------------------------------------------------------------------------- #
# U17-C04: /auth/me during a change
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("parked", ["change-before-its-commit", "me-before-minting"])
def test_u17_c04_a_token_reissued_during_a_change_is_refused(pg_env, monkeypatch, parked):
    env = pg_env
    changer, _ = open_session(env.request_engine, ALICE)
    reader, reader_sid = open_session(env.request_engine, ALICE)
    with choreography(monkeypatch) as c, ThreadPoolExecutor(max_workers=1) as pool:
        if parked == "change-before-its-commit":
            hold, _ = c.hold_commit_of(auth_service, "change_password")
            changing = pool.submit(change, _elsewhere(env), OLD_PASSWORD, CHANGED_PASSWORD, changer)
            hold.await_reached()  # the new hash and epoch are written, uncommitted
            me = get_me(env, reader)  # authenticates against the committed epoch 0
            hold.release.set()
            changed = changing.result(timeout=WAIT)
        else:
            park = c.park(auth_routes, "_session", parties=1, pid=False)
            reading = pool.submit(get_me, _elsewhere(env), reader)
            park.await_arrival(0)  # /me authenticated; it has not minted
            changed = change(env, OLD_PASSWORD, CHANGED_PASSWORD, changer)
            park.go[0].set()
            me = reading.result(timeout=WAIT)
    assert (changed.status_code, changed.content) == (204, b""), changed.text
    assert me.status_code == 200, me.text
    reissued = me.json()["access_token"]
    assert claims(reissued)["sid"] == reader_sid and claims(reissued)["auth_epoch"] == 0
    for token in (reissued, reader, changer):
        assert error(get_me(env, token)) == UNAUTHORIZED
    assert env.witness.user(ALICE)["auth_epoch"] == 1


# --------------------------------------------------------------------------- #
# U17-C05: logout-all during a change
# --------------------------------------------------------------------------- #
def test_u17_c05_a_logout_all_committed_during_a_change(pg_env, monkeypatch):
    """Pinned: the change is held at its COMMIT while a logout-all from another session of
    the account commits; the change still answers 204, and afterwards every token of the
    account is refused, every session row is revoked, and the new password signs in."""
    env = pg_env
    changer, _ = open_session(env.request_engine, ALICE)
    ender, _ = open_session(env.request_engine, ALICE)
    bystander, _ = open_session(env.request_engine, BOB)
    with choreography(monkeypatch) as c, ThreadPoolExecutor(max_workers=1) as pool:
        hold, _ = c.hold_commit_of(auth_service, "change_password")
        changing = pool.submit(change, _elsewhere(env), OLD_PASSWORD, CHANGED_PASSWORD, changer)
        hold.await_reached()
        ended = post(env, LOGOUT_ALL, None, token=ender)
        assert (ended.status_code, ended.content) == (204, b"")
        hold.release.set()
        changed = changing.result(timeout=WAIT)
    assert (changed.status_code, changed.content) == (204, b""), changed.text
    user = env.witness.user(ALICE)
    assert user["auth_epoch"] == 1
    assert verify_password(CHANGED_PASSWORD, user["hashed_password"])
    assert all(r["revoked_at"] is not None for r in env.witness.sessions(ALICE))
    for token in (changer, ender):
        assert error(get_me(env, token)) == UNAUTHORIZED
    assert get_me(env, bystander).status_code == 200
    assert login(env, EMAIL[ALICE], OLD_PASSWORD).status_code == 401
    fresh = login(env, EMAIL[ALICE], CHANGED_PASSWORD)
    assert fresh.status_code == 200
    assert get_me(env, fresh.json()["access_token"]).status_code == 200


# --------------------------------------------------------------------------- #
# U17-C06: the lock the change actually takes
# --------------------------------------------------------------------------- #
def test_u17_c06_the_change_locks_the_user_row_for_update_after_bcrypt(pg_env, monkeypatch):
    """The SQL the change path EXECUTES on PostgreSQL, captured at the cursor: exactly one
    ``SELECT … FROM users … FOR UPDATE``, after every bcrypt call and before both writes."""
    env = pg_env
    token, _ = open_session(env.request_engine, ALICE)
    steps: list[str] = []
    real_verify, real_hash = auth_service.verify_password, auth_service.hash_password

    def verify(plain, hashed):
        steps.append("bcrypt verify")
        return real_verify(plain, hashed)

    def hashing(plain):
        steps.append("bcrypt hash")
        return real_hash(plain)

    def statements(conn, cursor, statement, parameters, context, executemany):
        steps.append(" ".join(statement.split()))

    monkeypatch.setattr(auth_service, "verify_password", verify)
    monkeypatch.setattr(auth_service, "hash_password", hashing)
    event.listen(env.request_engine, "before_cursor_execute", statements)
    try:
        r = change(env, OLD_PASSWORD, CHANGED_PASSWORD, token)
    finally:
        event.remove(env.request_engine, "before_cursor_execute", statements)
    assert (r.status_code, r.content) == (204, b""), r.text
    locks = [i for i, s in enumerate(steps) if "FOR UPDATE" in s.upper()]
    assert len(locks) == 1, steps
    [at] = locks
    lock_sql = steps[at].upper()
    assert lock_sql.startswith("SELECT") and "FROM USERS" in lock_sql, lock_sql
    assert "WHERE USERS.ID = " in lock_sql and lock_sql.endswith("FOR UPDATE"), lock_sql
    bcrypt = [i for i, s in enumerate(steps) if s.startswith("bcrypt")]
    assert [steps[i] for i in bcrypt] == ["bcrypt verify", "bcrypt verify", "bcrypt hash"]
    assert max(bcrypt) < at, "bcrypt ran under the lock"
    writes = [s.split()[1].upper() for s in steps[at:] if s.upper().startswith("UPDATE")]
    assert writes == ["USERS", "PASSWORD_RESET_TOKENS"], steps[at:]
    assert not any(s.upper().startswith("UPDATE") for s in steps[:at]), "a write before the lock"
    # The helper the change path calls is the reset's own, and it compiles to FOR UPDATE.
    assert auth_service._lock_user is account_tokens._lock_user
    compiled = str(
        account_tokens._user_lock_select(User.id == "u").compile(dialect=postgresql.dialect())
    )
    assert compiled.rstrip().endswith("FOR UPDATE"), compiled


# --------------------------------------------------------------------------- #
# U17-B08p: an expired session on the database clock
# --------------------------------------------------------------------------- #
def _set_expiry(env: Env, sid: str, interval: str) -> None:
    """Move a session's absolute expiry relative to the DATABASE clock: ``now() + interval``."""
    stmt = (
        update(AuthSession)
        .where(AuthSession.id == sid)
        .values(expires_at=text(f"now() + interval '{interval}'"))
    )
    assert env.witness.write(stmt) == 1


def test_u17_b08p_an_expired_session_is_refused_on_the_database_clock(pg_env):
    """Expired on the database clock while the APPLICATION clock runs 60 minutes behind, so
    an app-clock comparison would still accept it: only the database can refuse."""
    env = pg_env
    token, sid = open_session(env.request_engine, ALICE)
    assert get_me(env, token).status_code == 200
    _set_expiry(env, sid, "-1 second")
    expires_at = as_utc(env.witness.session_row(sid)["expires_at"])
    assert decode_access_token(token) is not None  # the JWT layer alone would accept it
    before = unit.state(env)
    with app_clock_skewed(-SKEW_60) as shifted:
        assert security.datetime is shifted
        assert expires_at > shifted.now(UTC)
        r = change(env, OLD_PASSWORD, CHANGED_PASSWORD, token)
    assert error(r) == UNAUTHORIZED
    assert unit.state(env) == before


def test_u17_b08p_a_live_session_is_not_ended_by_an_ahead_app_clock(pg_env):
    """Positive control: live on the database clock, 'expired' on an app clock 60 minutes
    ahead -- the change goes through."""
    env = pg_env
    token, sid = open_session(env.request_engine, ALICE)
    _set_expiry(env, sid, "30 minutes")
    expires_at = as_utc(env.witness.session_row(sid)["expires_at"])
    with app_clock_skewed(SKEW_60) as shifted:
        assert shifted.now(UTC) > expires_at
        r = change(env, OLD_PASSWORD, CHANGED_PASSWORD, token)
    assert (r.status_code, r.content) == (204, b""), r.text


# --------------------------------------------------------------------------- #
# The PostgreSQL halves: the SQLite test bodies, unchanged
# --------------------------------------------------------------------------- #
def test_u17_b01p_success_replaces_the_hash_and_moves_the_epoch_only(pg_env):
    unit.TestSuccess().test_u17_b01_success_replaces_the_hash_and_moves_the_epoch_only(pg_env)


def test_u17_b02p_a_wrong_current_password_is_422_and_keeps_the_session(pg_env):
    unit.TestCurrentPassword().test_u17_b02_a_wrong_current_password_is_422_and_keeps_the_session(
        pg_env, unit.WRONG_PASSWORD
    )


@pytest.mark.parametrize("current, new", [(unit.LONG_A, unit.LONG_B), (unit.EURO_A, unit.EURO_B)])
def test_u17_b05bp_the_same_first_72_bytes_is_the_same_credential(pg_env, current, new):
    unit.TestSamePassword().test_u17_b05b_the_same_first_72_bytes_is_the_same_credential(
        pg_env, current, new
    )


def test_u17_ma01p_every_token_of_every_session_is_refused(pg_env):
    unit.TestSessionModelA().test_u17_ma01_every_token_of_every_session_is_refused(pg_env)


def test_u17_ma02p_no_session_is_created_extended_or_revoked(pg_env):
    unit.TestSessionModelA().test_u17_ma02_no_session_is_created_extended_or_revoked(pg_env)


def test_u17_ma03p_a_fresh_sign_in_opens_a_fresh_twelve_hour_session(pg_env):
    unit.TestSessionModelA().test_u17_ma03_a_fresh_sign_in_opens_a_fresh_twelve_hour_session(pg_env)


def test_u17_r01p_a_reset_token_issued_before_the_change_is_404_afterwards(pg_env):
    unit.TestAccountTokens().test_u17_r01_a_reset_token_issued_before_the_change_is_404_afterwards(
        pg_env
    )


def test_u17_r03p_email_verification_is_untouched(pg_env):
    unit.TestAccountTokens().test_u17_r03_email_verification_is_untouched(pg_env)


def test_u17_b15p_other_accounts_are_untouched(pg_env):
    unit.TestBystanders().test_u17_b15_other_accounts_are_untouched(pg_env)
