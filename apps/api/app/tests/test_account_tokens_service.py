"""6B-4A: ``app.auth.account_tokens`` called directly, as the routes call it.

The API tests cannot see some load-bearing properties because the request schema refuses a
bad password before the service runs, and because a route commits whatever the service
leaves. Here the service is called on a plain session over the seeded database:

* **The new password is validated before anything else** -- no statement at all reaches the
  database for a too-short or too-long password, so a rejected password can never spend (or
  even look up) a token.
* **One reading of the database clock per operation**: every timestamp an operation writes
  (``created_at``, ``updated_at``, ``expires_at``, ``used_at``, ``revoked_at``, and the
  account's ``email_verified_at``) derives from the same instant.
* **Requests never raise** for any account class; they return an ``IssuedToken`` whose
  recipient is the stored address, or ``None``.
* **A wrong-account verification is decided before any write**, and **the background revoke
  helpers** revoke only an open token, in a session of their own, and never raise -- not even
  when their database is unusable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.auth import account_tokens
from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationDomainError,
)
from app.db.session import SessionLocal
from app.organizations.models import User
from app.tests._auth2_support import (
    ALICE,
    BOB,
    DANA,
    EMAIL,
    INACTIVE,
    UNKNOWN_EMAIL,
    Env,
    as_utc,
    environment,
    reset_digest,
    verification_digest,
)


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "service.db") as e:
        yield e


def _session(env: Env):
    return sessionmaker(bind=env.request_engine, autoflush=False, expire_on_commit=False)()


def _request_reset(env: Env, email: str):
    with _session(env) as s:
        issued = account_tokens.request_password_reset(s, email=email)
        s.commit()
        return issued


def _request_verification(env: Env, user_id: str):
    with _session(env) as s:
        issued = account_tokens.request_email_verification(s, user=s.get(User, user_id))
        s.commit()
        return issued


class _Statements:
    """Counts the statements the request engine executes while armed."""

    def __init__(self, engine) -> None:
        self.engine, self.seen = engine, []

    def _hook(self, conn, cursor, statement, parameters, context, executemany):
        self.seen.append(statement)

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._hook)
        return self.seen

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._hook)


# --------------------------------------------------------------------------- #
class TestPasswordFirst:
    @pytest.mark.parametrize("password", ["short7!", "p" * 129, ""], ids=["short", "long", "empty"])
    def test_a_bad_password_is_refused_before_any_statement(self, env: Env, password: str):
        issued = _request_reset(env, EMAIL[ALICE])
        before = env.witness.snapshot()
        with _session(env) as s, _Statements(env.request_engine) as seen:
            with pytest.raises(ValidationDomainError) as caught:
                account_tokens.confirm_password_reset(
                    s, token=issued.raw_token, new_password=password
                )
            s.rollback()
        assert seen == [], seen
        assert caught.value.code == "validation_error" and issued.raw_token not in str(caught.value)
        assert env.witness.snapshot() == before

    def test_the_bad_password_wins_even_over_an_unknown_token(self, env: Env):
        with _session(env) as s, pytest.raises(ValidationDomainError):
            account_tokens.confirm_password_reset(s, token="A" * 43, new_password="short")


# --------------------------------------------------------------------------- #
class TestIssuing:
    def test_a_reset_request_returns_the_issued_token(self, env: Env):
        issued = _request_reset(env, EMAIL[DANA])
        [row] = env.witness.reset_tokens(DANA)
        assert issued.token_id == row["id"]
        assert issued.recipient == EMAIL[DANA]  # the stored address, case kept
        assert reset_digest(issued.raw_token) == row["token_hash"]
        assert as_utc(issued.expires_at) == as_utc(row["expires_at"])
        assert len(issued.raw_token) >= 43
        assert issued.raw_token not in repr(issued) and EMAIL[DANA] not in repr(issued)

    @pytest.mark.parametrize("case", ["unknown", "inactive", "cooldown", "cap"])
    def test_a_suppressed_reset_request_returns_none_and_never_raises(self, env: Env, case: str):
        email = {"unknown": UNKNOWN_EMAIL, "inactive": EMAIL[INACTIVE]}.get(case, EMAIL[ALICE])
        if case == "cooldown":
            assert _request_reset(env, email) is not None
        if case == "cap":
            for i in range(get_settings().auth_mail_daily_cap):
                env.witness.plant(
                    PasswordResetToken,
                    user_id=ALICE,
                    digest=f"{i:064x}",
                    age=timedelta(hours=2),
                    revoked=True,
                )
        before = env.witness.snapshot()
        assert _request_reset(env, email) is None
        assert env.witness.snapshot() == before

    def test_one_clock_reading_per_reset_request(self, env: Env):
        _request_reset(env, EMAIL[ALICE])
        env.witness.backdate(
            PasswordResetToken,
            ALICE,
            timedelta(seconds=get_settings().auth_mail_cooldown_seconds + 1),
        )
        _request_reset(env, EMAIL[ALICE])
        old, new = env.witness.reset_tokens(ALICE)
        ttl = timedelta(minutes=get_settings().password_reset_token_ttl_minutes)
        assert new["created_at"] == new["updated_at"]
        assert as_utc(new["expires_at"]) - as_utc(new["created_at"]) == ttl
        # The supersession's revoke and the new token's creation are the same instant.
        assert old["revoked_at"] == old["updated_at"] == new["created_at"]

    def test_one_clock_reading_per_reset_confirm(self, env: Env):
        issued = _request_reset(env, EMAIL[ALICE])
        with _session(env) as s:
            account_tokens.confirm_password_reset(
                s, token=issued.raw_token, new_password="brand-new-password"
            )
            s.commit()
        [row] = env.witness.reset_tokens(ALICE)
        user = env.witness.user(ALICE)
        assert row["used_at"] == row["updated_at"] == user["email_verified_at"]

    def test_verification_request_binds_the_stored_address(self, env: Env):
        issued = _request_verification(env, DANA)
        [row] = env.witness.verification_tokens(DANA)
        assert (issued.recipient, row["email_snapshot"]) == (EMAIL[DANA], EMAIL[DANA])
        assert verification_digest(issued.raw_token) == row["token_hash"]
        assert row["created_at"] == row["updated_at"]

    def test_verification_request_for_a_verified_address_is_a_conflict(self, env: Env):
        now = env.witness.db_now()
        env.witness.write(update(User).where(User.id == ALICE).values(email_verified_at=now))
        with _session(env) as s, pytest.raises(ConflictError) as caught:
            account_tokens.request_email_verification(s, user=s.get(User, ALICE))
        assert caught.value.code == "email_already_verified"

    def test_one_clock_reading_per_verification_confirm(self, env: Env):
        issued = _request_verification(env, ALICE)
        with _session(env) as s:
            account_tokens.confirm_email_verification(
                s, user=s.get(User, ALICE), token=issued.raw_token
            )
            s.commit()
        [row] = env.witness.verification_tokens(ALICE)
        assert row["used_at"] == row["updated_at"] == env.witness.user(ALICE)["email_verified_at"]


# --------------------------------------------------------------------------- #
class TestConfirmErrors:
    def test_reset_errors_are_one_static_not_found(self, env: Env):
        issued = _request_reset(env, EMAIL[ALICE])
        messages = set()
        for raw in ("A" * 43, "é" * 10, issued.raw_token + "x"):
            with _session(env) as s, pytest.raises(NotFoundError) as caught:
                account_tokens.confirm_password_reset(s, token=raw, new_password="long-enough-1")
            assert caught.value.code == "password_reset_invalid"
            assert raw not in caught.value.message
            messages.add(caught.value.message)
        assert len(messages) == 1

    def test_a_wrong_account_verification_writes_nothing(self, env: Env):
        issued = _request_verification(env, ALICE)
        before = env.witness.snapshot()
        with _session(env) as s, _Statements(env.request_engine) as seen:
            with pytest.raises(PermissionDeniedError) as caught:
                account_tokens.confirm_email_verification(
                    s, user=s.get(User, BOB), token=issued.raw_token
                )
            s.rollback()
        assert caught.value.code == "email_verification_wrong_account"
        writes = [q for q in seen if q.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
        assert writes == []
        assert env.witness.snapshot() == before


# --------------------------------------------------------------------------- #
class TestBackgroundRevoke:
    """``revoke_*_token`` runs after the response on ``SessionLocal()`` (bound to the test
    database by the environment) and must never raise."""

    @pytest.mark.parametrize(
        ("model", "revoke"),
        [
            (PasswordResetToken, account_tokens.revoke_password_reset_token),
            (EmailVerificationToken, account_tokens.revoke_email_verification_token),
        ],
        ids=["reset", "verification"],
    )
    def test_revokes_an_open_token_and_leaves_a_used_one(self, env: Env, model, revoke):
        digest = reset_digest if model is PasswordResetToken else verification_digest
        open_id = env.witness.plant(model, user_id=ALICE, digest=digest("open"))
        used_id = env.witness.plant(model, user_id=BOB, digest=digest("used"), used=True)
        used_before = env.witness.token(model, used_id)
        revoke(open_id)
        revoke(used_id)
        revoke("0" * 32)  # unknown: a no-op
        assert env.witness.token(model, open_id)["revoked_at"] is not None
        assert env.witness.token(model, used_id) == used_before

    @pytest.mark.parametrize(
        "revoke",
        [
            account_tokens.revoke_password_reset_token,
            account_tokens.revoke_email_verification_token,
        ],
        ids=["reset", "verification"],
    )
    def test_never_raises_on_an_unusable_database(self, tmp_path, monkeypatch, caplog, revoke):
        caplog.set_level(logging.DEBUG)
        empty = create_engine(f"sqlite:///{tmp_path / 'no-tables.db'}")
        monkeypatch.setitem(SessionLocal.kw, "bind", empty)
        try:
            revoke("0" * 32)  # no table at all: must be swallowed
        finally:
            empty.dispose()
        assert any("revoke_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw", ["x", "A" * 43, "é-non-ascii"])
def test_the_services_digests_match_the_contract_formula(raw: str):
    """M1's helpers against the independent computation used throughout these tests."""
    assert account_tokens.hash_password_reset_token(raw) == reset_digest(raw)
    assert account_tokens.hash_email_verification_token(raw) == verification_digest(raw)
    assert (account_tokens.MIN_PASSWORD_LENGTH, account_tokens.MAX_PASSWORD_LENGTH) == (8, 128)


def test_the_user_lock_compiles_to_for_update_on_postgresql():
    """The serialization the PostgreSQL races rely on, proven without a live database."""
    sql = str(
        account_tokens._user_lock_select(User.id == "u").compile(dialect=postgresql.dialect())
    )
    assert sql.rstrip().endswith("FOR UPDATE"), sql
