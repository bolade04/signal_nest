"""6B-4A: email verification (P6-AUTH-2; FD-4..FD-7, FD-10).

    POST /api/v1/auth/email-verification/request   Bearer, {}        -> 204 | 409
    POST /api/v1/auth/email-verification/confirm   Bearer, {token}   -> 204 | 403 | 404

Verification is a soft status (FD-4): ``SessionOut.user.email_verified`` reports it and
nothing is gated on it. Only ``get_db`` is overridden; mail goes to the in-memory backend
selected through settings (see ``_auth2_support``).

Load-bearing properties, each with its own test:

* **The address is the account's own.** The request body is an empty object (anything else
  is 422); the message goes to the stored address, with a ``/verify-email#token=`` link from
  ``public_web_origin``; the token row snapshots that address.
* **Only the token's own account can spend it** (FD-10). Another signed-in account gets 403
  ``email_verification_wrong_account`` and NOTHING moves -- the right account can still use
  it. Unauthenticated callers get 401 on both routes.
* **Dead tokens are one 404** ``email_verification_invalid``: unknown, expired, used, revoked,
  superseded, and a token whose snapshot no longer matches the stored address (which spends
  nothing: restoring the address makes it usable again).
* **Resend is bounded** by the same per-account cooldown and daily cap as reset, and a new
  mint supersedes the previous open token. An already verified address is 409
  ``email_already_verified``.
* **Only verification and a completed reset verify an address.** Invitation acceptance never
  does (FD-5); existing and newly registered accounts start unverified (FD-6); a reset does
  (FD-7). Verification never touches the password or the epoch.
* **The claim is conditional**, and **both routes commit before answering**.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import event, update

from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.core.config import get_settings
from app.organizations.invitations import MAX_TOKEN_LENGTH
from app.organizations.models import User
from app.tests._auth2_support import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    EMAIL,
    INACTIVE,
    INVITATION_ACCEPT,
    INVITATION_REGISTER,
    OLD_PASSWORD,
    REGISTER,
    TENANT_MODELS,
    VERIFICATION_COLUMNS,
    VERIFICATION_OPEN_INDEX,
    VERIFY_CONFIRM,
    VERIFY_PATH,
    VERIFY_REQUEST,
    Env,
    as_utc,
    bearer,
    code,
    confirm_reset,
    confirm_verification,
    environment,
    error,
    get_me,
    invite,
    link_token,
    login,
    mint_reset,
    mint_verification,
    past_cooldown,
    post,
    request_reset,
    request_verification,
    verification_digest,
)

INVALID = (404, "email_verification_invalid")
WRONG_ACCOUNT = (403, "email_verification_wrong_account")
ALREADY = (409, "email_already_verified")


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "verify.db") as e:
        yield e


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=pytest.mark.skipif(
                not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL not set"
            ),
        ),
    ]
)
def backend_env(request, tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "verify.db" if request.param == "sqlite" else None) as e:
        yield e


@pytest.fixture
def withheld_env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "verify-withheld.db", teardown_commit=False) as e:
        yield e


def _verified(env: Env, user_id: str) -> bool:
    r = get_me(env, bearer(env, user_id))
    assert r.status_code == 200, r.text
    return r.json()["user"]["email_verified"]


def _mark_verified(env: Env, user_id: str) -> None:
    now = env.witness.db_now()
    env.witness.write(update(User).where(User.id == user_id).values(email_verified_at=now))


# --------------------------------------------------------------------------- #
class TestRequest:
    def test_an_unverified_account_gets_one_token_mailed_to_its_stored_address(
        self, backend_env: Env
    ):
        env = backend_env
        tenants = env.witness.snapshot(*TENANT_MODELS, User)
        r = request_verification(env, DANA)
        assert (r.status_code, r.content) == (204, b"")
        [message] = env.messages()
        assert (message.to, message.template) == (EMAIL[DANA], "email_verification")
        raw = link_token(message, VERIFY_PATH)
        link = f"{get_settings().public_web_origin}/verify-email#token={raw}"
        assert link in message.text_body and link in message.html_body
        [row] = env.witness.verification_tokens(DANA)
        assert set(row) == VERIFICATION_COLUMNS
        assert row["token_hash"] == verification_digest(raw)
        assert row["email_snapshot"] == EMAIL[DANA]
        assert (row["used_at"], row["revoked_at"]) == (None, None)
        ttl = timedelta(hours=get_settings().email_verification_token_ttl_hours)
        assert as_utc(row["expires_at"]) - as_utc(row["created_at"]) == ttl
        assert env.witness.snapshot(*TENANT_MODELS, User) == tenants
        assert env.witness.reset_tokens() == []

    def test_an_already_verified_address_is_409_and_mints_nothing(self, env: Env):
        _mark_verified(env, ALICE)
        before = env.witness.snapshot()
        assert code(request_verification(env, ALICE)) == ALREADY
        assert env.witness.snapshot() == before and env.messages() == []

    @pytest.mark.parametrize("url", [VERIFY_REQUEST, VERIFY_CONFIRM], ids=["request", "confirm"])
    def test_unauthenticated_is_401(self, env: Env, url: str):
        before = env.witness.snapshot()
        body = {} if url == VERIFY_REQUEST else {"token": "A" * 43}
        assert code(post(env, url, body)) == (401, "unauthorized")
        assert env.witness.snapshot() == before and env.messages() == []

    def test_an_inactive_account_is_401(self, env: Env):
        before = env.witness.snapshot()
        assert code(request_verification(env, INACTIVE)) == (401, "unauthorized")
        assert env.witness.snapshot() == before

    @pytest.mark.parametrize(
        "body",
        [{"email": EMAIL[BOB]}, {"user_id": BOB}, {"to": EMAIL[BOB]}],
        ids=["email", "user_id", "to"],
    )
    def test_the_caller_cannot_choose_the_recipient(self, env: Env, body: dict):
        before = env.witness.snapshot()
        r = post(env, VERIFY_REQUEST, body, user_id=ALICE)
        assert code(r) == (422, "validation_error")
        assert env.witness.snapshot() == before and env.messages() == []

    def test_resend_inside_the_cooldown_mints_and_revokes_nothing(self, env: Env):
        first = mint_verification(env, ALICE)
        rows = env.witness.verification_tokens(ALICE)
        r = request_verification(env, ALICE)
        assert (r.status_code, r.content) == (204, b"")
        assert env.witness.verification_tokens(ALICE) == rows
        assert len(env.messages()) == 1
        assert confirm_verification(env, first, ALICE).status_code == 204

    def test_a_resend_after_the_cooldown_supersedes_the_open_token(self, env: Env):
        first = mint_verification(env, ALICE)
        past_cooldown(env, EmailVerificationToken, ALICE)
        second = mint_verification(env, ALICE)
        old, new = env.witness.verification_tokens(ALICE)
        assert old["revoked_at"] is not None and old["used_at"] is None
        assert (new["revoked_at"], new["used_at"]) == (None, None)
        assert code(confirm_verification(env, first, ALICE)) == INVALID
        assert confirm_verification(env, second, ALICE).status_code == 204

    def test_the_daily_cap_bounds_resends(self, env: Env):
        cap = get_settings().auth_mail_daily_cap
        for _ in range(cap):
            mint_verification(env, ALICE)
            past_cooldown(env, EmailVerificationToken, ALICE)
        rows = env.witness.verification_tokens(ALICE)
        r = request_verification(env, ALICE)
        assert (r.status_code, r.content) == (204, b"")
        assert env.witness.verification_tokens(ALICE) == rows
        assert len(env.messages()) == cap
        env.witness.backdate(EmailVerificationToken, ALICE, timedelta(hours=24))
        mint_verification(env, ALICE)

    def test_reset_and_verification_budgets_are_independent(self, env: Env):
        """Cooldown and daily cap are counted per purpose: neither flow spends the other's."""
        cap = get_settings().auth_mail_daily_cap
        # Inside each other's cooldown: a reset mint does not hold back a verification mint,
        # nor the reverse.
        mint_reset(env, ALICE)
        mint_verification(env, ALICE)
        mint_verification(env, CAROL)
        mint_reset(env, CAROL)
        # At one purpose's daily cap, the other purpose still mints.
        for i in range(cap):
            env.witness.plant(
                PasswordResetToken,
                user_id=BOB,
                digest=f"{i:064x}",
                age=timedelta(hours=1, minutes=i),
                revoked=True,
            )
            env.witness.plant(
                EmailVerificationToken,
                user_id=DANA,
                digest=f"{i + cap:064x}",
                age=timedelta(hours=1, minutes=i),
                revoked=True,
            )
        mint_verification(env, BOB)
        mint_reset(env, DANA)
        # ... while the capped purpose itself stays capped.
        resets, verifications = env.witness.reset_tokens(BOB), env.witness.verification_tokens(DANA)
        assert request_reset(env, EMAIL[BOB]).status_code == 204
        assert request_verification(env, DANA).status_code == 204
        assert env.witness.reset_tokens(BOB) == resets
        assert env.witness.verification_tokens(DANA) == verifications


