"""P6-AUTH-4: server-side sessions -- issuance, enforcement, re-issue and revocation.

    POST /api/v1/auth/logout       Bearer -> 204  revoke the token's own session
    POST /api/v1/auth/logout-all   Bearer -> 204  revoke every session of the account

Every access token names the ``auth_sessions`` row it belongs to (``sid``). A request
authenticates only if the JWT is valid, its ``sid`` is well formed, the user exists and is
active, the credential epoch matches, and the session row belongs to that user and is
unrevoked and unexpired on the DATABASE clock -- any failure is a 401 (authentication);
membership and role failures stay 403 (authorization). Exactly three routes open a session
(login, register, invitation register), each committing the row before the token exists;
``/auth/me`` and invitation accept re-issue inside the presented session, never past its
fixed absolute expiry. A pre-AUTH4 token has no ``sid`` and is refused: a one-time sign-in.

Only ``get_db`` is overridden; committed state is read through a separate ``Engine``. Every
refusal follows a positive control: the same token (or the same session) first succeeds.
The PostgreSQL halves (concurrency, the database deadline, skewed app clocks) are in
``test_auth4_database.py``; the credential-epoch interplay is in ``test_auth_epoch.py``;
session-id confusion is in ``test_auth_token_confusion.py``.
"""

from __future__ import annotations

import dataclasses
import inspect
import math
import os
import random
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth.models import AuthSession
from app.core import security
from app.core.config import get_settings
from app.core.security import RESERVED_CLAIMS, create_access_token
from app.main import app
from app.organizations.models import Organization, OrganizationMember, User
from app.tests._auth2_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    EMAIL,
    INACTIVE,
    INVITATION_ACCEPT,
    INVITATION_REGISTER,
    LOGIN,
    LOGOUT,
    LOGOUT_ALL,
    ME,
    OLD_PASSWORD,
    ORG_A,
    ORG_B,
    REGISTER,
    WS_A,
    WS_B,
    Env,
    active_rate_limiter,
    as_utc,
    claims,
    environment,
    get_me,
    invite,
    login,
    open_session,
    post,
    raw_token,
)

API_DIR = Path(__file__).resolve().parents[2]
PREV = "a452ee007cc2"
HEAD = "87198ab57b59"
UNAUTHORIZED = (401, "unauthorized", "Invalid or expired token.")
ORGANIZATIONS = f"{API}/organizations"
LIFETIME = timedelta(minutes=720)


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "sessions.db") as e:
        yield e


def _ttl_s() -> int:
    return get_settings().access_token_expire_minutes * 60


def _refused(r) -> tuple[int, str, str]:
    body = r.json()["error"]
    return r.status_code, body["code"], body["message"]


def _get(env: Env, url: str, token: str):
    active_rate_limiter()._hits.clear()
    return env.client.get(url, headers={"Authorization": f"Bearer {token}"})


def _deadline(env: Env, sid: str) -> int:
    """The session's absolute expiry in whole epoch seconds (jose floors ``exp`` the same way)."""
    return math.floor(as_utc(env.witness.session_row(sid)["expires_at"]).timestamp())


def _assert_capped(env: Env, access_token: str) -> None:
    token = claims(access_token)
    assert token["exp"] == min(token["iat"] + _ttl_s(), _deadline(env, token["sid"]))


#: The three fresh-session boundaries: sign-in, registration, invited registration.
BOUNDARIES = ("login", "register", "invitation-register")


def _boundary(env: Env, name: str) -> Callable[[], object]:
    """The named boundary's request, ready to send: its prerequisites (an invitation, whose
    creation opens a session for the inviter) are made now, so the request itself is the
    only thing that can open a session."""
    if name == "login":
        body = {"email": EMAIL[ALICE], "password": OLD_PASSWORD}
        url = LOGIN
    elif name == "register":
        body = {
            "email": "founder@example.com",
            "full_name": "Founder",
            "password": OLD_PASSWORD,
            "organization_name": "Founder Co",
        }
        url = REGISTER
    else:
        token = invite(env, "newcomer@example.com")["token"]
        body = {"token": token, "full_name": "Newcomer", "password": OLD_PASSWORD}
        url = INVITATION_REGISTER
    return lambda: post(env, url, body)


