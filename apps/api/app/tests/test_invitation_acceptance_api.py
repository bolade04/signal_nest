"""6B-3A: accepting an organization invitation (P6-AUTH-1).

    POST /api/v1/auth/invitations/preview    public, {token}                   -> 200
    POST /api/v1/auth/invitations/register   public, {token, full_name, password} -> 201 SessionOut
    POST /api/v1/auth/invitations/accept     Bearer, {token}                   -> 200 SessionOut

A second person joins an EXISTING organization with the invitation's non-OWNER role. A new
person registers through the invitation, which creates their account and membership and no
organization; a person who already has an account accepts while signed in with the invited
address, keeping every membership they already hold.

Invitations here are created through the real ``POST /organizations/{id}/invitations`` route
by seeded OWNER/ADMIN members, so each token is one the product actually issued. Only
``get_db`` is overridden; committed state is read through a separate ``Engine``.

Load-bearing properties, each with its own test:

* **Preview consumes nothing.** Repeated previews return the same body and leave the row,
  the users, the memberships and the audit trail byte-for-byte as they were; the token
  still registers afterwards, and only then does preview report it used.
* **Invited registration creates a user and a membership, never an organization.** Counted
  before and after. The ordinary ``POST /auth/register`` keeps creating User + Organization
  + OWNER membership, pinned alongside.
* **An existing user keeps every membership.** Same ``User`` row, organization A kept,
  organization B added with a different role; the SessionOut lists both.
* **The invited address is the only one that can accept.** Bob, signed in, presenting
  Alice's token gets 403 and the invitation stays pending with no membership for Bob.
* **One-time, and every dead token is refused with a stable code and no side effect.**
  Replay -> ``invitation_already_used``; revoked -> ``invitation_revoked``; expired ->
  ``invitation_expired``; unknown -> 404 ``invitation_invalid``; each checked on preview,
  register and accept independently, with the database compared before and after.
* **The inviter's authority is re-checked at acceptance.** Inviter removed, demoted below
  ADMIN, or deleted -> 409 ``invitation_inviter_not_authorized``, invitation unaccepted.
* **The caller supplies only the token.** ``organization_id``, ``role``, ``email`` and
  ``is_operator`` in any acceptance body are rejected (422), and nothing moves.
* **Committed before the response (§39)**, shown under a teardown that never commits.
* **No raw token in any captured log record** across create, preview, register and accept.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.invitations import MAX_TOKEN_LENGTH, hash_invitation_token
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
)

API = get_settings().api_prefix
PREVIEW = f"{API}/auth/invitations/preview"
REGISTER = f"{API}/auth/invitations/register"
ACCEPT = f"{API}/auth/invitations/accept"

ORG_A, ORG_B = "org-acc-a", "org-acc-b"
A_OWNER, A_ADMIN, B_OWNER = "acc-a-owner", "acc-a-admin", "acc-b-owner"
ALICE, BOB = "acc-alice", "acc-bob"  # existing accounts; Alice is ADMIN of B, Bob is in nothing
# Local-part case variants (EmailStr lowercases only the domain): stored exactly as written.
CASEY, CASEY_EMAIL = "acc-casey", "casey@example.com"
DANA, DANA_EMAIL = "acc-dana", "Dana@example.com"
PASSWORD = "correct-horse-9"

SESSION_FIELDS = {"access_token", "token_type", "user", "memberships"}
PREVIEW_FIELDS = {"organization_id", "organization_name", "email", "role", "expires_at"}
WRONG_EMAIL = "This invitation was issued to a different email address."


def _email(user_id: str) -> str:
    return f"{user_id}@example.com"


def _as_utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _active_rate_limiter() -> RateLimitMiddleware:
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None, "RateLimitMiddleware not found in app.middleware_stack"
    return node


def _sqlite_file_engine(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _seed(s) -> None:
    s.add(Organization(id=ORG_A, name="Acme A", slug="acme-a"))
    s.add(Organization(id=ORG_B, name="Bravo B", slug="bravo-b"))
    s.flush()
    for user_id in (A_OWNER, A_ADMIN, B_OWNER, ALICE, BOB):
        s.add(User(id=user_id, email=_email(user_id), full_name=user_id, hashed_password="x"))
    s.add(User(id=CASEY, email=CASEY_EMAIL, full_name="Casey", hashed_password="x"))
    s.add(User(id=DANA, email=DANA_EMAIL, full_name="Dana", hashed_password="x"))
    s.flush()
    for org, user_id, role in (
        (ORG_A, A_OWNER, "owner"),
        (ORG_A, A_ADMIN, "admin"),
        (ORG_B, B_OWNER, "owner"),
        (ORG_B, ALICE, "admin"),
    ):
        s.add(OrganizationMember(organization_id=org, user_id=user_id, role=role))
    s.commit()


Snapshot = dict[str, tuple]


@dataclass(frozen=True)
class Witness:
    engine: Engine

    def session(self):
        return sessionmaker(bind=self.engine, autoflush=False, future=True)()

    def db_now(self) -> datetime:
        """The database clock (SQLite: millisecond ``'now'``, as the service reads it)."""
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                clock = select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))
            else:
                clock = select(func.clock_timestamp())
            return _as_utc(conn.execute(clock).scalar())

    def snapshot(self) -> Snapshot:
        """Every row of every table this feature can touch, in a stable order."""
        out = {}
        with self.session() as s:
            for model in (User, Organization, OrganizationMember, OrganizationInvitation, AuditLog):
                cols = [c for c in model.__table__.c]
                rows = s.execute(select(*cols).order_by(model.__table__.c.id)).all()
                out[model.__tablename__] = tuple(tuple(r) for r in rows)
        return out

    def invitation(self, invitation_id: str) -> dict:
        with self.session() as s:
            row = s.get(OrganizationInvitation, invitation_id)
            return {c.key: getattr(row, c.key) for c in OrganizationInvitation.__table__.c}

    def memberships(self) -> set[tuple[str, str, str]]:
        with self.session() as s:
            return set(
                s.execute(
                    select(
                        OrganizationMember.user_id,
                        OrganizationMember.organization_id,
                        OrganizationMember.role,
                    )
                )
            )

    def user_by_email(self, email: str) -> dict | None:
        with self.session() as s:
            u = s.scalar(select(User).where(User.email == email))
            return None if u is None else {"id": u.id, "email": u.email, "full_name": u.full_name}

    def count(self, model) -> int:
        with self.session() as s:
            return s.scalar(select(func.count()).select_from(model))

    def audit(self, action: str) -> list[dict]:
        with self.session() as s:
            return [
                {c.key: getattr(r, c.key) for c in AuditLog.__table__.c}
                for r in s.scalars(select(AuditLog).where(AuditLog.action == action))
            ]

    def write(self, stmt) -> int:
        with self.engine.begin() as conn:
            return conn.execute(stmt).rowcount


@dataclass(frozen=True)
class Env:
    client: TestClient
    witness: Witness
    request_engine: Engine


@contextmanager
def _pg_database() -> Iterator[str]:  # pragma: no cover - gated on live PG
    """A PostgreSQL database of this test's own: created here, dropped on the way out."""
    base = make_url(os.environ["TEST_POSTGRES_URL"])
    name = f"sn_acc_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def _environment(db_file: Path | None, *, teardown_commit: bool = True) -> Iterator[Env]:
    """SQLite file at ``db_file``; or, with ``db_file=None``, a PostgreSQL database of its own."""
    if db_file is None:  # pragma: no cover - gated on live PG
        with _pg_database() as url:
            with _built_environment(
                create_engine(url, future=True),
                create_engine(url, future=True),
                teardown_commit=teardown_commit,
            ) as env:
                yield env
        return
    with _built_environment(
        _sqlite_file_engine(db_file), _sqlite_file_engine(db_file), teardown_commit=teardown_commit
    ) as env:
        yield env


