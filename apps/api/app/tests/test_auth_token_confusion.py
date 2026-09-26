"""6B-4A: a token minted for one purpose is useless for every other (P6-AUTH-2).

Three kinds of one-time token exist: password reset, email verification (6B-4A) and
organization invitation (6B-3A). Each lives in its own table under its own digest --
``sha256("password-reset:" + raw)``, ``sha256("email-verification:" + raw)`` and the
invitation's unprefixed ``sha256(raw)`` -- so a raw token can only ever resolve where it was
minted. All six wrong-purpose directions are tried against real, live tokens; each is refused
with the target endpoint's own "invalid" code, and the presented token is left intact and
still works where it belongs.

The digest separation is also checked on its own, without relying on separate tables: a row
planted under ANOTHER purpose's digest of a raw value is not found when that raw value is
presented, while a row under the right digest is (positive control).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.organizations.models import OrganizationInvitation
from app.tests._auth2_support import (
    ALICE,
    BOB,
    CAROL,
    INVITATION_ACCEPT,
    INVITATION_PREVIEW,
    INVITATION_REGISTER,
    OLD_PASSWORD,
    Env,
    code,
    confirm_reset,
    confirm_verification,
    environment,
    invite,
    mint_reset,
    mint_verification,
    past_cooldown,
    plain_digest,
    post,
    reset_digest,
    verification_digest,
)

RESET_INVALID = (404, "password_reset_invalid")
VERIFICATION_INVALID = (404, "email_verification_invalid")
INVITATION_INVALID = (404, "invitation_invalid")


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "confusion.db") as e:
        yield e


@pytest.fixture
def live(env: Env) -> dict[str, str]:
    """One live token of each kind. ALICE holds the reset and verification tokens; BOB is
    invited (existing-account path) and ``newcomer@example.com`` (registration path)."""
    reset = mint_reset(env, ALICE)
    past_cooldown(env, PasswordResetToken, ALICE)  # in case the cooldown is shared
    verification = mint_verification(env, ALICE)
    return {
        "reset": reset,
        "verification": verification,
        "invitation": invite(env, "bob@example.com")["token"],
        "invitation-new": invite(env, "newcomer@example.com")["token"],
    }


def _all_tokens(env: Env) -> tuple:
    return env.witness.snapshot(PasswordResetToken, EmailVerificationToken, OrganizationInvitation)


def _present(env: Env, where: str, raw: str):
    if where == "reset":
        return confirm_reset(env, raw, "never-applied-9")
    if where == "verification-as-alice":
        return confirm_verification(env, raw, ALICE)
    if where == "verification-as-bob":
        return confirm_verification(env, raw, BOB)
    if where == "invitation-preview":
        return post(env, INVITATION_PREVIEW, {"token": raw})
    if where == "invitation-register":
        body = {"token": raw, "full_name": "Confused", "password": OLD_PASSWORD}
        return post(env, INVITATION_REGISTER, body)
    assert where == "invitation-accept"
    return post(env, INVITATION_ACCEPT, {"token": raw}, user_id=BOB)


# (token kind, where it is presented, expected refusal)
DIRECTIONS = [
    ("reset", "verification-as-alice", VERIFICATION_INVALID),
    ("reset", "invitation-preview", INVITATION_INVALID),
    ("reset", "invitation-register", INVITATION_INVALID),
    ("reset", "invitation-accept", INVITATION_INVALID),
    ("verification", "reset", RESET_INVALID),
    ("verification", "invitation-preview", INVITATION_INVALID),
    ("verification", "invitation-register", INVITATION_INVALID),
    ("verification", "invitation-accept", INVITATION_INVALID),
    ("invitation", "reset", RESET_INVALID),
    ("invitation", "verification-as-bob", VERIFICATION_INVALID),
    ("invitation-new", "reset", RESET_INVALID),
    ("invitation-new", "verification-as-alice", VERIFICATION_INVALID),
]


def _still_works(env: Env, kind: str, raw: str) -> None:
    if kind == "reset":
        assert confirm_reset(env, raw).status_code == 204
    elif kind == "verification":
        assert confirm_verification(env, raw, ALICE).status_code == 204
    elif kind == "invitation":
        assert post(env, INVITATION_ACCEPT, {"token": raw}, user_id=BOB).status_code == 200
    else:
        body = {"token": raw, "full_name": "Newcomer", "password": OLD_PASSWORD}
        assert post(env, INVITATION_REGISTER, body).status_code == 201


@pytest.mark.parametrize(
    ("kind", "where", "refusal"), DIRECTIONS, ids=[f"{k}->{w}" for k, w, _ in DIRECTIONS]
)
def test_a_token_is_refused_everywhere_but_home_and_survives(
    env: Env, live: dict[str, str], kind: str, where: str, refusal: tuple[int, str]
):
    raw = live[kind]
    before = env.witness.snapshot()
    assert code(_present(env, where, raw)) == refusal
    assert env.witness.snapshot() == before
    _still_works(env, kind, raw)


def test_all_six_directions_are_covered():
    """Reset, verification and invitation, each presented to both of the other two."""
    families = {"reset": "reset", "verification": "verification", "invitation": "invitation"}

    def family(name: str) -> str:
        return next(f for prefix, f in families.items() if name.startswith(prefix))

    pairs = {(family(k), family(w)) for k, w, _ in DIRECTIONS}
    assert pairs == {(a, b) for a in families for b in families if a != b}


# --------------------------------------------------------------------------- #
class TestDigestsArePurposeBound:
    """A row stored under another purpose's digest of ``raw`` must not answer to ``raw``."""

    @pytest.mark.parametrize(
        "digest", [verification_digest, plain_digest], ids=["verification-digest", "unprefixed"]
    )
    def test_reset_confirm_needs_the_reset_digest(self, env: Env, digest):
        raw = "cross-purpose-raw-token-0001"
        env.witness.plant(PasswordResetToken, user_id=CAROL, digest=digest(raw))
        before = _all_tokens(env)
        assert code(confirm_reset(env, raw)) == RESET_INVALID
        assert _all_tokens(env) == before

    @pytest.mark.parametrize(
        "digest", [reset_digest, plain_digest], ids=["reset-digest", "unprefixed"]
    )
    def test_verification_confirm_needs_the_verification_digest(self, env: Env, digest):
        raw = "cross-purpose-raw-token-0002"
        env.witness.plant(EmailVerificationToken, user_id=CAROL, digest=digest(raw))
        before = _all_tokens(env)
        assert code(confirm_verification(env, raw, CAROL)) == VERIFICATION_INVALID
        assert _all_tokens(env) == before

    def test_the_right_digest_is_found(self, env: Env):
        """Positive control: the planted-row technique does reach the service."""
        env.witness.plant(PasswordResetToken, user_id=CAROL, digest=reset_digest("home-1"))
        env.witness.plant(EmailVerificationToken, user_id=BOB, digest=verification_digest("home-2"))
        assert confirm_reset(env, "home-1").status_code == 204
        assert confirm_verification(env, "home-2", BOB).status_code == 204

    def test_the_three_digests_of_one_raw_value_differ(self):
        raw = "same-raw-value"
        assert len({reset_digest(raw), verification_digest(raw), plain_digest(raw)}) == 3
