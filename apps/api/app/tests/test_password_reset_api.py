"""6B-4A: password reset (P6-AUTH-2).

    POST /api/v1/auth/password-reset/request   public, {email}                -> 204, always
    POST /api/v1/auth/password-reset/confirm   public, {token, new_password}  -> 204

Only ``get_db`` is overridden; mail goes to the in-memory backend selected through settings;
committed state is read through a separate ``Engine`` (see ``_auth2_support``).

Load-bearing properties, each with its own test:

* **The request says nothing about the account.** Unknown, inactive, cooling-down, capped,
  issued and provider-failure requests all answer 204 with the same empty body and the same
  headers. No per-account 429 exists.
* **At most one open token per account, and only the digest is stored.** A new mint after
  the cooldown revokes the previous open token (supersession); inside the cooldown nothing is
  minted and nothing is revoked; the per-account daily cap counts every token created in the
  last 24 hours of the database clock.
* **Mail goes to the stored address, with a link built from ``public_web_origin``.** Forged
  ``Host``/``X-Forwarded-Host``/``Origin``/``Referer`` change nothing; the token rides in the
  URL fragment, never a query string. Address matching is exact (finding F2, pinned).
* **Confirm spends the token once and only for its own account.** Password changed, epoch
  +1, address marked verified (FD-7), every other open reset token revoked, no session
  issued (FD-9). Every dead state is the same 404 ``password_reset_invalid``; a bad new
  password is a 422 that spends nothing; a bearer header is ignored.
* **The claim is conditional** (a competitor committed just before it wins), **nothing
  outside the account moves** (tenant tables and bystander users byte-identical), and **both
  routes commit before answering** (a teardown that never commits).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import event, update

from app.auth.models import PasswordResetToken
from app.core.config import get_settings
from app.core.security import verify_password
from app.organizations.invitations import MAX_TOKEN_LENGTH
from app.organizations.models import User
from app.tests._auth2_support import (
    ALICE,
    BOB,
    CAROL,
    CASEY,
    DANA,
    EMAIL,
    INACTIVE,
    NEW_PASSWORD,
    OLD_PASSWORD,
    REGISTER,
    RESET_COLUMNS,
    RESET_CONFIRM,
    RESET_OPEN_INDEX,
    RESET_PATH,
    RESET_REQUEST,
    TENANT_MODELS,
    UNKNOWN_EMAIL,
    Env,
    as_utc,
    bearer,
    code,
    confirm_reset,
    environment,
    error,
    failing_mail,
    get_me,
    link_token,
    login,
    mint_reset,
    past_cooldown,
    post,
    request_reset,
    reset_digest,
)

INVALID = (404, "password_reset_invalid")


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "reset.db") as e:
        yield e


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=pytest.mark.skipif(
                not os.getenv("TEST_POSTGRES_URL"),
                reason="TEST_POSTGRES_URL not set",
            ),
        ),
    ]
)
def backend_env(request, tmp_path) -> Iterator[Env]:
    """The same environment on SQLite and, when ``TEST_POSTGRES_URL`` is set, PostgreSQL."""
    with environment(tmp_path / "reset.db" if request.param == "sqlite" else None) as e:
        yield e


@pytest.fixture
def withheld_env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "reset-withheld.db", teardown_commit=False) as e:
        yield e


def _plant_cap(env: Env, user_id: str) -> None:
    """``auth_mail_daily_cap`` revoked tokens, all inside the last 24 h but past the cooldown."""
    for i in range(get_settings().auth_mail_daily_cap):
        env.witness.plant(
            PasswordResetToken,
            user_id=user_id,
            digest=f"{i:064x}",
            age=timedelta(hours=1, minutes=i),
            revoked=True,
        )


def _normalized(r) -> tuple:
    headers = {k.lower(): v for k, v in r.headers.items() if k.lower() != "x-request-id"}
    return r.status_code, r.content, headers


# --------------------------------------------------------------------------- #
class TestRequestRevealsNothing:
    def test_every_account_class_gets_the_same_response(self, env: Env, monkeypatch):
        mint_reset(env, ALICE)  # ALICE is now inside the cooldown
        _plant_cap(env, CAROL)  # CAROL has reached the daily cap
        responses = {
            "unknown": request_reset(env, UNKNOWN_EMAIL),
            "inactive": request_reset(env, EMAIL[INACTIVE]),
            "cooldown": request_reset(env, EMAIL[ALICE]),
            "cap": request_reset(env, EMAIL[CAROL]),
            "issued": request_reset(env, EMAIL[CASEY]),
        }
        with failing_mail(monkeypatch, env) as handed:
            responses["provider-failure"] = request_reset(env, EMAIL[BOB])
        assert [m.to for m in handed] == [EMAIL[BOB]], "the failing send really ran"
        baseline = _normalized(responses["unknown"])
        assert baseline[:2] == (204, b"")
        for kind, r in responses.items():
            assert _normalized(r) == baseline, kind
        # Only the two issuable requests reached the outbox.
        assert [m.to for m in env.messages()] == [EMAIL[ALICE], EMAIL[CASEY]]

    def test_no_per_account_throttle_response(self, env: Env):
        statuses = {request_reset(env, EMAIL[ALICE]).status_code for _ in range(12)}
        statuses |= {request_reset(env, UNKNOWN_EMAIL).status_code for _ in range(12)}
        assert statuses == {204}
        assert len(env.messages()) == 1

    def test_request_needs_no_authentication_and_ignores_a_bearer(self, env: Env):
        """A signed-in BOB asking for ALICE's address mails ALICE, never BOB."""
        r = request_reset(env, EMAIL[ALICE], user_id=BOB)
        assert (r.status_code, r.content) == (204, b"")
        assert [m.to for m in env.messages()] == [EMAIL[ALICE]]
        assert env.witness.reset_tokens(BOB) == []

    @pytest.mark.parametrize(
        "body",
        [{}, {"email": "not-an-address"}, {"email": EMAIL[ALICE], "user_id": BOB}],
        ids=["missing", "malformed", "extra-field"],
    )
    def test_malformed_bodies_are_422_and_mint_nothing(self, env: Env, body: dict):
        before = env.witness.snapshot()
        r = post(env, RESET_REQUEST, body)
        assert code(r) == (422, "validation_error")
        assert env.witness.snapshot() == before and env.messages() == []


