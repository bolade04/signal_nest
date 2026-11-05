"""Phase 5B login auditing on PostgreSQL 16: a sign-in locks no user row before its session.

    POST /api/v1/auth/login

PR #290's CI (run 41313017423) failed U17-C03 ``before-its-session`` in
``test_change_password_database.py``. The success audit rows were written before the
session. Each row references the account (``audit_logs.actor_user_id`` -> ``users.id``),
so its foreign-key check took ``FOR KEY SHARE`` on the user row before the sign-in reached
``create_session``. A concurrent password change's ``SELECT ... FOR UPDATE``
(``_lock_user``) then waited on the parked sign-in. U17-C03 requires the change to complete
in exactly that window.

The order pinned here: the session row first, then the audit rows, in the same transaction,
then the COMMIT, then the token (P6-AUTH-4). It is observed in PostgreSQL itself, never
inferred from timing. While the sign-in is parked at ``create_session``, after it has
verified the password:

* its transaction has no transaction id (no ``transactionid`` lock in ``pg_locks``), so it
  has written nothing and locked no row: a row lock is stamped with the locker's id;
* it holds no lock on ``audit_logs`` and no lock on ``users`` beyond the ``AccessShareLock``
  of its read, so no audit row is inserted and no foreign-key check has run;
* another connection's ``SELECT ... FOR UPDATE NOWAIT`` of the user row (the lock a password
  change takes) is granted at once.

Once released, the sign-in answers 200 and commits its session and its audit row in one
transaction: both rows carry the same ``xmin``.

The refusal rows carry no actor, so writing them runs no foreign-key check. A refused
sign-in held at its COMMIT, after its ``auth.login_failed`` rows are inserted, holds no lock
on the user row either. The 401 is unchanged.

Gate: :func:`require_postgres`. With ``TEST_POSTGRES_URL`` unset these tests FAIL in CI
(``CI=true``) and skip only on a developer machine.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth import service as auth_service
from app.main import app
from app.tests._auth2_support import (
    ALICE,
    EMAIL,
    OLD_PASSWORD,
    ORG_A,
    Env,
    claims,
    environment,
    login,
    require_postgres,
)

#: Every wait in this module is bounded by this many seconds and asserted to succeed.
WAIT = 20.0

_LOCKS = text(
    "SELECT l.locktype, l.mode, c.relname FROM pg_locks l"
    " LEFT JOIN pg_class c ON c.oid = l.relation"
    " WHERE l.pid = :pid AND l.granted"
    " ORDER BY l.locktype, c.relname, l.mode"
)
_USER_FOR_UPDATE_NOWAIT = text("SELECT id FROM users WHERE id = :id FOR UPDATE NOWAIT")
_SESSION_XMIN = text("SELECT xmin::text FROM auth_sessions WHERE id = :id")
_AUDIT_XMIN = text(
    "SELECT organization_id, xmin::text FROM audit_logs"
    " WHERE action = 'auth.login_succeeded' AND entity_id = :id"
)
_REFUSAL_ROWS = text(
    "SELECT organization_id, actor_user_id FROM audit_logs"
    " WHERE action = 'auth.login_failed' AND entity_id = :id"
)
WRONG_PASSWORD = "not-the-password-7"


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def pg_env() -> Iterator[Env]:
    require_postgres()
    with environment(None) as e:
        yield e


class _ParkedSignIn:
    """Parks the first caller of ``create_session`` (a sign-in that has verified the
    password) and records the backend pid of that request's transaction."""

    def __init__(self, real) -> None:
        self.real = real
        self.arrived = threading.Event()
        self.go = threading.Event()
        self.pid: int | None = None
        self._taken = False
        self._lock = threading.Lock()

    def __call__(self, db, user):
        with self._lock:
            first, self._taken = not self._taken, True
        if first:
            self.pid = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
            self.arrived.set()
            assert self.go.wait(WAIT), "the parked sign-in was never released"
        return self.real(db, user)