# --------------------------------------------------------------------------- #
class TestConfirm:
    def test_confirm_verifies_the_address_and_spends_the_token(self, backend_env: Env):
        env = backend_env
        assert _verified(env, ALICE) is False
        raw = mint_verification(env, ALICE)
        before = env.witness.user(ALICE)
        r = confirm_verification(env, raw, ALICE)
        assert (r.status_code, r.content) == (204, b"")
        after = env.witness.user(ALICE)
        assert before["email_verified_at"] is None and after["email_verified_at"] is not None
        # Verification is not a credential change.
        assert after["auth_epoch"] == before["auth_epoch"]
        assert after["hashed_password"] == before["hashed_password"]
        [row] = env.witness.verification_tokens(ALICE)
        assert row["used_at"] is not None and row["revoked_at"] is None
        assert _verified(env, ALICE) is True
        signed_in = login(env, EMAIL[ALICE], OLD_PASSWORD)
        assert signed_in.json()["user"]["email_verified"] is True

    def test_another_account_is_403_and_nothing_is_spent(self, env: Env):
        raw = mint_verification(env, ALICE)
        before = env.witness.snapshot()
        r = confirm_verification(env, raw, BOB)
        assert code(r) == WRONG_ACCOUNT
        assert env.witness.snapshot() == before
        assert _verified(env, BOB) is False and _verified(env, ALICE) is False
        # The right account can still spend it.
        assert confirm_verification(env, raw, ALICE).status_code == 204

    def test_a_changed_address_is_404_and_spends_nothing(self, env: Env):
        raw = mint_verification(env, ALICE)
        env.witness.write(
            update(User).where(User.id == ALICE).values(email="alice.moved@example.com")
        )
        before = env.witness.snapshot()
        assert code(confirm_verification(env, raw, ALICE)) == INVALID
        assert env.witness.snapshot() == before
        # Nothing was spent: with the address restored the snapshot matches again.
        env.witness.write(update(User).where(User.id == ALICE).values(email=EMAIL[ALICE]))
        assert confirm_verification(env, raw, ALICE).status_code == 204

    def _dead(self, env: Env, state: str) -> str:
        if state == "unknown":
            return "A" * 43
        if state == "unknown-at-max-length":
            return "A" * MAX_TOKEN_LENGTH
        raw = mint_verification(env, ALICE)
        [row] = env.witness.verification_tokens(ALICE)
        token = EmailVerificationToken.id == row["id"]
        now = env.witness.db_now()
        if state == "expired":
            past = now - timedelta(minutes=1)
            env.witness.write(update(EmailVerificationToken).where(token).values(expires_at=past))
        elif state == "used":
            assert confirm_verification(env, raw, ALICE).status_code == 204
        elif state == "revoked":
            env.witness.write(update(EmailVerificationToken).where(token).values(revoked_at=now))
        elif state == "superseded":
            past_cooldown(env, EmailVerificationToken, ALICE)
            mint_verification(env, ALICE)
        return raw

    STATES = ["expired", "used", "revoked", "superseded", "unknown", "unknown-at-max-length"]

    @pytest.mark.parametrize("state", STATES)
    def test_a_dead_token_is_404_and_changes_nothing(self, env: Env, state: str):
        raw = self._dead(env, state)
        before = env.witness.snapshot()
        assert code(confirm_verification(env, raw, ALICE)) == INVALID
        assert env.witness.snapshot() == before

    def test_every_dead_state_answers_identically(self, tmp_path):
        answers = set()
        for state in self.STATES:
            with environment(tmp_path / f"{state}.db") as e:
                answers.add(error(confirm_verification(e, self._dead(e, state), ALICE)))
        assert len(answers) == 1, answers
        [(status, error_code, _message)] = answers
        assert (status, error_code) == INVALID

    @pytest.mark.parametrize("token", ["", "A" * (MAX_TOKEN_LENGTH + 1)], ids=["empty", "overlong"])
    def test_a_malformed_token_is_422(self, env: Env, token: str):
        before = env.witness.snapshot()
        assert code(confirm_verification(env, token, ALICE)) == (422, "validation_error")
        assert env.witness.snapshot() == before

    @pytest.mark.parametrize("extra", ["email", "user_id"])
    def test_extra_fields_are_rejected(self, env: Env, extra: str):
        raw = mint_verification(env, ALICE)
        before = env.witness.snapshot()
        r = post(env, VERIFY_CONFIRM, {"token": raw, extra: EMAIL[BOB]}, user_id=ALICE)
        assert code(r) == (422, "validation_error")
        assert env.witness.snapshot() == before

    def test_every_other_open_verification_token_is_revoked(self, env: Env):
        """Defence in depth under the one-open-token index (removed here)."""
        env.witness.drop_index(VERIFICATION_OPEN_INDEX)
        digest = verification_digest
        first = env.witness.plant(EmailVerificationToken, user_id=ALICE, digest=digest("one"))
        second = env.witness.plant(EmailVerificationToken, user_id=ALICE, digest=digest("two"))
        assert confirm_verification(env, "one", ALICE).status_code == 204
        assert env.witness.token(EmailVerificationToken, first)["used_at"] is not None
        other = env.witness.token(EmailVerificationToken, second)
        assert other["revoked_at"] is not None and other["used_at"] is None