# --------------------------------------------------------------------------- #
class TestRequestIssuing:
    def test_an_active_account_gets_one_digest_only_token_and_one_message(self, env: Env):
        tenants = env.witness.snapshot(*TENANT_MODELS, User)
        r = request_reset(env, EMAIL[ALICE])
        assert (r.status_code, r.content) == (204, b"")
        [message] = env.messages()
        assert (message.to, message.template) == (EMAIL[ALICE], "password_reset")
        raw = link_token(message, RESET_PATH)
        assert len(raw) >= 43  # secrets.token_urlsafe(32): 256 bits
        [row] = env.witness.reset_tokens(ALICE)
        assert set(row) == RESET_COLUMNS
        assert row["token_hash"] == reset_digest(raw)
        assert raw not in json.dumps(row, default=str)
        assert (row["used_at"], row["revoked_at"]) == (None, None)
        ttl = timedelta(minutes=get_settings().password_reset_token_ttl_minutes)
        assert as_utc(row["expires_at"]) - as_utc(row["created_at"]) == ttl
        # Nothing else moved: no user, tenant or verification row, no invitation reused.
        assert env.witness.snapshot(*TENANT_MODELS, User) == tenants
        assert env.witness.verification_tokens() == []

    def test_the_lifetime_comes_from_settings(self, env: Env, monkeypatch):
        monkeypatch.setattr(get_settings(), "password_reset_token_ttl_minutes", 15)
        mint_reset(env, ALICE)
        [row] = env.witness.reset_tokens(ALICE)
        assert as_utc(row["expires_at"]) - as_utc(row["created_at"]) == timedelta(minutes=15)

    def test_inside_the_cooldown_nothing_is_minted_or_revoked(self, env: Env):
        first = mint_reset(env, ALICE)
        # Aged to just inside the window: still refused.
        cooldown = get_settings().auth_mail_cooldown_seconds
        env.witness.backdate(PasswordResetToken, ALICE, timedelta(seconds=cooldown - 5))
        rows = env.witness.reset_tokens(ALICE)
        r = request_reset(env, EMAIL[ALICE])
        assert (r.status_code, r.content) == (204, b"")
        assert env.witness.reset_tokens(ALICE) == rows
        assert len(env.messages()) == 1
        # The token issued before the cooldown is still the live one.
        assert confirm_reset(env, first).status_code == 204

    def test_a_new_request_after_the_cooldown_supersedes_the_open_token(self, env: Env):
        first = mint_reset(env, ALICE)
        past_cooldown(env, PasswordResetToken, ALICE)
        second = mint_reset(env, ALICE)
        old, new = env.witness.reset_tokens(ALICE)
        assert old["token_hash"] == reset_digest(first) and new["token_hash"] == reset_digest(
            second
        )
        assert old["revoked_at"] is not None and old["used_at"] is None
        assert (new["revoked_at"], new["used_at"]) == (None, None)
        assert code(confirm_reset(env, first)) == INVALID
        assert confirm_reset(env, second).status_code == 204

    def test_the_daily_cap_counts_every_token_of_the_last_24_hours(self, env: Env):
        cap = get_settings().auth_mail_daily_cap
        for _ in range(cap):
            mint_reset(env, ALICE)
            past_cooldown(env, PasswordResetToken, ALICE)
        rows = env.witness.reset_tokens(ALICE)
        assert len(rows) == cap and len(env.witness.open_tokens(PasswordResetToken, ALICE)) == 1
        r = request_reset(env, EMAIL[ALICE])
        assert (r.status_code, r.content) == (204, b"")
        assert env.witness.reset_tokens(ALICE) == rows, "the cap minted or revoked something"
        assert len(env.messages()) == cap
        # A day later the same tokens no longer count.
        env.witness.backdate(PasswordResetToken, ALICE, timedelta(hours=24))
        mint_reset(env, ALICE)
        assert len(env.witness.reset_tokens(ALICE)) == cap + 1

    def test_the_cap_is_per_account(self, env: Env):
        _plant_cap(env, CAROL)
        before = env.witness.reset_tokens(CAROL)
        request_reset(env, EMAIL[CAROL])
        assert env.witness.reset_tokens(CAROL) == before
        mint_reset(env, ALICE)

    @pytest.mark.parametrize("email", [UNKNOWN_EMAIL, EMAIL[INACTIVE]], ids=["unknown", "inactive"])
    def test_unknown_and_inactive_addresses_mint_and_send_nothing(self, env: Env, email: str):
        before = env.witness.snapshot()
        assert request_reset(env, email).status_code == 204
        assert env.witness.snapshot() == before
        assert env.messages() == []

    def test_mail_goes_to_the_stored_address(self, env: Env):
        """EmailStr lowercases the domain; the stored address is what receives the mail."""
        assert request_reset(env, "alice@EXAMPLE.com").status_code == 204
        assert request_reset(env, EMAIL[DANA]).status_code == 204
        assert [m.to for m in env.messages()] == [EMAIL[ALICE], EMAIL[DANA]]
        assert len(env.witness.reset_tokens(ALICE)) == len(env.witness.reset_tokens(DANA)) == 1


