"""6B-4A: the credential epoch (FD-3, AUTH2-C4).

``users.auth_epoch`` is a narrow integer that a successful password reset increments in the
same transaction as the password write. Every access token carries the epoch it was issued
under (``auth_epoch`` claim); ``get_current_user`` rejects a token whose claim is not a
genuine ``int`` equal to the account's current epoch. A token issued before epochs existed
carries no claim and counts as epoch 0, so the first reset invalidates it too.

Load-bearing properties, each with its own test:

* A session issued before a reset authenticates until the reset and never after it; a
  session issued after the reset authenticates.
* A missing claim is epoch 0 -- accepted while the account is at 0, refused afterwards.
* Only an ``int`` is accepted: ``"0"``, ``0.0``, ``False``, ``None`` and containers are
  refused even where they compare equal to the epoch.
* **Same-second issuance.** With the issuing clock frozen, a token minted before the reset
  and one minted after it share ``iat`` and ``exp`` exactly and differ only in the epoch --
  so no ``iat``-versus-reset-time comparison could tell them apart, and the epoch does.
* Every ``SessionOut``-issuing path (register, login, me, invitation register, invitation
  accept) stamps the account's current epoch, and the token it returns works.
* A reset moves only its own account's epoch, by exactly one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from app.auth.models import PasswordResetToken
from app.core import security
from app.core.security import create_access_token
from app.tests._auth2_support import (
    ALICE,
    BOB,
    CAROL,
    EMAIL,
    INVITATION_ACCEPT,
    INVITATION_REGISTER,
    NEW_PASSWORD,
    OLD_PASSWORD,
    REGISTER,
    VERIFY_REQUEST,
    Env,
    bearer,
    claims,
    confirm_reset,
    environment,
    get_me,
    invite,
    login,
    mint_reset,
    past_cooldown,
    post,
)

UNAUTHORIZED = (401, "unauthorized", "Invalid or expired token.")


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "epoch.db") as e:
        yield e


def _reset(env: Env, user_id: str, password: str = NEW_PASSWORD) -> None:
    assert confirm_reset(env, mint_reset(env, user_id), password).status_code == 204


def _refused(r) -> tuple[int, str, str]:
    body = r.json()["error"]
    return r.status_code, body["code"], body["message"]


def _session_token(env: Env, user_id: str, password: str) -> str:
    r = login(env, EMAIL[user_id], password)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# --------------------------------------------------------------------------- #
class TestResetEndsEarlierSessions:
    def test_a_pre_reset_session_works_until_the_reset_and_never_after(self, env: Env):
        old = _session_token(env, ALICE, OLD_PASSWORD)
        assert claims(old)["auth_epoch"] == 0
        assert get_me(env, old).status_code == 200
        _reset(env, ALICE)
        assert env.witness.user(ALICE)["auth_epoch"] == 1
        assert _refused(get_me(env, old)) == UNAUTHORIZED
        new = _session_token(env, ALICE, NEW_PASSWORD)
        assert claims(new)["auth_epoch"] == 1
        assert get_me(env, new).status_code == 200

    def test_each_reset_moves_the_epoch_by_exactly_one(self, env: Env):
        tokens = []
        for epoch, password in enumerate(("first-password-1", "second-password-2"), start=1):
            tokens.append(bearer(env, ALICE))
            past_cooldown(env, PasswordResetToken, ALICE)
            _reset(env, ALICE, password)
            assert env.witness.user(ALICE)["auth_epoch"] == epoch
        assert [get_me(env, t).status_code for t in tokens] == [401, 401]
        assert get_me(env, bearer(env, ALICE)).status_code == 200

    def test_a_reset_leaves_every_other_accounts_epoch_and_sessions_alone(self, env: Env):
        bob = _session_token(env, BOB, OLD_PASSWORD)
        carol = bearer(env, CAROL)
        _reset(env, ALICE)
        assert (env.witness.user(BOB)["auth_epoch"], env.witness.user(CAROL)["auth_epoch"]) == (
            0,
            0,
        )
        assert get_me(env, bob).status_code == 200
        assert get_me(env, carol).status_code == 200

    def test_nothing_but_a_completed_reset_moves_the_epoch(self, env: Env):
        raw = mint_reset(env, ALICE)  # a request
        confirm_reset(env, "A" * 43)  # a failed confirm
        confirm_reset(env, raw, "short")  # a refused password
        post(env, VERIFY_REQUEST, {}, user_id=ALICE)  # a verification mint
        assert env.witness.user(ALICE)["auth_epoch"] == 0
        assert get_me(env, create_access_token(ALICE)).status_code == 200


# --------------------------------------------------------------------------- #
class TestClaimShape:
    def test_a_legacy_token_without_the_claim_is_epoch_zero(self, env: Env):
        legacy = create_access_token(ALICE)  # no auth_epoch claim at all
        assert "auth_epoch" not in claims(legacy)
        assert get_me(env, legacy).status_code == 200
        _reset(env, ALICE)
        assert _refused(get_me(env, legacy)) == UNAUTHORIZED

    @pytest.mark.parametrize(
        "claim",
        ["0", 0.0, False, None, [0], {"epoch": 0}, -1, 1],
        ids=["str", "float", "bool", "null", "list", "object", "negative", "future"],
    )
    def test_anything_but_the_current_int_is_refused(self, env: Env, claim):
        assert env.witness.user(ALICE)["auth_epoch"] == 0
        token = create_access_token(ALICE, extra={"auth_epoch": claim})
        assert _refused(get_me(env, token)) == UNAUTHORIZED

    def test_true_is_not_epoch_one(self, env: Env):
        _reset(env, ALICE)
        token = create_access_token(ALICE, extra={"auth_epoch": True})
        assert _refused(get_me(env, token)) == UNAUTHORIZED
        assert get_me(env, create_access_token(ALICE, extra={"auth_epoch": 1})).status_code == 200

    def test_the_current_int_is_accepted(self, env: Env):
        """Positive control for the refusals above."""
        token = create_access_token(ALICE, extra={"auth_epoch": 0})
        assert get_me(env, token).status_code == 200


# --------------------------------------------------------------------------- #
class TestSameSecond:
    def test_a_token_minted_in_the_resets_second_is_told_apart_by_the_epoch(
        self, env: Env, monkeypatch
    ):
        """Why an epoch and not ``iat``: both tokens carry the same second.

        The issuing clock is frozen for the whole exchange, so a token minted before the
        reset and one minted after it are identical in ``sub``, ``email``, ``iat`` and
        ``exp``. An ``iat``-versus-reset-time rule cannot separate them within one second;
        the epoch does.
        """
        frozen = datetime.now(UTC).replace(microsecond=0)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz is not None else frozen.replace(tzinfo=None)

        monkeypatch.setattr(security, "datetime", FrozenDatetime)
        before = _session_token(env, ALICE, OLD_PASSWORD)
        _reset(env, ALICE)
        after = _session_token(env, ALICE, NEW_PASSWORD)
        old, new = claims(before), claims(after)
        assert old["iat"] == new["iat"] and old["exp"] == new["exp"]
        assert {k: v for k, v in old.items() if k != "auth_epoch"} == {
            k: v for k, v in new.items() if k != "auth_epoch"
        }
        assert (old["auth_epoch"], new["auth_epoch"]) == (0, 1)
        assert _refused(get_me(env, before)) == UNAUTHORIZED
        assert get_me(env, after).status_code == 200


# --------------------------------------------------------------------------- #
class TestEveryIssuancePathStampsTheEpoch:
    """AUTH2-C4: every SessionOut-issuing route stamps the account's current epoch."""

    def _assert_current(self, env: Env, access_token: str, user_id: str) -> None:
        epoch = claims(access_token)["auth_epoch"]
        assert type(epoch) is int
        assert epoch == env.witness.user(user_id)["auth_epoch"]
        assert get_me(env, access_token).status_code == 200

    def test_register(self, env: Env):
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
        assert r.status_code == 201, r.text
        assert claims(r.json()["access_token"])["auth_epoch"] == 0
        self._assert_current(env, r.json()["access_token"], r.json()["user"]["id"])

    def test_login_and_me_after_a_reset(self, env: Env):
        _reset(env, ALICE)
        signed_in = _session_token(env, ALICE, NEW_PASSWORD)
        self._assert_current(env, signed_in, ALICE)
        me = get_me(env, signed_in)
        refreshed = me.json()["access_token"]
        assert claims(refreshed)["auth_epoch"] == 1
        self._assert_current(env, refreshed, ALICE)

    def test_invitation_register(self, env: Env):
        inv = invite(env, "newcomer@example.com")
        r = post(
            env,
            INVITATION_REGISTER,
            {"token": inv["token"], "full_name": "Newcomer", "password": OLD_PASSWORD},
        )
        assert r.status_code == 201, r.text
        assert claims(r.json()["access_token"])["auth_epoch"] == 0
        self._assert_current(env, r.json()["access_token"], r.json()["user"]["id"])

    def test_invitation_accept_by_an_account_that_has_reset(self, env: Env):
        _reset(env, BOB)
        inv = invite(env, EMAIL[BOB])
        r = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, user_id=BOB)
        assert r.status_code == 200, r.text
        assert claims(r.json()["access_token"])["auth_epoch"] == 1
        self._assert_current(env, r.json()["access_token"], BOB)

    def test_invitation_accept_with_a_stale_session_is_refused(self, env: Env):
        stale = bearer(env, BOB)
        _reset(env, BOB)
        inv = invite(env, EMAIL[BOB])
        assert _refused(post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=stale)) == (
            UNAUTHORIZED
        )