# --------------------------------------------------------------------------- #
class TestClaimIsConditional:
    """As for reset: a competitor committed immediately before the claim must win."""

    @pytest.mark.parametrize("kind", ["revoke", "use", "expire", "none"])
    def test_competitor_immediately_before_the_claim(self, backend_env: Env, kind: str):
        env = backend_env
        raw = mint_verification(env, ALICE)
        [row] = env.witness.verification_tokens(ALICE)
        fired: list[str] = []
        after: dict = {}

        def compete() -> None:
            now = env.witness.db_now()
            values = {
                "revoke": {"revoked_at": now},
                "use": {"used_at": now},
                "expire": {"expires_at": now - timedelta(hours=1)},
            }.get(kind)
            if values is not None:
                env.witness.write(
                    update(EmailVerificationToken)
                    .where(EmailVerificationToken.id == row["id"])
                    .values(**values)
                )

        def hook(conn, cursor, statement, parameters, context, executemany):
            target = "UPDATE EMAIL_VERIFICATION_TOKENS"
            if fired or not statement.lstrip().upper().startswith(target):
                return
            fired.append(statement)
            compete()
            after.update(env.witness.token(EmailVerificationToken, row["id"]))

        event.listen(env.request_engine, "before_cursor_execute", hook)
        try:
            r = confirm_verification(env, raw, ALICE)
        finally:
            event.remove(env.request_engine, "before_cursor_execute", hook)
        assert len(fired) == 1, "the request never reached a claim"
        if kind == "none":
            assert r.status_code == 204, r.text
            return
        assert code(r) == INVALID
        assert env.witness.token(EmailVerificationToken, row["id"]) == after
        assert env.witness.user(ALICE)["email_verified_at"] is None