class TestDisclosedCaseVariantAccounts:
    """DISCLOSED PRODUCT RISK (finding F2) -- pinned so it cannot change silently, NOT endorsed.

    Addresses are matched exactly (no case folding), as everywhere else in the product. A
    request for an address differing from a stored one only in local-part case matches no
    account; where two case-variant accounts exist, each is its own account and each reset
    reaches only itself.
    """

    @pytest.mark.parametrize(
        "asked", ["dana@example.com", "Casey@example.com"], ids=["lower-dana", "upper-casey"]
    )
    def test_a_case_variant_of_a_stored_address_matches_nothing(self, env: Env, asked: str):
        before = env.witness.snapshot()
        assert request_reset(env, asked).status_code == 204
        assert env.witness.snapshot() == before and env.messages() == []

    def test_case_variant_accounts_are_reset_independently(self, env: Env):
        r = post(
            env,
            REGISTER,
            {
                "email": "Casey@example.com",
                "full_name": "Casey Two",
                "password": OLD_PASSWORD,
                "organization_name": "Casey Two Co",
            },
        )
        assert r.status_code == 201, r.text
        twin = r.json()["user"]["id"]
        assert request_reset(env, "Casey@example.com").status_code == 204
        [message] = env.messages()
        assert message.to == "Casey@example.com"
        assert env.witness.reset_tokens(CASEY) == []
        assert confirm_reset(env, link_token(message, RESET_PATH)).status_code == 204
        assert env.witness.user(twin)["auth_epoch"] == 1
        assert env.witness.user(CASEY)["auth_epoch"] == 0
        assert login(env, EMAIL[CASEY], OLD_PASSWORD).status_code == 200


