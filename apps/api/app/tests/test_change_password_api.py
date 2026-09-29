"""P6-UI-017: authenticated self-service password change (SQLite, request level).

    POST /api/v1/auth/password/change   Bearer, {current_password, new_password}  -> 204

Only ``get_db`` is overridden; committed state is read through a separate ``Engine`` (see
``_auth2_support``). Every test carries its stable id from the P6-UI-017 closure test matrix
(``U17-…``) in its name. The PostgreSQL races (U17-C01…C06, U17-R02, U17-B08p) are in
``test_change_password_database.py``; here SQLite proves the request-level contract:

* **Success (Model A).** 204 with an empty body, no token and no cookie; the hash is replaced
  and ``auth_epoch`` moves by exactly one, so every token of every session -- the one used
  included -- is refused. No session row is created, extended or revoked: the rows stay
  inert, as after a password reset. ``email_verified_at`` and email-verification tokens are
  untouched; every open password-reset token is revoked. The change is committed before
  the 204 and one ``security.password.changed`` event is logged.
* **Refusals.** A wrong current password is 422 ``current_password_incorrect`` (never 401:
  the session survives it). A new password equivalent to the current one under bcrypt's
  72-byte rule is 422 ``password_unchanged``, decided only AFTER the current password
  verified, so it is no guessing oracle. Bounds, missing and unknown fields are 422
  ``validation_error``. Anonymous, revoked, expired, stale-epoch and inactive callers are the
  dependency's unchanged 401s. Every refusal changes nothing.
* **Ordering.** Both bcrypt checks and the new hash run before the user-row lock; under it
  only plain comparisons run. A credential change committed between authentication and the
  lock is a 401 that writes nothing (the SQLite half of U17-C01; the lock itself needs
  PostgreSQL).
* **Leakage.** No password, hash, access token or session id reaches a log record, stdout
  or stderr; the security event carries only allow-listed fields. The 422 ``details`` echo
  is covered where the global handler is (not here).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, update

from app.audit.models import AuditLog
from app.auth import service as auth_service
from app.auth.models import AuthSession, EmailVerificationToken, PasswordResetToken
from app.core.logging import JsonFormatter
from app.core.security import hash_password, verify_password
from app.organizations.models import User
from app.tests._auth2_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    EMAIL,
    INACTIVE,
    LOGOUT,
    NEW_PASSWORD,
    OLD_HASH,
    OLD_PASSWORD,
    RESET_OPEN_INDEX,
    TENANT_MODELS,
    TOKEN_MODELS,
    Env,
    as_utc,
    bearer,
    claims,
    code,
    confirm_reset,
    confirm_verification,
    environment,
    error,
    get_me,
    login,
    mint_reset,
    mint_verification,
    open_session,
    post,
    reset_digest,
)

CHANGE = f"{API}/auth/password/change"
ORGANIZATIONS = f"{API}/organizations"
LIFETIME = timedelta(minutes=720)

UNAUTHORIZED = (401, "unauthorized", "Invalid or expired token.")
MISSING_BEARER = (401, "unauthorized", "Missing bearer token.")
INACTIVE_USER = (401, "unauthorized", "User not found or inactive.")
INCORRECT = (422, "current_password_incorrect", "The current password is incorrect.")
UNCHANGED = (422, "password_unchanged", "Choose a password different from your current one.")
INVALID_BODY = (422, "validation_error")
RESET_INVALID = (404, "password_reset_invalid")

#: Synthetic test values only.
WRONG_PASSWORD = "wrong-password-0"
THIRD_PASSWORD = "third-password-3"

#: Everything a refused change must leave byte-identical: tenants, users, both token
#: tables and every session row.
STATE_MODELS = (*TENANT_MODELS, User, *TOKEN_MODELS, AuthSession)


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "change.db") as e:
        yield e


def change(env: Env, current: str, new: str, **kw):
    return post(env, CHANGE, {"current_password": current, "new_password": new}, **kw)


def state(env: Env):
    return env.witness.snapshot(*STATE_MODELS)


def signed_in(env: Env, user_id: str = ALICE, password: str = OLD_PASSWORD) -> str:
    r = login(env, EMAIL[user_id], password)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def assert_changed(env: Env, r, *, password: str = NEW_PASSWORD, epoch: int = 1) -> None:
    assert (r.status_code, r.content) == (204, b""), r.text
    user = env.witness.user(ALICE)
    assert verify_password(password, user["hashed_password"])
    assert user["auth_epoch"] == epoch


def assert_refused(env: Env, r, expected: tuple, before) -> None:
    assert error(r) == expected, r.text
    assert state(env) == before, "a refused change wrote something"


# --------------------------------------------------------------------------- #
# Log capture (every logger, every rendering)
# --------------------------------------------------------------------------- #
class _Everything(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_NOISY = ("signalnest", "app", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx")
_FORMATTER = JsonFormatter(service="signalnest-api", environment="production")


@contextmanager
def capture_logs() -> Iterator[list[logging.LogRecord]]:
    """Every record from every logger at DEBUG, non-propagating loggers included."""
    handler = _Everything()
    root = logging.getLogger()
    saved_root = root.level
    saved = {name: logging.getLogger(name).level for name in _NOISY}
    detached = [
        lg
        for lg in logging.root.manager.loggerDict.values()
        if isinstance(lg, logging.Logger) and not lg.propagate
    ]
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.DEBUG)
    for lg in detached:
        lg.addHandler(handler)
    try:
        yield handler.records
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_root)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
        for lg in detached:
            lg.removeHandler(handler)


def rendered(rec: logging.LogRecord) -> str:
    parts = [rec.getMessage(), json.dumps(rec.__dict__, default=str), _FORMATTER.format(rec)]
    if rec.exc_info:
        parts.append(logging.Formatter().formatException(rec.exc_info))
    return "\n".join(parts)


def changed_events(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == "security.password.changed"]


# --------------------------------------------------------------------------- #
class TestSuccess:
    def test_u17_b01_success_replaces_the_hash_and_moves_the_epoch_only(self, env: Env):
        token = signed_in(env)
        before, others = env.witness.user(ALICE), env.witness.bystanders(ALICE)
        sessions = env.witness.count(AuthSession)
        t0 = env.witness.db_now()
        with capture_logs() as records:
            r = change(env, OLD_PASSWORD, NEW_PASSWORD, token=token)
        t1 = env.witness.db_now()
        assert_changed(env, r)
        assert "set-cookie" not in r.headers and "access_token" not in r.text
        after = env.witness.user(ALICE)
        assert not verify_password(OLD_PASSWORD, after["hashed_password"])
        assert after["auth_epoch"] == before["auth_epoch"] + 1
        changed = {k for k in before if before[k] != after[k]}
        assert {"hashed_password", "auth_epoch"} <= changed
        assert changed <= {"hashed_password", "auth_epoch", "updated_at"}
        assert after["email_verified_at"] is None  # a password proves nothing about the mailbox
        assert t0 <= as_utc(after["updated_at"]) <= t1  # the database clock
        assert env.witness.count(AuthSession) == sessions  # no session opened by the change
        assert env.witness.bystanders(ALICE) == others
        [event_record] = changed_events(records)
        assert event_record.extra_fields == {"outcome": "success", "user_id": ALICE}

    def test_u17_b01_the_change_is_committed_before_the_204(self, tmp_path):
        """Under a teardown that never commits, the change still stands."""
        with environment(tmp_path / "withheld.db", teardown_commit=False) as env:
            r = change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE)
            assert_changed(env, r)

    def test_u17_b10_the_old_password_no_longer_signs_in(self, env: Env):
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE))
        r = login(env, EMAIL[ALICE], OLD_PASSWORD)
        assert error(r) == (401, "unauthorized", "Invalid email or password.")

    def test_u17_b11_the_new_password_signs_in_on_the_new_epoch(self, env: Env):
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE))
        fresh = signed_in(env, ALICE, NEW_PASSWORD)
        assert claims(fresh)["auth_epoch"] == env.witness.user(ALICE)["auth_epoch"] == 1
        assert get_me(env, fresh).status_code == 200


# --------------------------------------------------------------------------- #
class TestCurrentPassword:
    @pytest.mark.parametrize(
        "wrong",
        [WRONG_PASSWORD, OLD_PASSWORD.upper(), OLD_PASSWORD + "x", OLD_PASSWORD[:-1]],
        ids=["different", "upper-case", "suffixed", "truncated"],
    )
    def test_u17_b02_a_wrong_current_password_is_422_and_keeps_the_session(
        self, env: Env, wrong: str
    ):
        token = signed_in(env)
        assert get_me(env, token).status_code == 200
        before = state(env)
        with capture_logs() as records:
            r = change(env, wrong, NEW_PASSWORD, token=token)
        assert_refused(env, r, INCORRECT, before)
        assert changed_events(records) == []
        # Never a 401: the SAME token still authenticates, and can still change the password.
        assert get_me(env, token).status_code == 200
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=token))


# --------------------------------------------------------------------------- #
class TestNewPasswordBounds:
    def test_u17_b03_seven_characters_is_422_and_eight_is_accepted(self, env: Env):
        token = signed_in(env)
        before = state(env)
        assert code(change(env, OLD_PASSWORD, "q" * 7, token=token)) == INVALID_BODY
        assert state(env) == before
        assert_changed(env, change(env, OLD_PASSWORD, "q" * 8, token=token), password="q" * 8)
        assert login(env, EMAIL[ALICE], "q" * 8).status_code == 200

    def test_u17_b04_129_characters_is_422_and_128_is_accepted(self, env: Env):
        token = signed_in(env)
        before = state(env)
        assert code(change(env, OLD_PASSWORD, "q" * 129, token=token)) == INVALID_BODY
        assert state(env) == before
        assert_changed(env, change(env, OLD_PASSWORD, "q" * 128, token=token), password="q" * 128)
        assert login(env, EMAIL[ALICE], "q" * 128).status_code == 200


# --------------------------------------------------------------------------- #
#: Two 80-character passwords that share their first 72 bytes: bcrypt cannot tell them apart.
LONG_A = "L" * 72 + "11111111"
LONG_B = "L" * 72 + "22222222"
#: The same, in multi-byte characters: 24 x 3-byte "€" = 72 bytes, then different tails.
EURO_A = "€" * 24 + "-tail-one"
EURO_B = "€" * 24 + "-tail-two"


class TestSamePassword:
    """FD-U17-2: the server rejects a new password equivalent to the current credential."""

    def _set_password(self, env: Env, password: str) -> None:
        """Store ``password``'s hash directly; the epoch does not move."""
        env.witness.write(
            update(User).where(User.id == ALICE).values(hashed_password=hash_password(password))
        )

    def test_u17_b05_the_current_password_as_the_new_one_is_422(self, env: Env):
        token = signed_in(env)
        before = state(env)
        assert_refused(env, change(env, OLD_PASSWORD, OLD_PASSWORD, token=token), UNCHANGED, before)
        assert get_me(env, token).status_code == 200  # a field error, not a session error

    @pytest.mark.parametrize(
        "current, new", [(LONG_A, LONG_B), (EURO_A, EURO_B)], ids=["ascii-80", "multibyte"]
    )
    def test_u17_b05b_the_same_first_72_bytes_is_the_same_credential(
        self, env: Env, current: str, new: str
    ):
        assert current != new and current.encode()[:72] == new.encode()[:72]
        assert len(LONG_A) == len(LONG_B) == 80
        self._set_password(env, current)
        token = bearer(env, ALICE)
        before = state(env)
        assert_refused(env, change(env, current, new, token=token), UNCHANGED, before)
        # Positive control: a password differing INSIDE the first 72 bytes is accepted.
        differing = new[:10] + "#" + new[11:]
        assert current.encode()[:72] != differing.encode()[:72]
        assert_changed(env, change(env, current, differing, token=token), password=differing)

    @pytest.mark.parametrize(
        "current, new, real",
        [
            (WRONG_PASSWORD, OLD_PASSWORD, OLD_PASSWORD),
            ("X" + LONG_B[1:], LONG_B, LONG_A),
        ],
        ids=["new-is-the-real-password", "new-shares-the-real-72-bytes"],
    )
    def test_u17_b05c_a_wrong_current_password_wins_over_the_same_password_rule(
        self, env: Env, current: str, new: str, real: str
    ):
        """No oracle: with a wrong current password, a new password equal to the real one
        answers exactly like any other wrong attempt."""
        self._set_password(env, real)
        assert not verify_password(current, env.witness.user(ALICE)["hashed_password"])
        token = bearer(env, ALICE)
        before = state(env)
        assert_refused(env, change(env, current, new, token=token), INCORRECT, before)
        assert_refused(env, change(env, current, THIRD_PASSWORD, token=token), INCORRECT, before)

    @pytest.mark.parametrize("case", ["wrong-current", "unchanged", "success"])
    def test_u17_b05_bcrypt_runs_before_the_lock_and_the_same_check_after_verification(
        self, env: Env, monkeypatch, case: str
    ):
        """The order of the expensive and the locking steps, spied at the service's own
        bindings: verify(current) -> verify(new) -> hash -> lock -> the two UPDATEs."""
        steps: list[str] = []
        real_verify, real_hash, real_lock = (
            auth_service.verify_password,
            auth_service.hash_password,
            auth_service._lock_user,
        )
        passwords = {OLD_PASSWORD: "current", WRONG_PASSWORD: "current", NEW_PASSWORD: "new"}

        def verify(plain, hashed):
            steps.append(f"verify {passwords[plain]}")
            return real_verify(plain, hashed)

        def hashing(plain):
            steps.append(f"hash {passwords[plain]}")
            return real_hash(plain)

        def lock(db, *criteria):
            steps.append("lock")
            return real_lock(db, *criteria)

        def statements(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("UPDATE"):
                steps.append(" ".join(statement.split()[:2]).upper())

        token = bearer(env, ALICE)
        new = {"wrong-current": NEW_PASSWORD, "unchanged": OLD_PASSWORD, "success": NEW_PASSWORD}
        current = WRONG_PASSWORD if case == "wrong-current" else OLD_PASSWORD
        monkeypatch.setattr(auth_service, "verify_password", verify)
        monkeypatch.setattr(auth_service, "hash_password", hashing)
        monkeypatch.setattr(auth_service, "_lock_user", lock)
        event.listen(env.request_engine, "before_cursor_execute", statements)
        try:
            r = change(env, current, new[case], token=token)
        finally:
            event.remove(env.request_engine, "before_cursor_execute", statements)
        expected = {
            "wrong-current": ["verify current"],
            "unchanged": ["verify current", "verify current"],
            "success": [
                "verify current",
                "verify new",
                "hash new",
                "lock",
                "UPDATE USERS",
                "UPDATE PASSWORD_RESET_TOKENS",
            ],
        }[case]
        assert steps == expected, (r.status_code, steps)
        assert r.status_code == {"wrong-current": 422, "unchanged": 422, "success": 204}[case]


# --------------------------------------------------------------------------- #
class TestAuthentication:
    @pytest.mark.parametrize(
        "body",
        [
            {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
            {},
            {"current_password": "", "new_password": "short"},
            {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD, "user_id": ALICE},
        ],
        ids=["valid", "empty", "out-of-bounds", "extra-field"],
    )
    def test_u17_b06_anonymous_is_401_even_with_an_invalid_body(self, env: Env, body: dict):
        before = state(env)
        assert_refused(env, post(env, CHANGE, body), MISSING_BEARER, before)
        garbage = {"Authorization": "Bearer not-a-jwt"}
        assert_refused(env, post(env, CHANGE, body, headers=garbage), UNAUTHORIZED, before)

    def test_u17_b07_a_revoked_session_is_401(self, env: Env):
        token = signed_in(env)
        assert get_me(env, token).status_code == 200
        assert post(env, LOGOUT, None, token=token).status_code == 204
        before = state(env)
        assert_refused(
            env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=token), UNAUTHORIZED, before
        )

    def test_u17_b08_an_expired_session_is_401(self, env: Env):
        token, sid = open_session(env.request_engine, ALICE)
        assert get_me(env, token).status_code == 200
        past = env.witness.db_now() - timedelta(seconds=1)
        env.witness.write(update(AuthSession).where(AuthSession.id == sid).values(expires_at=past))
        before = state(env)
        assert_refused(
            env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=token), UNAUTHORIZED, before
        )

    def test_u17_b09_a_token_from_before_an_epoch_change_is_401(self, env: Env):
        stale = bearer(env, ALICE)
        assert confirm_reset(env, mint_reset(env, ALICE)).status_code == 204  # epoch 0 -> 1
        assert env.witness.session_row(claims(stale)["sid"])["revoked_at"] is None
        before = state(env)
        r = change(env, NEW_PASSWORD, THIRD_PASSWORD, token=stale)
        assert_refused(env, r, UNAUTHORIZED, before)
        assert verify_password(NEW_PASSWORD, env.witness.user(ALICE)["hashed_password"])

    def test_u17_b14_an_inactive_account_is_401(self, env: Env):
        inactive = bearer(env, INACTIVE)
        active = bearer(env, ALICE)
        before = state(env)
        assert_refused(
            env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=inactive), INACTIVE_USER, before
        )
        # Deactivated after its token was issued.
        env.witness.write(update(User).where(User.id == ALICE).values(is_active=False))
        before = state(env)
        assert_refused(
            env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=active), INACTIVE_USER, before
        )
        assert verify_password(OLD_PASSWORD, env.witness.user(ALICE)["hashed_password"])