# --------------------------------------------------------------------------- #
class TestSessionBoundaries:
    """T-01, T-22, T-23: exactly three routes open a session, each on the database clock."""

    @pytest.mark.parametrize("boundary", BOUNDARIES)
    def test_each_boundary_opens_exactly_one_session(self, env: Env, boundary: str):
        send = _boundary(env, boundary)
        opened = env.witness.count(AuthSession)
        r = send()
        assert r.status_code in (200, 201), r.text
        assert env.witness.count(AuthSession) == opened + 1
        token = claims(r.json()["access_token"])
        row = env.witness.session_row(token["sid"])
        assert (row["user_id"], row["revoked_at"]) == (r.json()["user"]["id"], None)
        assert token["auth_epoch"] == env.witness.user(row["user_id"])["auth_epoch"]
        assert get_me(env, r.json()["access_token"]).status_code == 200

    def test_me_and_accept_open_no_session(self, env: Env):
        inv = invite(env, EMAIL[BOB])
        token, sid = open_session(env.request_engine, BOB)
        opened = env.witness.count(AuthSession)
        me = get_me(env, token)
        assert me.status_code == 200 and claims(me.json()["access_token"])["sid"] == sid
        accepted = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=token)
        assert accepted.status_code == 200, accepted.text
        assert claims(accepted.json()["access_token"])["sid"] == sid
        assert env.witness.count(AuthSession) == opened

    def test_each_boundary_stamps_the_database_clock_and_a_720_minute_deadline(self, env: Env):
        """T-22 (UNIT half): distinct sessions; ``db_t0 <= created_at <= db_t1`` read with the
        SQLite millisecond clock the service itself reads; ``expires_at = created_at + 720``."""
        sends = [_boundary(env, name) for name in BOUNDARIES]
        sids = []
        for send in sends:
            t0 = env.witness.db_now()
            r = send()
            t1 = env.witness.db_now()
            assert r.status_code in (200, 201), r.text
            sid = claims(r.json()["access_token"])["sid"]
            row = env.witness.session_row(sid)
            created, expires = as_utc(row["created_at"]), as_utc(row["expires_at"])
            assert t0 <= created <= t1
            assert expires - created == LIFETIME
            _assert_capped(env, r.json()["access_token"])
            sids.append(sid)
        assert len(set(sids)) == 3

    @pytest.mark.parametrize("boundary", BOUNDARIES)
    def test_the_session_is_committed_before_the_token_is_returned(self, tmp_path, boundary: str):
        """T-23 (UNIT half): under a teardown that never commits, the token still works."""
        with environment(tmp_path / "withheld.db", teardown_commit=False) as env:
            r = _boundary(env, boundary)()
            assert r.status_code in (200, 201), r.text
            sid = claims(r.json()["access_token"])["sid"]
            assert env.witness.session_row(sid)["revoked_at"] is None
            assert get_me(env, r.json()["access_token"]).status_code == 200

    @pytest.mark.parametrize("boundary", BOUNDARIES)
    def test_a_failed_commit_returns_no_token(self, env: Env, monkeypatch, boundary: str):
        """T-23 (UNIT half): the commit comes first, so when it fails there is no token."""
        quiet = dataclasses.replace(env, client=TestClient(app, raise_server_exceptions=False))
        send = _boundary(quiet, boundary)
        opened = env.witness.count(AuthSession)

        def refuse(self):
            raise RuntimeError("forced commit failure")

        with monkeypatch.context() as m:
            m.setattr(Session, "commit", refuse)
            r = send()
        assert r.status_code == 500
        assert "access_token" not in r.text
        assert env.witness.count(AuthSession) == opened