# --------------------------------------------------------------------------- #
class TestLinks:
    FORGED = {
        "Host": "evil.example",
        "X-Forwarded-Host": "evil.example",
        "X-Forwarded-Proto": "http",
        "Forwarded": "host=evil.example;proto=http",
        "Origin": "https://evil.example",
        "Referer": "https://evil.example/steal",
    }

    def test_link_is_the_configured_origin_with_the_token_in_the_fragment(
        self, env: Env, monkeypatch
    ):
        origin = "https://app.signalnest.test"
        monkeypatch.setattr(get_settings(), "public_web_origin", origin)
        r = request_reset(env, EMAIL[ALICE], headers=self.FORGED)
        assert r.status_code == 204
        [message] = env.messages()
        raw = link_token(message, RESET_PATH)
        link = f"{origin}/reset-password#token={raw}"
        assert link in message.text_body and link in message.html_body
        for body in (message.subject, message.text_body, message.html_body):
            assert "evil" not in body
            assert "?token" not in body and "&token" not in body

    def test_default_origin_is_used_when_not_overridden(self, env: Env):
        mint_reset(env, ALICE)
        [message] = env.messages()
        assert f"{get_settings().public_web_origin}/reset-password#token=" in message.text_body


# --------------------------------------------------------------------------- #
class TestConfirm:
    def test_confirm_sets_the_password_and_spends_the_token(self, backend_env: Env):
        env = backend_env
        session_before = login(env, EMAIL[ALICE], OLD_PASSWORD).json()["access_token"]
        raw = mint_reset(env, ALICE)
        before = env.witness.user(ALICE)
        r = confirm_reset(env, raw)
        # FD-9: no session is created or swapped -- the user goes back to sign in.
        assert (r.status_code, r.content) == (204, b"")
        assert "set-cookie" not in r.headers and "access_token" not in r.text
        after = env.witness.user(ALICE)
        assert after["auth_epoch"] == before["auth_epoch"] + 1
        assert verify_password(NEW_PASSWORD, after["hashed_password"])
        assert not verify_password(OLD_PASSWORD, after["hashed_password"])
        assert before["email_verified_at"] is None and after["email_verified_at"] is not None
        [row] = env.witness.reset_tokens(ALICE)
        assert row["used_at"] is not None and row["revoked_at"] is None
        assert login(env, EMAIL[ALICE], OLD_PASSWORD).status_code == 401
        fresh = login(env, EMAIL[ALICE], NEW_PASSWORD)
        assert fresh.status_code == 200 and fresh.json()["user"]["email_verified"] is True
        assert get_me(env, session_before).status_code == 401

    def test_only_the_credential_columns_of_the_target_change(self, env: Env):
        raw = mint_reset(env, ALICE)
        before, others = env.witness.user(ALICE), env.witness.bystanders(ALICE)
        assert confirm_reset(env, raw).status_code == 204
        after = env.witness.user(ALICE)
        changed = {k for k in before if before[k] != after[k]}
        assert {"hashed_password", "auth_epoch", "email_verified_at"} <= changed
        assert changed <= {"hashed_password", "auth_epoch", "email_verified_at", "updated_at"}
        assert env.witness.bystanders(ALICE) == others

    def test_an_earlier_verification_is_kept(self, env: Env):
        """FD-7 marks the address verified with COALESCE: an existing timestamp survives."""
        earlier = env.witness.db_now() - timedelta(days=3)
        env.witness.write(update(User).where(User.id == ALICE).values(email_verified_at=earlier))
        assert confirm_reset(env, mint_reset(env, ALICE)).status_code == 204
        assert as_utc(env.witness.user(ALICE)["email_verified_at"]) == earlier

    def _dead(self, env: Env, state: str) -> str:
        if state == "unknown":
            return "A" * 43
        if state == "unknown-at-max-length":
            return "A" * MAX_TOKEN_LENGTH
        raw = mint_reset(env, ALICE)
        [row] = env.witness.reset_tokens(ALICE)
        token = PasswordResetToken.id == row["id"]
        now = env.witness.db_now()
        if state == "expired":
            past = now - timedelta(minutes=1)
            env.witness.write(update(PasswordResetToken).where(token).values(expires_at=past))
        elif state == "used":
            assert confirm_reset(env, raw, "first-new-password").status_code == 204
        elif state == "revoked":
            env.witness.write(update(PasswordResetToken).where(token).values(revoked_at=now))
        elif state == "superseded":
            past_cooldown(env, PasswordResetToken, ALICE)
            mint_reset(env, ALICE)
        elif state == "inactive-user":
            env.witness.write(update(User).where(User.id == ALICE).values(is_active=False))
        return raw

    STATES = [
        "expired",
        "used",
        "revoked",
        "superseded",
        "inactive-user",
        "unknown",
        "unknown-at-max-length",
    ]

    @pytest.mark.parametrize("state", STATES)
    def test_a_dead_token_is_404_and_changes_nothing(self, env: Env, state: str):
        raw = self._dead(env, state)
        before = env.witness.snapshot()
        r = confirm_reset(env, raw, "never-applied-9")
        assert code(r) == INVALID
        assert env.witness.snapshot() == before
        assert not verify_password("never-applied-9", env.witness.user(ALICE)["hashed_password"])

    def test_every_dead_state_answers_identically(self, tmp_path):
        answers = set()
        for state in self.STATES:
            with environment(tmp_path / f"{state}.db") as e:
                answers.add(error(confirm_reset(e, self._dead(e, state), "never-applied-9")))
        assert len(answers) == 1, answers
        [(status, error_code, _message)] = answers
        assert (status, error_code) == INVALID  # never 401: a token error is not a session error

    @pytest.mark.parametrize("token", ["", "A" * (MAX_TOKEN_LENGTH + 1)], ids=["empty", "overlong"])
    def test_a_malformed_token_is_422(self, env: Env, token: str):
        before = env.witness.snapshot()
        assert code(confirm_reset(env, token)) == (422, "validation_error")
        assert env.witness.snapshot() == before

    @pytest.mark.parametrize(
        "password", ["short7!", "p" * 129, None], ids=["too-short", "too-long", "missing"]
    )
    def test_a_bad_new_password_is_422_and_spends_nothing(self, env: Env, password):
        raw = mint_reset(env, ALICE)
        before = env.witness.snapshot()
        body = {"token": raw} if password is None else {"token": raw, "new_password": password}
        assert code(post(env, RESET_CONFIRM, body)) == (422, "validation_error")
        assert env.witness.snapshot() == before
        # The token is intact: a good password still spends it.
        assert confirm_reset(env, raw).status_code == 204

    @pytest.mark.parametrize("length", [8, 128])
    def test_password_bounds_are_inclusive(self, env: Env, length: int):
        password = "q" * length
        assert confirm_reset(env, mint_reset(env, ALICE), password).status_code == 204
        assert login(env, EMAIL[ALICE], password).status_code == 200

    def test_replay_is_404_and_the_epoch_moves_once(self, env: Env):
        raw = mint_reset(env, ALICE)
        assert confirm_reset(env, raw).status_code == 204
        before = env.witness.snapshot()
        assert code(confirm_reset(env, raw, "another-password-1")) == INVALID
        assert env.witness.snapshot() == before
        assert env.witness.user(ALICE)["auth_epoch"] == 1
        assert login(env, EMAIL[ALICE], NEW_PASSWORD).status_code == 200

    def test_every_other_open_reset_token_is_revoked(self, env: Env):
        """Defence in depth under the one-open-token index: with the index removed, two open
        tokens can coexist, and spending one must revoke the other."""
        env.witness.drop_index(RESET_OPEN_INDEX)
        first = env.witness.plant(PasswordResetToken, user_id=ALICE, digest=reset_digest("one"))
        second = env.witness.plant(PasswordResetToken, user_id=ALICE, digest=reset_digest("two"))
        assert confirm_reset(env, "one").status_code == 204
        spent = env.witness.token(PasswordResetToken, first)
        other = env.witness.token(PasswordResetToken, second)
        assert spent["used_at"] is not None
        assert other["revoked_at"] is not None and other["used_at"] is None
        assert code(confirm_reset(env, "two", "second-password-2")) == INVALID
        assert env.witness.user(ALICE)["auth_epoch"] == 1

    def test_a_bearer_is_ignored(self, env: Env):
        """The token alone names the account: BOB's bearer neither redirects nor blocks it."""
        raw = mint_reset(env, ALICE)
        bob_before = env.witness.user(BOB)
        assert confirm_reset(env, raw, user_id=BOB).status_code == 204
        assert verify_password(NEW_PASSWORD, env.witness.user(ALICE)["hashed_password"])
        assert env.witness.user(BOB) == bob_before
        # A garbage bearer is ignored too: no authentication dependency on this route.
        other = mint_reset(env, CAROL)
        garbage = {"Authorization": "Bearer not-a-jwt"}
        assert confirm_reset(env, other, headers=garbage).status_code == 204

    @pytest.mark.parametrize("extra", ["email", "user_id", "auth_epoch"])
    def test_extra_fields_are_rejected(self, env: Env, extra: str):
        raw = mint_reset(env, ALICE)
        before = env.witness.snapshot()
        body = {"token": raw, "new_password": NEW_PASSWORD, extra: EMAIL[BOB]}
        assert code(post(env, RESET_CONFIRM, body)) == (422, "validation_error")
        assert env.witness.snapshot() == before