# --------------------------------------------------------------------------- #
class TestBody:
    @pytest.mark.parametrize(
        "body",
        [
            {"new_password": NEW_PASSWORD},
            {"current_password": OLD_PASSWORD},
            {},
            {"current_password": "", "new_password": NEW_PASSWORD},
            {"current_password": "c" * 129, "new_password": NEW_PASSWORD},
            {"current_password": None, "new_password": NEW_PASSWORD},
            {"current_password": OLD_PASSWORD, "new_password": 12345678},
        ],
        ids=[
            "no-current",
            "no-new",
            "empty",
            "empty-current",
            "overlong-current",
            "null-current",
            "number-new",
        ],
    )
    def test_u17_b12_a_missing_or_malformed_field_is_422(self, env: Env, body: dict):
        token = bearer(env, ALICE)
        before = state(env)
        assert code(post(env, CHANGE, body, token=token)) == INVALID_BODY
        assert state(env) == before
        assert get_me(env, token).status_code == 200

    @pytest.mark.parametrize(
        "extra", ["confirm_password", "new_password_confirmation", "user_id", "email", "auth_epoch"]
    )
    def test_u17_b13_an_unknown_field_is_422(self, env: Env, extra: str):
        token = bearer(env, ALICE)
        before = state(env)
        body = {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD, extra: BOB}
        assert code(post(env, CHANGE, body, token=token)) == INVALID_BODY
        assert state(env) == before