# --------------------------------------------------------------------------- #
class TestWhatVerifiesAnAddress:
    def test_existing_accounts_start_unverified(self, env: Env):
        """FD-6: no backfill."""
        for user_id in (ALICE, BOB, CAROL, DANA):
            assert env.witness.user(user_id)["email_verified_at"] is None
            r = login(env, EMAIL[user_id], OLD_PASSWORD)
            assert r.json()["user"]["email_verified"] is False

    def test_a_new_registration_starts_unverified(self, env: Env):
        r = post(
            env,
            REGISTER,
            {
                "email": "founder@example.com",
                "full_name": "Founder",
                "password": OLD_PASSWORD,
                "organization_name": "Founder Co",
            },
        )
        assert r.status_code == 201 and r.json()["user"]["email_verified"] is False

    def test_invited_registration_never_verifies(self, env: Env):
        """FD-5: possessing an invitation proves nothing about the mailbox."""
        inv = invite(env, "newcomer@example.com")
        r = post(
            env,
            INVITATION_REGISTER,
            {"token": inv["token"], "full_name": "Newcomer", "password": OLD_PASSWORD},
        )
        assert r.status_code == 201, r.text
        assert r.json()["user"]["email_verified"] is False
        assert env.witness.user(r.json()["user"]["id"])["email_verified_at"] is None

    def test_invitation_acceptance_never_verifies(self, env: Env):
        inv = invite(env, EMAIL[BOB])
        r = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, user_id=BOB)
        assert r.status_code == 200, r.text
        assert r.json()["user"]["email_verified"] is False
        assert env.witness.user(BOB)["email_verified_at"] is None

    def test_a_completed_reset_verifies(self, env: Env):
        """FD-7."""
        assert confirm_reset(env, mint_reset(env, ALICE)).status_code == 204
        assert _verified(env, ALICE) is True
        # And a verified address can no longer ask for verification.
        assert code(request_verification(env, ALICE)) == ALREADY