# --------------------------------------------------------------------------- #
def _competitor(kind: str):
    def act(env: Env, token_id: str) -> None:
        now = env.witness.db_now()
        values = {
            "revoke": {"revoked_at": now},
            "use": {"used_at": now},
            # Well before the request's own reading of the clock (PostgreSQL's now() is the
            # transaction start), so only the claim's predicate can be what refuses it.
            "expire": {"expires_at": now - timedelta(hours=1)},
            "none": None,
        }[kind]
        if values is not None:
            assert (
                env.witness.write(
                    update(PasswordResetToken)
                    .where(PasswordResetToken.id == token_id)
                    .values(**values)
                )
                == 1
            )

    return act


class TestClaimIsConditional:
    """The claim's WHERE predicates and rowcount check are what stop a concurrent change.

    A sequential replay is refused before the claim is reached, so it cannot tell a
    conditional claim from an unconditional one. Here the request resolves an OPEN token,
    and immediately before its first ``UPDATE password_reset_tokens`` -- the claim -- a
    competing change is committed on a separate connection. Only ``UPDATE ... WHERE used_at
    IS NULL AND revoked_at IS NULL AND expires_at > now`` with a rowcount check can refuse it
    now. SQLite (the request has written nothing yet) and PostgreSQL (READ COMMITTED; the
    competitor touches only the token row) both let the competitor commit in that window.
    """

    @pytest.mark.parametrize("kind", ["revoke", "use", "expire", "none"])
    def test_competitor_immediately_before_the_claim(self, backend_env: Env, kind: str):
        env = backend_env
        raw = mint_reset(env, ALICE)
        [row] = env.witness.reset_tokens(ALICE)
        user_before = env.witness.user(ALICE)
        fired: list[str] = []
        after: dict = {}

        def hook(conn, cursor, statement, parameters, context, executemany):
            if fired or not statement.lstrip().upper().startswith("UPDATE PASSWORD_RESET_TOKENS"):
                return
            fired.append(statement)
            _competitor(kind)(env, row["id"])
            after.update(env.witness.token(PasswordResetToken, row["id"]))

        event.listen(env.request_engine, "before_cursor_execute", hook)
        try:
            r = confirm_reset(env, raw)
        finally:
            event.remove(env.request_engine, "before_cursor_execute", hook)
        assert len(fired) == 1, "the request never reached a claim"
        if kind == "none":  # positive control: the hook itself breaks nothing
            assert r.status_code == 204, r.text
            return
        assert code(r) == INVALID
        # Nothing the request did survived: the token is as the competitor left it and the
        # account's credentials are untouched.
        assert env.witness.token(PasswordResetToken, row["id"]) == after
        user_after = env.witness.user(ALICE)
        for key in ("hashed_password", "auth_epoch", "email_verified_at"):
            assert user_after[key] == user_before[key], key