# --------------------------------------------------------------------------- #
class TestTokenBuilder:
    """T-05: one clock read per mint; ``exp = min(iat + TTL, the session's deadline)``."""

    #: Every exp stays in the future on jose's real clock for any run before 2100.
    BASE = datetime(2100, 1, 1, tzinfo=UTC)

    def _stepping(self, monkeypatch) -> list[int]:
        """Replace the builder's clock: the k-th ``now()`` returns BASE + k seconds."""
        calls = [0]
        base = self.BASE

        class Stepping(datetime):
            @classmethod
            def now(cls, tz=None):
                calls[0] += 1
                value = base + timedelta(seconds=calls[0])
                return value if tz is not None else value.replace(tzinfo=None)

        monkeypatch.setattr(security, "datetime", Stepping)
        return calls

    def _mint(self, expires_at: datetime) -> dict:
        return claims(
            create_access_token(
                subject=ALICE,
                session_id=uuid.uuid4().hex,
                auth_epoch=0,
                expires_at_cap=expires_at,
                email=EMAIL[ALICE],
            )
        )

    def test_an_uncapped_token_reads_the_clock_once(self, monkeypatch):
        calls = self._stepping(monkeypatch)
        token = self._mint(self.BASE + timedelta(days=30))
        assert calls[0] == 1
        assert token["iat"] == math.floor((self.BASE + timedelta(seconds=1)).timestamp())
        assert token["exp"] - token["iat"] == _ttl_s()

    def test_a_capped_token_reads_the_clock_once_and_stops_at_the_deadline(self, monkeypatch):
        calls = self._stepping(monkeypatch)
        expires_at = self.BASE + timedelta(minutes=30, seconds=0.5)
        token = self._mint(expires_at)
        assert calls[0] == 1
        assert token["exp"] == math.floor(expires_at.timestamp())
        assert token["exp"] <= expires_at.timestamp()

    def test_the_builder_takes_no_claims(self):
        parameters = inspect.signature(create_access_token).parameters
        assert set(parameters) == {"subject", "session_id", "auth_epoch", "expires_at_cap", "email"}
        assert {p.kind for p in parameters.values()} == {inspect.Parameter.KEYWORD_ONLY}
        assert RESERVED_CLAIMS == {
            *("sub", "sid", "auth_epoch", "iat", "exp", "nbf", "iss", "aud", "jti")
        }
        cap = datetime.now(UTC) + LIFETIME
        for extension in ("extra", "claims"):
            with pytest.raises(TypeError):
                create_access_token(
                    subject=ALICE,
                    session_id=uuid.uuid4().hex,
                    auth_epoch=0,
                    expires_at_cap=cap,
                    email=EMAIL[ALICE],
                    **{extension: {"sid": "0" * 32}},
                )
        token = claims(
            create_access_token(
                subject=ALICE,
                session_id=uuid.uuid4().hex,
                auth_epoch=0,
                expires_at_cap=cap,
                email=EMAIL[ALICE],
            )
        )
        assert set(token) - RESERVED_CLAIMS == {"email"}


# --------------------------------------------------------------------------- #
class TestJwtLayer:
    def test_an_expired_jwt_is_refused_on_a_live_session(self, env: Env):  # T-03
        _, sid = open_session(env.request_engine, ALICE)
        now = datetime.now(UTC)
        assert get_me(env, raw_token(sub=ALICE, sid=sid, auth_epoch=0)).status_code == 200
        past = {"iat": now - timedelta(hours=1), "exp": now - timedelta(seconds=5)}
        expired = raw_token(sub=ALICE, sid=sid, auth_epoch=0, **past)
        assert _refused(get_me(env, expired)) == UNAUTHORIZED

    def test_a_foreign_signature_is_refused_on_a_live_session(self, env: Env):  # T-04
        _, sid = open_session(env.request_engine, ALICE)
        assert get_me(env, raw_token(sub=ALICE, sid=sid, auth_epoch=0)).status_code == 200
        forged = raw_token(sub=ALICE, sid=sid, auth_epoch=0, key="not-the-signing-key-" * 3)
        assert _refused(get_me(env, forged)) == UNAUTHORIZED

    def test_an_inactive_user_is_an_authentication_failure(self, env: Env):  # T-35
        token, _ = open_session(env.request_engine, INACTIVE)
        assert _refused(get_me(env, token)) == (401, "unauthorized", "User not found or inactive.")
        env.witness.write(update(User).where(User.id == INACTIVE).values(is_active=True))
        assert get_me(env, token).status_code == 200