# --------------------------------------------------------------------------- #
class TestTenantIsolation:
    def test_request_and_confirm_touch_only_the_account_and_its_tokens(self, backend_env: Env):
        env = backend_env
        tenants = env.witness.snapshot(*TENANT_MODELS)
        others = env.witness.bystanders(ALICE)
        resets = env.witness.reset_tokens()
        raw = mint_verification(env, ALICE)
        assert confirm_verification(env, raw, ALICE).status_code == 204
        assert env.witness.snapshot(*TENANT_MODELS) == tenants
        assert env.witness.bystanders(ALICE) == others
        assert env.witness.reset_tokens() == resets
        assert {r["user_id"] for r in env.witness.verification_tokens()} == {ALICE}


class TestExplicitCommit:
    def test_request_is_committed_before_the_response(self, withheld_env: Env):
        raw = mint_verification(withheld_env, ALICE)
        [row] = withheld_env.witness.verification_tokens(ALICE)
        assert row["token_hash"] == verification_digest(raw)

    def test_confirm_is_committed_before_the_response(self, withheld_env: Env):
        raw = mint_verification(withheld_env, ALICE)
        assert confirm_verification(withheld_env, raw, ALICE).status_code == 204
        assert withheld_env.witness.user(ALICE)["email_verified_at"] is not None