# --------------------------------------------------------------------------- #
class TestBystanders:
    def test_u17_b15_other_accounts_are_untouched(self, env: Env):
        bob, carol = signed_in(env, BOB), bearer(env, CAROL)
        bob_reset = mint_reset(env, BOB)
        tenants = env.witness.snapshot(*TENANT_MODELS)
        others = env.witness.bystanders(ALICE)
        other_sessions = [r for r in env.witness.sessions() if r["user_id"] != ALICE]
        other_resets = [r for r in env.witness.reset_tokens() if r["user_id"] != ALICE]
        verification = env.witness.verification_tokens()
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE))
        assert env.witness.snapshot(*TENANT_MODELS) == tenants
        assert env.witness.bystanders(ALICE) == others
        assert [r for r in env.witness.sessions() if r["user_id"] != ALICE] == other_sessions
        assert [r for r in env.witness.reset_tokens() if r["user_id"] != ALICE] == other_resets
        assert env.witness.verification_tokens() == verification
        assert [get_me(env, t).status_code for t in (bob, carol)] == [200, 200]
        assert confirm_reset(env, bob_reset, THIRD_PASSWORD).status_code == 204


# --------------------------------------------------------------------------- #
class TestSessionModelA:
    def test_u17_ma01_every_token_of_every_session_is_refused(self, env: Env):
        first = signed_in(env)
        reissued = get_me(env, first).json()["access_token"]
        second, third = bearer(env, ALICE), bearer(env, ALICE)
        tokens = [first, reissued, second, third]
        for token in tokens:
            assert get_me(env, token).status_code == 200
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=second))
        for token in tokens:
            assert error(get_me(env, token)) == UNAUTHORIZED
            active = env.client.get(ORGANIZATIONS, headers={"Authorization": f"Bearer {token}"})
            assert error(active) == UNAUTHORIZED
        # The token that made the change cannot make another one.
        before = state(env)
        r = change(env, NEW_PASSWORD, THIRD_PASSWORD, token=second)
        assert_refused(env, r, UNAUTHORIZED, before)

    def test_u17_ma02_no_session_is_created_extended_or_revoked(self, env: Env):
        tokens = [signed_in(env), bearer(env, ALICE)]
        rows = env.witness.sessions(ALICE)
        opened = env.witness.count(AuthSession)
        assert len(rows) == 2 and all(r["revoked_at"] is None for r in rows)
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=tokens[0]))
        # Rows byte-identical: none revoked, none extended; the epoch alone makes them inert.
        assert env.witness.sessions(ALICE) == rows
        assert env.witness.count(AuthSession) == opened
        assert [get_me(env, t).status_code for t in tokens] == [401, 401]

    def test_u17_ma03_a_fresh_sign_in_opens_a_fresh_twelve_hour_session(self, env: Env):
        old = signed_in(env)
        opened = env.witness.count(AuthSession)
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, token=old))
        assert env.witness.count(AuthSession) == opened  # the change opened nothing
        t0 = env.witness.db_now()
        fresh = signed_in(env, ALICE, NEW_PASSWORD)
        t1 = env.witness.db_now()
        assert env.witness.count(AuthSession) == opened + 1  # the sign-in opened one
        sid = claims(fresh)["sid"]
        assert sid != claims(old)["sid"]
        row = env.witness.session_row(sid)
        created, expires = as_utc(row["created_at"]), as_utc(row["expires_at"])
        assert t0 <= created <= t1
        assert expires - created == LIFETIME
        assert get_me(env, fresh).status_code == 200
        assert error(get_me(env, old)) == UNAUTHORIZED