# --------------------------------------------------------------------------- #
class TestLogout:
    def test_logout_ends_only_its_own_session(self, env: Env):  # T-06
        s1, sid1 = open_session(env.request_engine, ALICE)
        s2, _ = open_session(env.request_engine, ALICE)
        s3, _ = open_session(env.request_engine, BOB)
        for token in (s1, s2, s3):
            assert get_me(env, token).status_code == 200
            assert _get(env, ORGANIZATIONS, token).status_code == 200
        assert post(env, LOGOUT, None, token=s1).status_code == 204
        assert _refused(get_me(env, s1)) == UNAUTHORIZED
        assert _refused(_get(env, ORGANIZATIONS, s1)) == UNAUTHORIZED
        for token in (s2, s3):
            assert get_me(env, token).status_code == 200
            assert _get(env, ORGANIZATIONS, token).status_code == 200
        revoked = {r["id"] for r in env.witness.sessions() if r["revoked_at"] is not None}
        assert revoked == {sid1}
        assert env.witness.user(ALICE)["auth_epoch"] == 0

    @pytest.mark.parametrize("end_with", ["original", "reissued"])
    def test_logout_ends_every_token_of_the_session(self, env: Env, end_with: str):  # T-07
        original, sid = open_session(env.request_engine, ALICE)
        reissued = get_me(env, original).json()["access_token"]
        assert claims(reissued)["sid"] == sid
        assert get_me(env, original).status_code == 200
        assert get_me(env, reissued).status_code == 200
        ending = original if end_with == "original" else reissued
        assert post(env, LOGOUT, None, token=ending).status_code == 204
        assert _refused(get_me(env, original)) == UNAUTHORIZED
        assert _refused(get_me(env, reissued)) == UNAUTHORIZED

    def test_me_on_a_revoked_session_returns_no_token(self, env: Env):  # T-08
        token, _ = open_session(env.request_engine, ALICE)
        assert get_me(env, token).status_code == 200
        assert post(env, LOGOUT, None, token=token).status_code == 204
        r = get_me(env, token)
        assert _refused(r) == UNAUTHORIZED
        assert "access_token" not in r.text

    def test_a_repeated_logout_is_refused(self, env: Env):  # T-09 (UNIT half)
        token, sid = open_session(env.request_engine, ALICE)
        assert post(env, LOGOUT, None, token=token).status_code == 204
        revoked_at = env.witness.session_row(sid)["revoked_at"]
        assert revoked_at is not None
        assert _refused(post(env, LOGOUT, None, token=token)) == UNAUTHORIZED
        assert env.witness.session_row(sid)["revoked_at"] == revoked_at

    def test_five_reissues_share_one_session_and_one_logout(self, env: Env):  # T-16
        token, sid = open_session(env.request_engine, ALICE)
        chain = [token]
        for _ in range(5):
            r = get_me(env, chain[-1])
            assert r.status_code == 200
            chain.append(r.json()["access_token"])
        assert get_me(env, chain[-1]).status_code == 200
        assert {claims(t)["sid"] for t in chain} == {sid}
        assert env.witness.sessions(ALICE) == [env.witness.session_row(sid)]
        assert post(env, LOGOUT, None, token=chain[3]).status_code == 204
        assert [get_me(env, t).status_code for t in chain] == [401] * 6


class TestLogoutAll:
    def test_logout_all_ends_every_session_of_the_account_only(self, env: Env):  # T-13
        mine = [open_session(env.request_engine, ALICE)[0] for _ in range(3)]
        theirs, _ = open_session(env.request_engine, BOB)
        for token in (*mine, theirs):
            assert get_me(env, token).status_code == 200
            assert _get(env, ORGANIZATIONS, token).status_code == 200
        assert post(env, LOGOUT_ALL, None, token=mine[0]).status_code == 204
        for token in mine:
            assert _refused(get_me(env, token)) == UNAUTHORIZED
            assert _refused(_get(env, ORGANIZATIONS, token)) == UNAUTHORIZED
        assert get_me(env, theirs).status_code == 200
        assert all(r["revoked_at"] is not None for r in env.witness.sessions(ALICE))
        assert all(r["revoked_at"] is None for r in env.witness.sessions(BOB))
        assert env.witness.user(ALICE)["auth_epoch"] == 0
        fresh = login(env, EMAIL[ALICE], OLD_PASSWORD).json()["access_token"]
        assert claims(fresh)["sid"] not in {claims(t)["sid"] for t in mine}
        assert get_me(env, fresh).status_code == 200