def _observe(env: Env, pid: int) -> tuple[list[tuple], dict]:
    """The granted locks of backend ``pid``, and what they mean for the user row."""
    with env.witness.engine.connect() as conn:
        locks = [tuple(row) for row in conn.execute(_LOCKS, {"pid": pid}).all()]
        conn.rollback()
        try:
            conn.execute(_USER_FOR_UPDATE_NOWAIT, {"id": ALICE}).scalar_one()
            for_update = "granted"
        except OperationalError as exc:
            for_update = f"refused ({getattr(exc.orig, 'sqlstate', None)})"
        finally:
            conn.rollback()
    return locks, {
        "transaction id": any(locktype == "transactionid" for locktype, _, _ in locks),
        "audit_logs locks": [mode for _, mode, rel in locks if rel == "audit_logs"],
        "users locks beyond its read": [
            mode for _, mode, rel in locks if rel == "users" and mode != "AccessShareLock"
        ],
        "a change's FOR UPDATE NOWAIT": for_update,
    }


def test_a_sign_in_parked_before_its_session_holds_no_user_row_lock_and_no_audit_row(
    pg_env, monkeypatch
):
    env = pg_env
    park = _ParkedSignIn(auth_service.create_session)
    monkeypatch.setattr(auth_service, "create_session", park)
    elsewhere = dataclasses.replace(env, client=TestClient(app))
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            signing_in = pool.submit(login, elsewhere, EMAIL[ALICE], OLD_PASSWORD)
            assert park.arrived.wait(WAIT), "the sign-in never reached create_session"
            locks, observed = _observe(env, park.pid)
        finally:
            park.go.set()
        signed_in = signing_in.result(timeout=WAIT)

    assert observed == {
        "transaction id": False,
        "audit_logs locks": [],
        "users locks beyond its read": [],
        "a change's FOR UPDATE NOWAIT": "granted",
    }, locks

    # Released: one answer, one session, one audit row, committed in the same transaction.
    assert signed_in.status_code == 200, signed_in.text
    sid = claims(signed_in.json()["access_token"])["sid"]
    with env.witness.engine.connect() as conn:
        session_xmin = conn.execute(_SESSION_XMIN, {"id": sid}).scalar_one()
        audited = [tuple(row) for row in conn.execute(_AUDIT_XMIN, {"id": ALICE}).all()]
    assert audited == [(ORG_A, session_xmin)]


def test_a_refused_sign_in_holds_no_user_row_lock_while_it_writes_its_audit_rows(
    pg_env, monkeypatch
):
    env = pg_env
    held, release = threading.Event(), threading.Event()
    marked: dict = {}
    real_authenticate = auth_service.authenticate

    def authenticate(db, **kwargs):
        marked["thread"] = threading.get_ident()
        marked["pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar_one()
        return real_authenticate(db, **kwargs)

    def before_commit(_session) -> None:
        if marked.get("thread") == threading.get_ident():
            marked["thread"] = None
            held.set()
            assert release.wait(WAIT), "the held COMMIT was never released"

    monkeypatch.setattr(auth_service, "authenticate", authenticate)
    elsewhere = dataclasses.replace(env, client=TestClient(app))
    event.listen(Session, "before_commit", before_commit)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                refusing = pool.submit(login, elsewhere, EMAIL[ALICE], WRONG_PASSWORD)
                assert held.wait(WAIT), "the refused sign-in never reached its COMMIT"
                locks, observed = _observe(env, marked["pid"])
            finally:
                release.set()
            refused = refusing.result(timeout=WAIT)
    finally:
        event.remove(Session, "before_commit", before_commit)

    # Its refusal row is inserted (uncommitted); the user row is not locked.
    assert observed == {
        "transaction id": True,
        "audit_logs locks": ["RowExclusiveLock"],
        "users locks beyond its read": [],
        "a change's FOR UPDATE NOWAIT": "granted",
    }, locks
    assert refused.status_code == 401, refused.text
    assert refused.json()["error"]["message"] == "Invalid email or password."
    with env.witness.engine.connect() as conn:
        rows = [tuple(row) for row in conn.execute(_REFUSAL_ROWS, {"id": ALICE}).all()]
    assert rows == [(ORG_A, None)]