# --------------------------------------------------------------------------- #
class TestAccountTokens:
    def test_u17_r01_a_reset_token_issued_before_the_change_is_404_afterwards(self, env: Env):
        raw = mint_reset(env, ALICE)
        [issued] = env.witness.reset_tokens(ALICE)
        t0 = env.witness.db_now()
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE))
        t1 = env.witness.db_now()
        [revoked] = env.witness.reset_tokens(ALICE)
        assert revoked["id"] == issued["id"] and revoked["used_at"] is None
        assert t0 <= as_utc(revoked["revoked_at"]) <= t1  # the database clock
        before = state(env)
        assert code(confirm_reset(env, raw, THIRD_PASSWORD)) == RESET_INVALID
        assert state(env) == before
        user = env.witness.user(ALICE)
        assert verify_password(NEW_PASSWORD, user["hashed_password"]) and user["auth_epoch"] == 1
        assert login(env, EMAIL[ALICE], NEW_PASSWORD).status_code == 200

    def test_u17_r01_every_open_reset_token_is_revoked_and_history_kept(self, env: Env):
        """Defence in depth under the one-open-token index: with the index removed, two open
        tokens coexist and the change revokes both; used and revoked history is untouched."""
        env.witness.drop_index(RESET_OPEN_INDEX)
        plant = env.witness.plant
        open_ids = [
            plant(PasswordResetToken, user_id=ALICE, digest=reset_digest(name))
            for name in ("one", "two")
        ]
        used = plant(PasswordResetToken, user_id=ALICE, digest=reset_digest("u"), used=True)
        gone = plant(PasswordResetToken, user_id=ALICE, digest=reset_digest("r"), revoked=True)
        history = [env.witness.token(PasswordResetToken, i) for i in (used, gone)]
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE))
        for token_id in open_ids:
            row = env.witness.token(PasswordResetToken, token_id)
            assert row["revoked_at"] is not None and row["used_at"] is None
        assert [env.witness.token(PasswordResetToken, i) for i in (used, gone)] == history
        for name in ("one", "two"):
            assert code(confirm_reset(env, name, THIRD_PASSWORD)) == RESET_INVALID

    def test_u17_r03_email_verification_is_untouched(self, env: Env):
        raw = mint_verification(env, ALICE)
        rows = env.witness.verification_tokens()
        assert env.witness.user(ALICE)["email_verified_at"] is None
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE))
        assert env.witness.verification_tokens() == rows
        assert env.witness.user(ALICE)["email_verified_at"] is None
        # The open verification token still verifies the address, from a new session.
        assert confirm_verification(env, raw, ALICE).status_code == 204
        assert env.witness.user(ALICE)["email_verified_at"] is not None

    def test_u17_r03_an_earlier_verification_is_kept(self, env: Env):
        earlier = env.witness.db_now() - timedelta(days=3)
        env.witness.write(update(User).where(User.id == ALICE).values(email_verified_at=earlier))
        assert_changed(env, change(env, OLD_PASSWORD, NEW_PASSWORD, user_id=ALICE))
        assert as_utc(env.witness.user(ALICE)["email_verified_at"]) == earlier
        assert env.witness.verification_tokens(ALICE) == []
        assert env.witness.count(EmailVerificationToken) == 0