# --------------------------------------------------------------------------- #
class TestReissue:
    def test_me_reissues_in_the_same_session_and_deadline(self, env: Env, monkeypatch):  # T-15
        moment = [datetime.now(UTC).replace(microsecond=0)]
        real = datetime

        class Frozen(real):
            @classmethod
            def now(cls, tz=None):
                return moment[0] if tz is not None else moment[0].replace(tzinfo=None)

        monkeypatch.setattr(security, "datetime", Frozen)
        token, sid = open_session(env.request_engine, ALICE)
        row = env.witness.session_row(sid)
        first = get_me(env, token).json()["access_token"]
        moment[0] += timedelta(seconds=1)
        second = get_me(env, first).json()["access_token"]
        one, two = claims(first), claims(second)
        assert one["sid"] == two["sid"] == sid
        assert env.witness.session_row(sid) == row
        for access_token in (first, second):
            _assert_capped(env, access_token)
        assert two["iat"] > one["iat"]

    def test_accept_reissues_in_the_same_session_and_deadline(self, env: Env):  # T-18
        inv = invite(env, EMAIL[BOB], "marketer")
        token, sid = open_session(env.request_engine, BOB)
        row, opened = env.witness.session_row(sid), env.witness.count(AuthSession)
        r = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=token)
        assert r.status_code == 200, r.text
        accepted = r.json()["access_token"]
        assert claims(accepted)["sid"] == sid
        assert env.witness.session_row(sid) == row
        assert env.witness.count(AuthSession) == opened
        _assert_capped(env, accepted)
        members = env.witness.rows(OrganizationMember, OrganizationMember.user_id == BOB)
        assert {(m["organization_id"], m["role"]) for m in members} == {
            (ORG_A, "marketer"),
            (ORG_B, "owner"),
        }
        assert get_me(env, accepted).status_code == 200

    def test_accept_on_a_revoked_session_changes_nothing(self, env: Env):  # T-19
        inv = invite(env, EMAIL[BOB])
        token, _ = open_session(env.request_engine, BOB)
        assert get_me(env, token).status_code == 200
        assert post(env, LOGOUT, None, token=token).status_code == 204
        before = env.witness.snapshot()
        r = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=token)
        assert _refused(r) == UNAUTHORIZED
        assert "access_token" not in r.text
        assert env.witness.snapshot() == before  # no membership; the invitation still pending

    @pytest.mark.parametrize("end_with", ["original", "reissued"])
    def test_a_stolen_bearer_cannot_fork_a_session_through_accept(
        self, env: Env, end_with: str
    ):  # T-21 (UNIT half)
        victim, sid = open_session(env.request_engine, BOB)
        assert get_me(env, victim).status_code == 200
        expires_at = env.witness.session_row(sid)["expires_at"]
        inv = invite(env, EMAIL[BOB])  # the attacker's organization invites the victim ...
        r = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=victim)  # ... and accepts
        assert r.status_code == 200, r.text
        forked = r.json()["access_token"]
        assert claims(forked)["sid"] == sid
        assert env.witness.session_row(sid)["expires_at"] == expires_at
        assert get_me(env, forked).status_code == 200
        ending = victim if end_with == "original" else forked
        assert post(env, LOGOUT, None, token=ending).status_code == 204
        assert _refused(get_me(env, victim)) == UNAUTHORIZED
        assert _refused(get_me(env, forked)) == UNAUTHORIZED

    def test_interleaved_reissues_keep_one_session_and_deadline(self, env: Env):  # T-24
        """Property, fixed seed: /me and accept in a shuffled order never leave the session
        or its deadline."""
        orgs = [f"org-t24-{i}" for i in range(3)]
        with env.witness.session() as s:
            for org in orgs:
                s.add(Organization(id=org, name=org, slug=org))
                s.flush()
                s.add(OrganizationMember(organization_id=org, user_id=ALICE, role="owner"))
            s.commit()
        invitations = []
        for org in orgs:
            body = {"email": EMAIL[CAROL], "role": "viewer"}
            r = post(env, f"{ORGANIZATIONS}/{org}/invitations", body, user_id=ALICE)
            assert r.status_code == 201, r.text
            invitations.append(r.json()["token"])
        token, sid = open_session(env.request_engine, CAROL)
        row, opened, deadline = (
            env.witness.session_row(sid),
            env.witness.count(AuthSession),
            _deadline(env, sid),
        )
        steps = ["me"] * 9 + ["accept"] * len(invitations)
        random.Random(20260928).shuffle(steps)
        current = token
        for step in steps:
            if step == "me":
                r = get_me(env, current)
            else:
                r = post(env, INVITATION_ACCEPT, {"token": invitations.pop()}, token=current)
            assert r.status_code == 200, (step, r.text)
            current = r.json()["access_token"]
            assert claims(current)["sid"] == sid
            assert claims(current)["exp"] <= deadline
            _assert_capped(env, current)
        assert env.witness.session_row(sid) == row
        assert env.witness.count(AuthSession) == opened