# --------------------------------------------------------------------------- #
class TestTenantIsolation:
    def test_request_and_confirm_touch_only_the_account_and_its_tokens(self, backend_env: Env):
        env = backend_env
        tenants = env.witness.snapshot(*TENANT_MODELS)
        others = env.witness.bystanders(ALICE)
        verification = env.witness.verification_tokens()
        raw = mint_reset(env, ALICE)
        assert confirm_reset(env, raw).status_code == 204
        assert env.witness.snapshot(*TENANT_MODELS) == tenants
        assert env.witness.bystanders(ALICE) == others
        assert env.witness.verification_tokens() == verification
        assert {r["user_id"] for r in env.witness.reset_tokens()} == {ALICE}

    def test_a_bystanders_session_survives_anothers_reset(self, env: Env):
        carol_session = login(env, EMAIL[CAROL], OLD_PASSWORD).json()["access_token"]
        assert confirm_reset(env, mint_reset(env, ALICE)).status_code == 204
        assert get_me(env, carol_session).status_code == 200
        assert get_me(env, bearer(env, BOB)).status_code == 200


# --------------------------------------------------------------------------- #
class TestExplicitCommit:
    def test_request_is_committed_before_the_response(self, withheld_env: Env):
        raw = mint_reset(withheld_env, ALICE)
        [row] = withheld_env.witness.reset_tokens(ALICE)
        assert row["token_hash"] == reset_digest(raw)

    def test_confirm_is_committed_before_the_response(self, withheld_env: Env):
        raw = mint_reset(withheld_env, ALICE)
        assert confirm_reset(withheld_env, raw).status_code == 204
        user = withheld_env.witness.user(ALICE)
        assert verify_password(NEW_PASSWORD, user["hashed_password"])
        assert user["auth_epoch"] == 1