@contextmanager
def _built_environment(request_engine: Engine, witness_engine: Engine, *, teardown_commit: bool):
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
    )
    with factory() as s:
        _seed(s)

    def _override_get_db():
        s = factory()
        try:
            yield s
            if teardown_commit:
                s.commit()
            else:
                s.rollback()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    _active_rate_limiter()._hits.clear()
    try:
        yield Env(TestClient(app), Witness(witness_engine), request_engine)
    finally:
        app.dependency_overrides.clear()
        _active_rate_limiter()._hits.clear()
        request_engine.dispose()
        witness_engine.dispose()


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with _environment(tmp_path / "acceptance.db") as e:
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
    """The same environment on SQLite and, when ``TEST_POSTGRES_URL`` is set, PostgreSQL."""
    db_file = tmp_path / "acceptance.db" if request.param == "sqlite" else None
    with _environment(db_file) as e:
        yield e


@pytest.fixture
def withheld_env(tmp_path) -> Iterator[Env]:
    with _environment(tmp_path / "acceptance-withheld.db", teardown_commit=False) as e:
        yield e


def _post(
    env: Env, url: str, body: object, *, token: str | None = None, user_id: str | None = None
):
    assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
    _active_rate_limiter()._hits.clear()
    headers = {}
    if user_id is not None:
        token = create_access_token(user_id)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return env.client.post(url, json=body, headers=headers)