# --------------------------------------------------------------------------- #
class TestLegacyTokens:
    """A pre-AUTH4 token -- current key, current epoch, unexpired, NO ``sid`` -- is refused
    everywhere (T-12, T-25, T-26); a same-user token with a live ``sid`` is the control."""

    def _legacy(self, user_id: str = ALICE) -> str:
        return raw_token(sub=user_id, auth_epoch=0, email=EMAIL[user_id])

    def test_logout_and_logout_all_refuse_a_legacy_token(self, env: Env):  # T-12
        live, _ = open_session(env.request_engine, ALICE)
        before = env.witness.sessions()
        assert _refused(post(env, LOGOUT, None, token=self._legacy())) == UNAUTHORIZED
        assert _refused(post(env, LOGOUT_ALL, None, token=self._legacy())) == UNAUTHORIZED
        assert env.witness.sessions() == before
        assert post(env, LOGOUT, None, token=live).status_code == 204

    ROUTES = [
        ("authenticated", ORGANIZATIONS),
        ("organization-membership", f"{ORGANIZATIONS}/{ORG_A}/members"),
        ("handler-membership", f"{ORGANIZATIONS}/{ORG_A}/workspaces"),
        ("workspace-membership", f"{API}/workspaces/{WS_A}/brands"),
        ("role-gated", f"{ORGANIZATIONS}/{ORG_A}/invitations"),
        ("operator", f"{API}/internal/system/capabilities"),
    ]

    @pytest.mark.parametrize(("route_class", "url"), ROUTES, ids=[c for c, _ in ROUTES])
    def test_a_legacy_token_is_refused_on_every_route_class(
        self, env: Env, route_class: str, url: str
    ):  # T-25
        env.witness.write(update(User).where(User.id == ALICE).values(is_operator=True))
        live, _ = open_session(env.request_engine, ALICE)
        assert _get(env, url, live).status_code == 200
        assert _refused(_get(env, url, self._legacy())) == UNAUTHORIZED

    def test_me_and_accept_refuse_a_legacy_token_and_open_nothing(self, env: Env):  # T-25, T-26
        inv = invite(env, EMAIL[BOB])
        live, _ = open_session(env.request_engine, BOB)
        assert get_me(env, live).status_code == 200
        opened, before = env.witness.count(AuthSession), env.witness.snapshot()
        me = get_me(env, self._legacy(BOB))
        assert _refused(me) == UNAUTHORIZED and "access_token" not in me.text
        accepted = post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=self._legacy(BOB))
        assert _refused(accepted) == UNAUTHORIZED and "access_token" not in accepted.text
        assert env.witness.count(AuthSession) == opened
        assert env.witness.snapshot() == before  # no membership; the invitation still pending
        assert post(env, INVITATION_ACCEPT, {"token": inv["token"]}, token=live).status_code == 200