# --------------------------------------------------------------------------- #
def _competitor(kind: str):
    """A credential change committed on a separate connection, as a concurrent change or
    reset would leave it."""

    def act(env: Env) -> None:
        values = {
            "epoch": {"auth_epoch": User.auth_epoch + 1},
            # The same password, freshly salted: only the stored hash STRING differs.
            "hash": {"hashed_password": hash_password(OLD_PASSWORD)},
            "reset": {"hashed_password": hash_password(THIRD_PASSWORD), "auth_epoch": 1},
            "inactive": {"is_active": False},
            "none": None,
        }[kind]
        if values is not None:
            assert env.witness.write(update(User).where(User.id == ALICE).values(**values)) == 1

    return act


class TestRecheckUnderTheLock:
    """The SQLite half of U17-C01: the under-lock re-check (the lock itself needs PostgreSQL).

    Immediately before the request locks the user row -- after it authenticated and after
    both bcrypt checks passed -- a competing credential change is committed on a separate
    connection. Only the re-check of the locked row (active, same epoch, same stored hash
    string) can refuse the request now; it must be a 401 that writes nothing.
    """

    @pytest.mark.parametrize("kind", ["epoch", "hash", "reset", "inactive", "none"])
    def test_u17_c01_a_change_committed_before_the_lock_is_401(
        self, env: Env, monkeypatch, kind: str
    ):
        token = bearer(env, ALICE)
        mint_reset(env, ALICE)
        real_lock = auth_service._lock_user
        fired: list[str] = []
        after: dict = {}

        def lock(db, *criteria):
            fired.append(kind)
            _competitor(kind)(env)
            after.update(state(env))
            return real_lock(db, *criteria)

        monkeypatch.setattr(auth_service, "_lock_user", lock)
        r = change(env, OLD_PASSWORD, NEW_PASSWORD, token=token)
        assert fired == [kind], "the request never reached the lock"
        if kind == "none":  # positive control: the hook itself breaks nothing
            assert_changed(env, r)
            return
        assert error(r) == UNAUTHORIZED
        assert state(env) == after  # the reset token is still open; the user row is theirs
        assert not verify_password(NEW_PASSWORD, env.witness.user(ALICE)["hashed_password"])