def _invite(
    env: Env, email: str, role: str = "marketer", *, inviter: str = A_OWNER, org: str = ORG_A
) -> dict:
    r = _post(
        env,
        f"{API}/organizations/{org}/invitations",
        {"email": email, "role": role},
        user_id=inviter,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _code(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


def _register_body(token: str, full_name: str = "Newcomer", password: str = PASSWORD) -> dict:
    return {"token": token, "full_name": full_name, "password": password}


def _expire(env: Env, invitation_id: str) -> None:
    past = env.witness.db_now() - timedelta(hours=1)
    assert (
        env.witness.write(
            update(OrganizationInvitation)
            .where(OrganizationInvitation.id == invitation_id)
            .values(expires_at=past)
        )
        == 1
    )


# --------------------------------------------------------------------------- #
class TestPreview:
    def test_preview_is_repeatable_and_consumes_nothing(self, env: Env):
        inv = _invite(env, "newbie@example.com", "reviewer")
        before = env.witness.snapshot()
        bodies = [_post(env, PREVIEW, {"token": inv["token"]}) for _ in range(3)]
        assert [b.status_code for b in bodies] == [200, 200, 200]
        assert bodies[0].json() == bodies[1].json() == bodies[2].json()
        assert env.witness.snapshot() == before
        body = bodies[0].json()
        assert set(body) == PREVIEW_FIELDS
        assert (
            body["organization_id"],
            body["organization_name"],
            body["email"],
            body["role"],
        ) == (
            ORG_A,
            "Acme A",
            "newbie@example.com",
            "reviewer",
        )
        assert _as_utc(body["expires_at"]) == _as_utc(
            env.witness.invitation(inv["id"])["expires_at"]
        )
        assert inv["token"] not in json.dumps(body)
        # Still usable after being previewed; only then is it spent.
        assert _post(env, REGISTER, _register_body(inv["token"])).status_code == 201
        assert _code(_post(env, PREVIEW, {"token": inv["token"]})) == (
            409,
            "invitation_already_used",
        )

    def test_preview_needs_no_authentication_and_ignores_one(self, env: Env):
        inv = _invite(env, "anon@example.com")
        assert _post(env, PREVIEW, {"token": inv["token"]}).status_code == 200
        assert _post(env, PREVIEW, {"token": inv["token"]}, user_id=BOB).status_code == 200


# --------------------------------------------------------------------------- #
class TestInvitedRegistration:
    def test_creates_user_and_membership_and_no_organization(self, env: Env):
        inv = _invite(env, "fresh@example.com", "marketer", inviter=A_ADMIN)
        orgs_before, users_before = env.witness.count(Organization), env.witness.count(User)
        r = _post(env, REGISTER, _register_body(inv["token"], "Fresh Person"))
        assert r.status_code == 201, r.text
        body = r.json()
        assert set(body) == SESSION_FIELDS
        user = env.witness.user_by_email("fresh@example.com")
        assert user is not None and user["full_name"] == "Fresh Person"
        assert body["user"] == {
            "id": user["id"],
            "email": "fresh@example.com",
            "full_name": "Fresh Person",
            "is_operator": False,
        }
        assert body["memberships"] == [
            {"organization_id": ORG_A, "organization_name": "Acme A", "role": "marketer"}
        ]
        assert (env.witness.count(Organization), env.witness.count(User)) == (
            orgs_before,
            users_before + 1,
        )
        assert {m for m in env.witness.memberships() if m[0] == user["id"]} == {
            (user["id"], ORG_A, "marketer")
        }
        row = env.witness.invitation(inv["id"])
        assert row["accepted_at"] is not None and row["accepted_by_user_id"] == user["id"]
        [audit] = env.witness.audit("organization_invitation.accepted")
        assert (
            audit["actor_user_id"],
            audit["entity_id"],
            audit["organization_id"],
            audit["workspace_id"],
        ) == (
            user["id"],
            inv["id"],
            ORG_A,
            None,
        )
        assert inv["token"] not in json.dumps(audit, default=str)
        # The session is real: the token authenticates and the password logs in.
        me = env.client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
        )
        assert me.status_code == 200 and me.json()["memberships"] == body["memberships"]
        login = _post(
            env, f"{API}/auth/login", {"email": "fresh@example.com", "password": PASSWORD}
        )
        assert login.status_code == 200

    @pytest.mark.parametrize(
        "patch",
        [
            {"full_name": ""},
            {"full_name": "x" * 201},
            {"password": "short7!"},
            {"password": "p" * 129},
        ],
        ids=["empty-name", "long-name", "short-password", "long-password"],
    )
    def test_registration_bounds_match_register(self, env: Env, patch: dict):
        inv = _invite(env, "bounds@example.com")
        before = env.witness.snapshot()
        r = _post(env, REGISTER, {**_register_body(inv["token"]), **patch})
        assert _code(r) == (422, "validation_error")
        assert env.witness.snapshot() == before

    def test_existing_account_must_sign_in_instead(self, env: Env):
        inv = _invite(env, _email(BOB), "viewer")
        before = env.witness.snapshot()
        r = _post(env, REGISTER, _register_body(inv["token"]))
        assert _code(r) == (409, "invitation_account_exists")
        assert env.witness.snapshot() == before
        # The invitation is intact: Bob accepts while signed in.
        assert _post(env, ACCEPT, {"token": inv["token"]}, user_id=BOB).status_code == 200


class TestOrdinaryRegisterIsUnchanged:
    def test_register_creates_user_organization_and_owner(self, env: Env):
        orgs_before = env.witness.count(Organization)
        r = _post(
            env,
            f"{API}/auth/register",
            {
                "email": "founder@example.com",
                "full_name": "Founder",
                "password": PASSWORD,
                "organization_name": "Founder Co",
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert set(body) == SESSION_FIELDS
        [membership] = body["memberships"]
        assert (membership["organization_name"], membership["role"]) == ("Founder Co", "owner")
        assert env.witness.count(Organization) == orgs_before + 1
        assert (
            body["user"]["id"],
            membership["organization_id"],
            "owner",
        ) in env.witness.memberships()
        assert len(env.witness.audit("auth.register")) == 1

    def test_register_ignores_a_pending_invitation_for_the_same_email(self, env: Env):
        inv = _invite(env, "both@example.com", "viewer")
        r = _post(
            env,
            f"{API}/auth/register",
            {
                "email": "both@example.com",
                "full_name": "Both",
                "password": PASSWORD,
                "organization_name": "Both Co",
            },
        )
        assert r.status_code == 201
        assert [m["role"] for m in r.json()["memberships"]] == ["owner"]
        assert env.witness.invitation(inv["id"])["accepted_at"] is None
        # Later, signed in, the same person accepts and holds both.
        acc = _post(env, ACCEPT, {"token": inv["token"]}, token=r.json()["access_token"])
        assert acc.status_code == 200
        assert sorted(m["role"] for m in acc.json()["memberships"]) == ["owner", "viewer"]


# --------------------------------------------------------------------------- #
class TestExistingUserAcceptance:
    def test_multi_organization_acceptance_keeps_the_same_user_and_every_membership(self, env: Env):
        inv = _invite(env, _email(ALICE), "marketer")
        users_before = env.witness.count(User)
        r = _post(env, ACCEPT, {"token": inv["token"]}, user_id=ALICE)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == SESSION_FIELDS and body["user"]["id"] == ALICE
        assert sorted((m["organization_id"], m["role"]) for m in body["memberships"]) == [
            (ORG_A, "marketer"),
            (ORG_B, "admin"),
        ]
        assert env.witness.count(User) == users_before
        assert {m for m in env.witness.memberships() if m[0] == ALICE} == {
            (ALICE, ORG_A, "marketer"),
            (ALICE, ORG_B, "admin"),
        }
        row = env.witness.invitation(inv["id"])
        assert row["accepted_by_user_id"] == ALICE and row["accepted_at"] is not None

    def test_wrong_signed_in_user_is_403_and_nothing_changes(self, env: Env):
        inv = _invite(env, _email(ALICE), "viewer")
        before = env.witness.snapshot()
        r = _post(env, ACCEPT, {"token": inv["token"]}, user_id=BOB)
        assert r.status_code == 403
        assert (r.json()["error"]["code"], r.json()["error"]["message"]) == (
            "permission_denied",
            WRONG_EMAIL,
        )
        assert env.witness.snapshot() == before
        assert (BOB, ORG_A) not in {(u, o) for u, o, _ in env.witness.memberships()}
        # Alice can still accept it.
        assert _post(env, ACCEPT, {"token": inv["token"]}, user_id=ALICE).status_code == 200

    @pytest.mark.parametrize(
        ("invited", "signed_in"),
        [("Casey@example.com", CASEY), ("dana@example.com", DANA)],
        ids=["invited-upper-local-part", "invited-lower-local-part"],
    )
    def test_local_part_case_is_a_different_address(self, env: Env, invited: str, signed_in: str):
        """EmailStr lowercases only the domain; the local part is compared exactly."""
        inv = _invite(env, invited, "viewer")
        assert inv["email"] == invited  # the premise: stored as written, case kept
        before = env.witness.snapshot()
        r = _post(env, ACCEPT, {"token": inv["token"]}, user_id=signed_in)
        assert r.status_code == 403
        assert (r.json()["error"]["code"], r.json()["error"]["message"]) == (
            "permission_denied",
            WRONG_EMAIL,
        )
        assert env.witness.snapshot() == before
        assert env.witness.invitation(inv["id"])["accepted_at"] is None
        assert not {(u, o) for u, o, _ in env.witness.memberships()} & {(signed_in, ORG_A)}

    def test_accept_requires_authentication(self, env: Env):
        inv = _invite(env, _email(ALICE))
        before = env.witness.snapshot()
        assert _post(env, ACCEPT, {"token": inv["token"]}).status_code == 401
        assert env.witness.snapshot() == before

    def test_already_member_is_409_and_the_invitation_is_not_consumed(self, env: Env):
        inv = _invite(env, _email(BOB), "viewer")
        # Bob becomes a member by another path between invitation and acceptance.
        with env.witness.engine.begin() as conn:
            conn.execute(
                OrganizationMember.__table__.insert().values(
                    id="m-bob-a", organization_id=ORG_A, user_id=BOB, role="reviewer"
                )
            )
        before = env.witness.snapshot()
        r = _post(env, ACCEPT, {"token": inv["token"]}, user_id=BOB)
        assert _code(r) == (409, "invitation_already_member")
        assert env.witness.snapshot() == before
        assert env.witness.invitation(inv["id"])["accepted_at"] is None

    def test_acceptance_is_recorded_in_exactly_one_secret_free_audit_row(self, env: Env):
        """The existing-user path audits its acceptance -- on SQLite, not only under PostgreSQL."""
        inv = _invite(env, _email(BOB), "reviewer")
        token_hash = env.witness.invitation(inv["id"])["token_hash"]
        r = _post(env, ACCEPT, {"token": inv["token"]}, user_id=BOB)
        assert r.status_code == 200, r.text
        [audit] = env.witness.audit("organization_invitation.accepted")
        assert (
            audit["actor_user_id"],
            audit["entity_type"],
            audit["entity_id"],
            audit["organization_id"],
            audit["workspace_id"],
            audit["previous_state"],
            audit["reason"],
        ) == (BOB, "organization_invitation", inv["id"], ORG_A, None, None, None)
        assert audit["new_state"] == {
            "email": _email(BOB),
            "role": "reviewer",
            "accepted_by_user_id": BOB,
        }
        row = json.dumps(audit, default=str)
        assert inv["token"] not in row and token_hash not in row and "token" not in row


class TestDisclosedCaseVariantAccounts:
    """DISCLOSED PRODUCT RISK (6B-3A finding F2) -- pinned so it cannot change silently,
    NOT endorsed.

    ``users.email`` is unique but case-sensitive, and addresses are matched exactly (the
    contract forbids ``lower()``/citext). An address differing from an existing account
    only in local-part case is therefore a *different* address: registering through an
    invitation issued to it creates a second account. This is the pre-existing behaviour
    of ``POST /auth/register`` too, pinned alongside. Changing it is a product decision
    outside 6B-3A.
    """

    def _case_variants(self, env: Env) -> list[str]:
        with env.witness.session() as s:
            rows = s.scalars(select(User.email).where(func.lower(User.email) == CASEY_EMAIL))
            return sorted(rows)

    def test_invited_registration_creates_a_case_variant_second_account(self, env: Env):
        inv = _invite(env, "Casey@example.com", "viewer")  # casey@example.com already exists
        r = _post(env, REGISTER, _register_body(inv["token"]))
        assert r.status_code == 201, r.text
        assert r.json()["user"]["email"] == "Casey@example.com"
        assert self._case_variants(env) == ["Casey@example.com", CASEY_EMAIL]

    def test_ordinary_registration_behaves_the_same(self, env: Env):
        r = _post(
            env,
            f"{API}/auth/register",
            {
                "email": "Casey@example.com",
                "full_name": "Casey Two",
                "password": PASSWORD,
                "organization_name": "Casey Two Co",
            },
        )
        assert r.status_code == 201, r.text
        assert self._case_variants(env) == ["Casey@example.com", CASEY_EMAIL]


# --------------------------------------------------------------------------- #
class TestOneTime:
    def test_replay_after_existing_user_acceptance(self, env: Env):
        inv = _invite(env, _email(ALICE))
        assert _post(env, ACCEPT, {"token": inv["token"]}, user_id=ALICE).status_code == 200
        before = env.witness.snapshot()
        for r in (
            _post(env, ACCEPT, {"token": inv["token"]}, user_id=ALICE),
            _post(env, REGISTER, _register_body(inv["token"])),
            _post(env, PREVIEW, {"token": inv["token"]}),
        ):
            assert _code(r) == (409, "invitation_already_used")
        assert env.witness.snapshot() == before
        assert sum(1 for u, o, _ in env.witness.memberships() if (u, o) == (ALICE, ORG_A)) == 1

    def test_replay_after_invited_registration(self, env: Env):
        inv = _invite(env, "once@example.com")
        assert _post(env, REGISTER, _register_body(inv["token"])).status_code == 201
        users = env.witness.count(User)
        assert _code(_post(env, REGISTER, _register_body(inv["token"], "Again"))) == (
            409,
            "invitation_already_used",
        )
        assert env.witness.count(User) == users


def _dead_token_cases():
    return ["revoked", "expired", "unknown", "unknown-at-max-length", "overlong"]


class TestDeadTokens:
    """Each dead state, on each of the three endpoints, independently, with no side effect."""

    def _make(self, env: Env, state: str) -> tuple[str, tuple[int, str]]:
        if state == "unknown":
            return "A" * 43, (404, "invitation_invalid")
        if state == "unknown-at-max-length":
            return "A" * MAX_TOKEN_LENGTH, (404, "invitation_invalid")
        if state == "overlong":  # rejected by the request schema before any lookup
            return "A" * (MAX_TOKEN_LENGTH + 1), (422, "validation_error")
        inv = _invite(env, f"{state}@example.com")
        if state == "revoked":
            r = env.client.delete(
                f"{API}/organizations/{ORG_A}/invitations/{inv['id']}",
                headers={"Authorization": f"Bearer {create_access_token(A_OWNER)}"},
            )
            assert r.status_code == 204
            return inv["token"], (409, "invitation_revoked")
        _expire(env, inv["id"])
        return inv["token"], (409, "invitation_expired")

    @pytest.mark.parametrize("state", _dead_token_cases())
    @pytest.mark.parametrize("endpoint", ["preview", "register", "accept"])
    def test_dead_token_is_refused_with_a_stable_code(self, env: Env, state: str, endpoint: str):
        token, expected = self._make(env, state)
        before = env.witness.snapshot()
        if endpoint == "preview":
            r = _post(env, PREVIEW, {"token": token})
        elif endpoint == "register":
            r = _post(env, REGISTER, _register_body(token))
        else:
            r = _post(env, ACCEPT, {"token": token}, user_id=BOB)
        assert _code(r) == expected
        assert env.witness.snapshot() == before

    def test_revoked_outranks_expired_and_used_outranks_expired(self, env: Env):
        revoked = _invite(env, "r-and-e@example.com")
        env.client.delete(
            f"{API}/organizations/{ORG_A}/invitations/{revoked['id']}",
            headers={"Authorization": f"Bearer {create_access_token(A_OWNER)}"},
        )
        _expire(env, revoked["id"])
        used = _invite(env, "u-and-e@example.com")
        assert _post(env, REGISTER, _register_body(used["token"])).status_code == 201
        _expire(env, used["id"])
        assert _code(_post(env, PREVIEW, {"token": revoked["token"]})) == (
            409,
            "invitation_revoked",
        )
        assert _code(_post(env, PREVIEW, {"token": used["token"]})) == (
            409,
            "invitation_already_used",
        )


# --------------------------------------------------------------------------- #
class TestInviterReauthorization:
    def _assert_blocked(self, env: Env, inv: dict, *, email_user: str | None = None) -> None:
        before = env.witness.snapshot()
        if email_user is None:
            r = _post(env, REGISTER, _register_body(inv["token"]))
        else:
            r = _post(env, ACCEPT, {"token": inv["token"]}, user_id=email_user)
        assert _code(r) == (409, "invitation_inviter_not_authorized")
        assert env.witness.snapshot() == before
        assert env.witness.invitation(inv["id"])["accepted_at"] is None

    @pytest.mark.parametrize("path", ["register", "accept"])
    def test_inviter_removed(self, env: Env, path: str):
        inv = _invite(
            env, "new@example.com" if path == "register" else _email(BOB), inviter=A_ADMIN
        )
        env.witness.write(
            delete(OrganizationMember).where(
                OrganizationMember.user_id == A_ADMIN, OrganizationMember.organization_id == ORG_A
            )
        )
        self._assert_blocked(env, inv, email_user=None if path == "register" else BOB)

    @pytest.mark.parametrize(
        "demoted_to", ["marketer", "reviewer", "compliance_reviewer", "viewer"]
    )
    def test_inviter_demoted_below_admin(self, env: Env, demoted_to: str):
        inv = _invite(env, "demoted@example.com", "viewer", inviter=A_ADMIN)
        env.witness.write(
            update(OrganizationMember)
            .where(
                OrganizationMember.user_id == A_ADMIN, OrganizationMember.organization_id == ORG_A
            )
            .values(role=demoted_to)
        )
        self._assert_blocked(env, inv)

    def test_inviter_account_deleted(self, env: Env):
        inv = _invite(env, "orphan@example.com", inviter=A_ADMIN)
        env.witness.write(delete(User).where(User.id == A_ADMIN))
        assert env.witness.invitation(inv["id"])["invited_by_user_id"] is None  # SET NULL
        self._assert_blocked(env, inv)

    def test_inviter_deactivated(self, env: Env):
        inv = _invite(env, "inactive@example.com", inviter=A_ADMIN)
        env.witness.write(update(User).where(User.id == A_ADMIN).values(is_active=False))
        self._assert_blocked(env, inv)

    def test_inviter_owner_demoted_to_admin_can_still_grant_admin(self, env: Env):
        """Positive control: re-authorization checks current authority, not the issuing role."""
        inv = _invite(env, "still-ok@example.com", "admin", inviter=A_OWNER)
        env.witness.write(
            update(OrganizationMember)
            .where(OrganizationMember.user_id == A_OWNER)
            .values(role="admin")
        )
        assert _post(env, REGISTER, _register_body(inv["token"])).status_code == 201

    def test_preview_does_not_reauthorize(self, env: Env):
        inv = _invite(env, "peek@example.com", inviter=A_ADMIN)
        env.witness.write(delete(OrganizationMember).where(OrganizationMember.user_id == A_ADMIN))
        assert _post(env, PREVIEW, {"token": inv["token"]}).status_code == 200

    @pytest.mark.parametrize("change", ["removed", "demoted"])
    @pytest.mark.parametrize("path", ["register", "accept"])
    def test_authority_in_another_organization_does_not_count(
        self, env: Env, path: str, change: str
    ):
        """The inviter's authority is re-checked in the INVITATION's organization only.

        The inviter loses OWNER/ADMIN in organization A -- removed, or demoted to VIEWER,
        which still ranks at or above the VIEWER invitation, so only the OWNER/ADMIN
        requirement can refuse it -- while being OWNER of organization B. Authority read
        from any organization would let the invitation through.
        """
        inv = _invite(
            env,
            "elsewhere@example.com" if path == "register" else _email(BOB),
            "viewer",
            inviter=A_ADMIN,
        )
        with env.witness.engine.begin() as conn:
            conn.execute(
                OrganizationMember.__table__.insert().values(
                    id="m-admin-owns-b", organization_id=ORG_B, user_id=A_ADMIN, role="owner"
                )
            )
        in_a = (OrganizationMember.user_id == A_ADMIN, OrganizationMember.organization_id == ORG_A)
        if change == "removed":
            env.witness.write(delete(OrganizationMember).where(*in_a))
        else:
            env.witness.write(update(OrganizationMember).where(*in_a).values(role="viewer"))
        # The premise: authority elsewhere, none here.
        held = {(o, r) for u, o, r in env.witness.memberships() if u == A_ADMIN}
        assert held == (
            {(ORG_B, "owner")} if change == "removed" else {(ORG_B, "owner"), (ORG_A, "viewer")}
        )
        self._assert_blocked(env, inv, email_user=None if path == "register" else BOB)


# --------------------------------------------------------------------------- #
class TestPlantedOwnerInvitation:
    """An OWNER-role row can only exist by a direct database write (the CHECK admits all six
    roles; the schema and the service refuse OWNER). Accepting one must never grant OWNER.

    Only acceptance is asserted. How preview and the administrators' list render such a row
    is deliberately NOT pinned: the response schemas carry the five invitable roles only, so
    OpenAPI never advertises ``owner``, and a planted row is the accepted, disclosed 6B-3A
    residual R2 (reachable by a direct database write, never through the API).
    """

    RAW = "planted-owner-" + "t" * 29

    def _plant(self, env: Env, email: str, token: str) -> str:
        now = env.witness.db_now()
        invitation_id = f"planted-{uuid.uuid4().hex[:8]}"
        with env.witness.engine.begin() as conn:
            conn.execute(
                OrganizationInvitation.__table__.insert().values(
                    id=invitation_id,
                    organization_id=ORG_A,
                    email=email,
                    role="owner",
                    token_hash=hash_invitation_token(token),
                    invited_by_user_id=A_OWNER,  # even an OWNER inviter cannot confer OWNER
                    expires_at=now + timedelta(hours=1),
                    created_at=now,
                    updated_at=now,
                )
            )
        return invitation_id

    def _owners(self, env: Env) -> set[str]:
        return {u for u, o, r in env.witness.memberships() if o == ORG_A and r == "owner"}

    def test_existing_user_cannot_accept_owner(self, env: Env):
        invitation_id = self._plant(env, _email(BOB), self.RAW)
        before = env.witness.snapshot()
        r = _post(env, ACCEPT, {"token": self.RAW}, user_id=BOB)
        assert _code(r) == (409, "invitation_inviter_not_authorized")
        assert env.witness.snapshot() == before
        assert self._owners(env) == {A_OWNER}
        assert env.witness.invitation(invitation_id)["accepted_at"] is None

    def test_new_user_cannot_register_into_owner(self, env: Env):
        invitation_id = self._plant(env, "owner-grab@example.com", self.RAW)
        before = env.witness.snapshot()
        r = _post(env, REGISTER, _register_body(self.RAW))
        assert _code(r) == (409, "invitation_inviter_not_authorized")
        assert env.witness.snapshot() == before
        assert env.witness.user_by_email("owner-grab@example.com") is None
        assert self._owners(env) == {A_OWNER}
        assert env.witness.invitation(invitation_id)["accepted_at"] is None


def _competitor(kind: str) -> Callable[[Env, str], None]:
    """A change committed on a SEPARATE connection, mid-request (see ``TestClaimIsConditional``)."""

    def act(env: Env, invitation_id: str) -> None:
        now = env.witness.db_now()
        values = {
            "revoke": {"revoked_at": now},
            # Accepted elsewhere, then that member removed: the acceptor holds no membership,
            # so only the claim's ``accepted_at IS NULL`` can stop this request.
            "accept-then-remove": {"accepted_at": now, "accepted_by_user_id": None},
            "expire": {"expires_at": now - timedelta(hours=1)},
            "none": None,
        }[kind]
        if values is not None:
            assert (
                env.witness.write(
                    update(OrganizationInvitation)
                    .where(OrganizationInvitation.id == invitation_id)
                    .values(**values)
                )
                == 1
            )

    return act


class TestClaimIsConditional:
    """G1: the claim's WHERE predicates and rowcount check are what stop a concurrent change.

    A sequential replay never reaches the claim -- token resolution refuses it first -- so it
    cannot tell a conditional claim from an unconditional one. Here the request resolves a
    PENDING token; then, at the first statement after the token lookup (the organization
    lock), a competing change is committed on a separate connection; then the request goes
    on to its claim. Only ``UPDATE ... WHERE accepted_at IS NULL AND revoked_at IS NULL AND
    expires_at > now`` with a rowcount check can refuse it now. SQLite (pysqlite holds no
    lock for a SELECT) and PostgreSQL (READ COMMITTED; the competitor does not touch the
    organization row) both let the competitor commit in that window.
    """

    CASES = {
        "revoke": (409, "invitation_revoked"),
        "accept-then-remove": (409, "invitation_already_used"),
        "expire": (409, "invitation_expired"),
    }

    def _hooked(self, env: Env, invitation_id: str, competitor) -> tuple[list[str], dict, Callable]:
        fired: list[str] = []
        after: dict = {}
        armed = {"token_lookup_seen": False}

        def hook(conn, cursor, statement, parameters, context, executemany):
            if fired:
                return
            if statement.lstrip().upper().startswith("SELECT") and (
                "organization_invitations.token_hash" in statement
            ):
                armed["token_lookup_seen"] = True
                return
            if armed["token_lookup_seen"]:
                fired.append(statement)
                competitor(env, invitation_id)
                after.update(env.witness.invitation(invitation_id))

        event.listen(env.request_engine, "before_cursor_execute", hook)
        return fired, after, hook

    @pytest.mark.parametrize("kind", ["revoke", "accept-then-remove", "expire", "none"])
    @pytest.mark.parametrize("path", ["accept", "register"])
    def test_competitor_between_resolution_and_claim(self, backend_env: Env, path: str, kind: str):
        env = backend_env
        email = _email(BOB) if path == "accept" else "racer@example.com"
        inv = _invite(env, email, "reviewer")
        fired, after, hook = self._hooked(env, inv["id"], _competitor(kind))
        try:
            if path == "accept":
                r = _post(env, ACCEPT, {"token": inv["token"]}, user_id=BOB)
            else:
                r = _post(env, REGISTER, _register_body(inv["token"]))
        finally:
            event.remove(env.request_engine, "before_cursor_execute", hook)
        # The window really opened, and exactly where intended: at the organization lock.
        assert len(fired) == 1, fired
        assert "FROM organizations" in fired[0] and "organization_members" not in fired[0]
        acceptor = BOB if path == "accept" else None
        if kind == "none":  # positive control: the hook itself breaks nothing
            assert r.status_code == (200 if path == "accept" else 201), r.text
            return
        assert _code(r) == self.CASES[kind]
        # Nothing the request did survived: the row is exactly as the competitor left it,
        # and no membership, account or acceptance audit was written.
        assert env.witness.invitation(inv["id"]) == after
        assert not [m for m in env.witness.memberships() if m[1] == ORG_A and m[0] == acceptor]
        if path == "register":
            assert env.witness.user_by_email(email) is None
        assert env.witness.audit("organization_invitation.accepted") == []


# --------------------------------------------------------------------------- #
class TestCallerSuppliesOnlyTheToken:
    @pytest.mark.parametrize("extra", ["organization_id", "role", "email", "is_operator"])
    @pytest.mark.parametrize("endpoint", ["preview", "register", "accept"])
    def test_extra_fields_are_rejected(self, env: Env, endpoint: str, extra: str):
        inv = _invite(env, _email(BOB) if endpoint == "accept" else "strict@example.com")
        value = {
            "organization_id": ORG_B,
            "role": "owner",
            "email": "other@example.com",
            "is_operator": True,
        }[extra]
        before = env.witness.snapshot()
        if endpoint == "preview":
            r = _post(env, PREVIEW, {"token": inv["token"], extra: value})
        elif endpoint == "register":
            r = _post(env, REGISTER, {**_register_body(inv["token"]), extra: value})
        else:
            r = _post(env, ACCEPT, {"token": inv["token"], extra: value}, user_id=BOB)
        assert _code(r) == (422, "validation_error")
        assert env.witness.snapshot() == before

    def test_no_token_in_any_path_or_query_parameter(self):
        doc = app.openapi()
        for path, item in doc["paths"].items():
            for op in item.values():
                for param in op.get("parameters", []):
                    assert "token" not in param["name"].lower(), (path, param["name"])
        for path in (PREVIEW, REGISTER, ACCEPT):
            schema_ref = doc["paths"][path]["post"]["requestBody"]["content"]["application/json"][
                "schema"
            ]["$ref"]
            schema = doc["components"]["schemas"][schema_ref.split("/")[-1]]
            assert "token" in schema["required"]
            assert schema.get("additionalProperties") is False
            assert not {"organization_id", "role", "email", "is_operator"} & set(
                schema["properties"]
            )


# --------------------------------------------------------------------------- #
class TestExplicitCommit:
    def test_register_is_committed_before_the_response(self, withheld_env: Env):
        inv = _invite(withheld_env, "c1@example.com")
        assert _post(withheld_env, REGISTER, _register_body(inv["token"])).status_code == 201
        assert withheld_env.witness.user_by_email("c1@example.com") is not None
        assert withheld_env.witness.invitation(inv["id"])["accepted_at"] is not None

    def test_accept_is_committed_before_the_response(self, withheld_env: Env):
        inv = _invite(withheld_env, _email(BOB))
        assert _post(withheld_env, ACCEPT, {"token": inv["token"]}, user_id=BOB).status_code == 200
        assert (BOB, ORG_A, "marketer") in withheld_env.witness.memberships()


# --------------------------------------------------------------------------- #
def test_no_raw_token_in_any_log_record(env: Env, caplog):
    caplog.set_level(logging.DEBUG)
    new = _invite(env, "log-new@example.com")
    old = _invite(env, _email(BOB))
    dead = _invite(env, "log-dead@example.com")
    _expire(env, dead["id"])
    _post(env, PREVIEW, {"token": new["token"]})
    _post(env, REGISTER, _register_body(new["token"]))
    _post(env, ACCEPT, {"token": old["token"]}, user_id=BOB)
    _post(env, PREVIEW, {"token": dead["token"]})  # a refusal path too
    assert any(rec.name == "signalnest.request" for rec in caplog.records), "capture saw no app log"
    tokens = [new["token"], old["token"], dead["token"]]
    for rec in caplog.records:
        rendered = rec.getMessage() + json.dumps(rec.__dict__, default=str)
        assert not any(t in rendered for t in tokens), rec.name