# --------------------------------------------------------------------------- #
def test_authentication_and_authorization_failures_stay_apart(env: Env):  # T-36
    token, _ = open_session(env.request_engine, ALICE)
    own, other = f"{API}/workspaces/{WS_A}/brands", f"{API}/workspaces/{WS_B}/brands"
    assert _get(env, own, token).status_code == 200
    forbidden = _get(env, other, token)
    assert forbidden.status_code == 403  # authorization: the token itself is still good ...
    assert _get(env, own, token).status_code == 200  # ... as the next request shows
    assert post(env, LOGOUT, None, token=token).status_code == 204
    for url in (own, other, ORGANIZATIONS, ME):
        assert _refused(_get(env, url, token)) == UNAUTHORIZED


# --------------------------------------------------------------------------- #
# T-32 (UNIT half): the migration, through the real Alembic CLI on SQLite
# --------------------------------------------------------------------------- #
def _downgrade_confirmation(db_path: Path, args: tuple[str, ...]) -> list[str]:
    """The target-bound downgrade guard's confirmation for this temporary database (as the
    sibling migration tests compute it; its algorithm is pinned elsewhere by literal value)."""
    if not args or args[0] != "downgrade":
        return []
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine

    from app.db.downgrade_guard import (
        chain_identity,
        confirmation_token,
        live_identity,
        resolve_downgrade,
    )

    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    with engine.connect() as conn:
        heads = MigrationContext.configure(conn).get_current_heads()
        identity = live_identity(conn, url)
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


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    confirm = _downgrade_confirmation(db_path, args)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *confirm, *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def _master(db_path: Path, kind: str) -> dict[str, str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT name, sql FROM sqlite_master WHERE type = ?", (kind,))
        return {name: sql or "" for name, sql in rows}
    finally:
        con.close()


class TestMigration:
    def test_single_head_follows_the_account_token_revision(self, tmp_path):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
        assert [r.revision for r in script.get_revisions("heads")] == [HEAD]
        revision = script.get_revision(HEAD)
        assert revision.down_revision == PREV
        assert Path(revision.path).name.endswith("_add_auth_sessions.py")
        result = _alembic(tmp_path / "heads.db", "heads")
        assert result.returncode == 0, result.stderr
        heads = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(heads) == 1 and HEAD in heads[0], result.stdout

    def test_upgrade_check_downgrade_and_reupgrade(self, tmp_path):
        db_path = tmp_path / "migration.db"
        assert _alembic(db_path, "upgrade", "head").returncode == 0
        tables, indexes = _master(db_path, "table"), _master(db_path, "index")
        assert "auth_sessions" in tables and "ix_auth_sessions_user_id" in indexes
        ddl = " ".join(tables["auth_sessions"].split())
        assert "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE" in ddl
        assert "created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL" in ddl
        assert "expires_at DATETIME NOT NULL" in ddl and "revoked_at DATETIME," in ddl
        check = _alembic(db_path, "check")
        assert check.returncode == 0, check.stdout + check.stderr

        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT INTO organizations (id, name, slug) VALUES (?, ?, ?)",
                ("org-keep-auth4", "Keep Co", "keep-co"),
            )
            con.commit()
        finally:
            con.close()
        down = _alembic(db_path, "downgrade", PREV)
        assert down.returncode == 0, down.stderr
        tables = _master(db_path, "table")
        assert "auth_sessions" not in tables and "users" in tables
        assert "ix_auth_sessions_user_id" not in _master(db_path, "index")
        con = sqlite3.connect(db_path)
        try:
            kept = con.execute("SELECT name FROM organizations WHERE id = 'org-keep-auth4'")
            assert kept.fetchone() == ("Keep Co",)
        finally:
            con.close()
        assert _alembic(db_path, "upgrade", "head").returncode == 0
        assert "auth_sessions" in _master(db_path, "table")

    def test_deleting_a_user_deletes_their_sessions(self, env: Env):
        open_session(env.request_engine, CAROL)
        _, kept = open_session(env.request_engine, BOB)
        with env.witness.session() as s:
            s.execute(
                OrganizationMember.__table__.delete().where(OrganizationMember.user_id == CAROL)
            )
            s.execute(User.__table__.delete().where(User.id == CAROL))
            s.commit()
        assert env.witness.sessions(CAROL) == []
        assert [r["id"] for r in env.witness.sessions(BOB)] == [kept]