# --------------------------------------------------------------------------- #
def _drive_every_path(env: Env) -> tuple[list[str], list]:
    """Every outcome of the route: returns (access tokens and sids used, responses)."""
    token, sid = open_session(env.request_engine, ALICE)
    other, other_sid = open_session(env.request_engine, ALICE)
    responses = [
        change(env, WRONG_PASSWORD, NEW_PASSWORD, token=token),  # 422 incorrect
        change(env, OLD_PASSWORD, OLD_PASSWORD, token=token),  # 422 unchanged
        change(env, OLD_PASSWORD, "short7!", token=token),  # 422 bounds
        post(env, CHANGE, {"current_password": OLD_PASSWORD}, token=token),  # 422 missing
        post(env, CHANGE, {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD, "x": 1}),
        change(env, OLD_PASSWORD, NEW_PASSWORD),  # 401 anonymous
        change(env, OLD_PASSWORD, NEW_PASSWORD, token=token),  # 204
        change(env, NEW_PASSWORD, THIRD_PASSWORD, token=other),  # 401 stale epoch
        login(env, EMAIL[ALICE], NEW_PASSWORD),  # 200
    ]
    assert [r.status_code for r in responses] == [422, 422, 422, 422, 401, 401, 204, 401, 200]
    fresh = responses[-1].json()["access_token"]
    return [token, sid, other, other_sid, fresh, claims(fresh)["sid"]], responses


class TestLeakage:
    def test_u17_s01_no_password_hash_token_or_sid_reaches_a_log(self, env: Env, capsys):
        with capture_logs() as records:
            credentials, _ = _drive_every_path(env)
        out = capsys.readouterr()
        names = [r.getMessage() for r in records]
        # Positive controls: the capture saw the request log and exactly one change event.
        assert any(r.name == "signalnest.request" for r in records), sorted(set(names))
        assert names.count("security.password.changed") == 1, sorted(set(names))
        new_hash = env.witness.user(ALICE)["hashed_password"]
        secrets = [
            OLD_PASSWORD,
            NEW_PASSWORD,
            WRONG_PASSWORD,
            THIRD_PASSWORD,
            "short7!",
            OLD_HASH,
            new_hash,
            "$2b$",
            EMAIL[ALICE],
            *credentials,
        ]
        forbidden_keys = {"sid", "session_id", "password", "current_password", "new_password"}
        forbidden_keys |= {"hashed_password", "token", "access_token", "email"}
        for rec in records:
            text = rendered(rec)
            for secret in secrets:
                assert secret not in text, (rec.name, rec.getMessage(), secret[:12])
            fields = set(getattr(rec, "extra_fields", {}) or {})
            assert not fields & forbidden_keys, (rec.getMessage(), fields & forbidden_keys)
        for stream in (out.out, out.err):
            for secret in secrets:
                assert secret not in stream, secret[:12]

    def test_u17_s02_the_event_carries_only_allow_listed_fields(self, env: Env):
        audits = env.witness.count(AuditLog)
        with capture_logs() as records:
            _drive_every_path(env)
        allowed = {"outcome", "user_id", "error_class", "duration_ms"}
        seen = 0
        for rec in records:
            if not rec.getMessage().startswith("security."):
                continue
            fields = getattr(rec, "extra_fields", {}) or {}
            assert set(fields) <= allowed, (rec.getMessage(), set(fields) - allowed)
            if rec.getMessage() == "security.password.changed":
                seen += 1
                assert fields == {"outcome": "success", "user_id": ALICE}  # an id, not an address
        assert seen == 1  # only the success logs it; no refusal does
        assert env.witness.count(AuditLog) == audits  # account-level: no audit row

    def test_u17_s03_responses_carry_no_body_cookie_or_hash(self, env: Env):
        """The response half of U17-S03: an empty 204, no cookie, and no hash anywhere."""
        _, responses = _drive_every_path(env)
        success = responses[6]
        assert (success.status_code, success.content) == (204, b"")
        assert "content-type" not in success.headers
        new_hash = env.witness.user(ALICE)["hashed_password"]
        for r in responses:
            assert "set-cookie" not in r.headers, r.status_code
            wire = r.text + json.dumps(dict(r.headers))
            for secret in (OLD_HASH, new_hash, "$2b$", "hashed_password"):
                assert secret not in wire, (r.status_code, secret[:12])
