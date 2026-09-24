"""6B-3A: organization invitations -- create, list, revoke (P6-AUTH-1).

    POST   /api/v1/organizations/{organization_id}/invitations
    GET    /api/v1/organizations/{organization_id}/invitations
    DELETE /api/v1/organizations/{organization_id}/invitations/{invitation_id}

All three are gated by ``require_exact_organization_roles(OWNER, ADMIN)``. Acceptance
(preview, invited registration, authenticated acceptance) has its own module; the six-role
matrix over every route is ``test_e7_role_policy_matrix``.

What runs for real. Only ``get_db`` is overridden, and every request helper asserts it is
the only override, so bearer parsing, membership and the exact-role gate execute. The
database is a temporary SQLite file with foreign keys on; committed state is read through a
*separate* ``Engine`` (the witness), never the request's session or pool.

Load-bearing properties, each with its own test:

* **OWNER is never an invitation role -- at the contract and at the service.** The request
  schema's ``role`` enum does not contain ``owner`` (OpenAPI and a 422), and the service
  refuses ``Role.OWNER`` with ``invitation_owner_forbidden`` even when called directly with
  an OWNER's context, so bypassing the schema changes nothing.
* **The token is a secret shown once.** The raw token appears in the 201 response and
  nowhere else: not in the list, not in any audit row, not in any captured log record, and
  not anywhere in the SQLite file's bytes. Only ``token_hash`` is stored: exactly
  ``sha256(token)``, 64 lowercase hex characters. The digest is not in any audit row either.
* **Expiry is measured on the database clock.** ``expires_at`` lies one configured lifetime
  (default 72 hours, owner decision D2) after the database's own time read immediately
  before and after the request, within a small tolerance; ``invitation_expire_hours``
  overrides it, and the override leaves the default untouched.
* **Pending uniqueness per (organization, email).** A second pending invitation is a 409
  ``invitation_pending_exists``; an *expired* one is revoked (and audited) and replaced with
  a new token, so the old link then reports ``invitation_revoked``.
* **Explicit commit before success (§39).** Production ``get_db`` commits only after the
  handler, and on a real server after the response is sent. A variant of the override that
  never commits at teardown shows that each successful mutation is already committed when
  the response is returned -- and, as a positive control, that a route which does *not*
  commit explicitly (workspace creation) leaves nothing behind under the same variant.
* **Per-instance error codes reach the envelope.** ``SignalNestError(message, code=...)``
  is used for the first time in production; every refusal here is asserted by its code in
  ``{"error": {"code", "message", "request_id"}}``, and the class-level defaults are shown
  untouched afterwards.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.auth.dependencies import OrganizationContext
from app.core.config import Settings, get_settings
from app.core.enums import Role
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, SignalNestError
from app.core.middleware import RateLimitMiddleware
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import SessionLocal, get_db
from app.main import app
from app.organizations import invitations as invitation_service
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
    Workspace,
)

API = get_settings().api_prefix

ORG_A, ORG_B = "org-inv-a", "org-inv-b"
WS_A = "ws-inv-a"
OUTSIDER = "inv-outsider"
ALL_ROLES = (
    Role.OWNER,
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
)
ADMINS = (Role.OWNER, Role.ADMIN)
NON_ADMINS = (Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER, Role.VIEWER)
INVITABLE = (Role.ADMIN, Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER, Role.VIEWER)

INVITATION_OUT_FIELDS = {"id", "email", "role", "expires_at", "created_at", "invited_by_user_id"}
NOT_A_MEMBER = "You are not a member of this organization."
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # secrets.token_urlsafe(32)
# SQLite's clock resolution here is 1 ms and the request takes well under a second; two
# seconds of tolerance keeps the bound meaningful against a lifetime of hours.
CLOCK_TOLERANCE = timedelta(seconds=2)
#: Owner decision D2: the default invitation lifetime. The one place this module states it.
DEFAULT_EXPIRE_HOURS = 72


def _role_denial(role: Role) -> str:
    return f"Role '{role.value}' is not permitted for this action."


_ID_MAX = User.__table__.c.id.type.length


def _db_id(value: str) -> str:
    if len(value) > _ID_MAX:
        raise AssertionError(f"fixture id {value!r} exceeds String({_ID_MAX})")
    return value


def _uid(org: str, role: Role) -> str:
    return _db_id(f"{org}-{role.value[:3]}")


def _email(user_id: str) -> str:
    return f"{user_id}@example.com"


def _bearer(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
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
    for org in (ORG_A, ORG_B):
        s.add(Organization(id=org, name=f"Org {org}", slug=org))
    s.flush()
    s.add(Workspace(id=WS_A, organization_id=ORG_A, name="WS", slug="ws"))
    users = [_uid(ORG_A, r) for r in ALL_ROLES] + [_uid(ORG_B, Role.OWNER), OUTSIDER]
    for user_id in users:
        s.add(
            User(
                id=_db_id(user_id),
                email=_email(user_id),
                full_name=user_id,
                hashed_password="x",
                is_active=True,
            )
        )
    s.flush()
    for role in ALL_ROLES:
        s.add(OrganizationMember(organization_id=ORG_A, user_id=_uid(ORG_A, role), role=role.value))
    s.add(OrganizationMember(organization_id=ORG_B, user_id=_uid(ORG_B, Role.OWNER), role="owner"))
    s.commit()


@dataclass(frozen=True)
class Witness:
    """Committed-state reader on its OWN ``Engine`` -- never the request's session or pool."""

    engine: Engine

    def session(self):
        return sessionmaker(bind=self.engine, autoflush=False, future=True)()

    def db_now(self) -> datetime:
        """The database clock, read the same way the service reads it on SQLite."""
        with self.engine.connect() as conn:
            value = conn.execute(select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))).scalar()
        return _as_utc(value)

    def invitations(self, org: str | None = None) -> list[dict]:
        stmt = select(OrganizationInvitation).order_by(OrganizationInvitation.created_at)
        if org is not None:
            stmt = stmt.where(OrganizationInvitation.organization_id == org)
        with self.session() as s:
            return [
                {c.key: getattr(row, c.key) for c in OrganizationInvitation.__table__.c}
                for row in s.scalars(stmt)
            ]

    def invitation(self, invitation_id: str) -> dict:
        rows = [r for r in self.invitations() if r["id"] == invitation_id]
        assert len(rows) == 1, invitation_id
        return rows[0]

    def pending(self, org: str) -> list[dict]:
        now = self.db_now()
        return [
            r
            for r in self.invitations(org)
            if r["accepted_at"] is None
            and r["revoked_at"] is None
            and _as_utc(r["expires_at"]) > now
        ]

    def audit(self, action: str | None = None) -> list[dict]:
        stmt = select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        with self.session() as s:
            return [
                {c.key: getattr(row, c.key) for c in AuditLog.__table__.c}
                for row in s.scalars(stmt)
            ]

    def count(self, model) -> int:
        with self.session() as s:
            return s.scalar(select(func.count()).select_from(model))

    def write(self, stmt) -> int:
        """Commit one statement on the witness engine; return its rowcount."""
        with self.engine.begin() as conn:
            return conn.execute(stmt).rowcount


@dataclass(frozen=True)
class Env:
    client: TestClient
    witness: Witness
    request_factory: sessionmaker
    db_file: Path


@contextmanager
def _environment(db_file: Path, *, teardown_commit: bool = True) -> Iterator[Env]:
    """``teardown_commit=False`` withholds production ``get_db``'s post-handler commit.

    Under it, anything the witness sees after a response was committed by the route itself
    before it returned -- which is what §39 requires of every successful mutation.
    """
    request_engine = _sqlite_file_engine(db_file)
    witness_engine = _sqlite_file_engine(db_file)
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
        yield Env(TestClient(app), Witness(witness_engine), factory, db_file)
    finally:
        app.dependency_overrides.clear()
        _active_rate_limiter()._hits.clear()
        request_engine.dispose()
        witness_engine.dispose()


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with _environment(tmp_path / "invitations.db") as e:
        yield e


@pytest.fixture
def withheld_env(tmp_path) -> Iterator[Env]:
    with _environment(tmp_path / "invitations-withheld.db", teardown_commit=False) as e:
        yield e


def _send(env: Env, method: str, url: str, user_id: str | None, body: object = None):
    assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
    _active_rate_limiter()._hits.clear()
    headers = _bearer(user_id) if user_id else {}
    if body is None:
        return env.client.request(method, url, headers=headers)
    return env.client.request(method, url, headers=headers, json=body)


def _invitations_url(org: str = ORG_A) -> str:
    return f"{API}/organizations/{org}/invitations"


def _create(env: Env, email: str, role: str = "viewer", *, actor: str | None = None, org=ORG_A):
    return _send(
        env,
        "POST",
        _invitations_url(org),
        actor or _uid(org, Role.OWNER),
        {"email": email, "role": role},
    )


def _error(r) -> tuple[int, str, str]:
    body = r.json()
    assert set(body) == {"error"} and {"code", "message", "request_id"} <= set(body["error"])
    return r.status_code, body["error"]["code"], body["error"]["message"]


def _expire(env: Env, invitation_id: str) -> None:
    """Move ``expires_at`` one hour behind the DATABASE clock, committed on the witness."""
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
class TestFixtureIntegrity:
    def test_seeded_memberships(self, env: Env):
        with env.witness.session() as s:
            rows = set(
                s.execute(
                    select(
                        OrganizationMember.user_id,
                        OrganizationMember.organization_id,
                        OrganizationMember.role,
                    )
                )
            )
        assert rows == {(_uid(ORG_A, r), ORG_A, r.value) for r in ALL_ROLES} | {
            (_uid(ORG_B, Role.OWNER), ORG_B, "owner")
        }
        assert env.witness.invitations() == [] and env.witness.audit() == []

    def test_witness_is_a_separate_engine_and_sees_only_committed_rows(self, env: Env):
        s = env.request_factory()
        try:
            s.add(
                OrganizationInvitation(
                    organization_id=ORG_A,
                    email="uncommitted@example.com",
                    role="viewer",
                    token_hash="0" * 64,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            s.flush()
            assert env.witness.invitations() == []
            s.commit()
            assert [r["email"] for r in env.witness.invitations()] == ["uncommitted@example.com"]
        finally:
            s.close()

    def test_db_now_reads_the_database_clock(self, env: Env):
        with env.request_factory() as s:
            service_now = invitation_service.database_now(s)
        witness_now = env.witness.db_now()
        assert abs(witness_now - service_now) < CLOCK_TOLERANCE
        assert witness_now.tzinfo is UTC


# --------------------------------------------------------------------------- #
class TestCreate:
    @pytest.mark.parametrize("actor", ADMINS)
    @pytest.mark.parametrize("role", INVITABLE)
    def test_owner_and_admin_invite_every_invitable_role(self, env: Env, actor: Role, role: Role):
        email = f"new-{role.value.replace('_', '-')}@example.com"
        r = _create(env, email, role.value, actor=_uid(ORG_A, actor))
        assert r.status_code == 201, r.text
        body = r.json()
        assert set(body) == INVITATION_OUT_FIELDS | {"token"}
        assert (body["email"], body["role"], body["invited_by_user_id"]) == (
            email,
            role.value,
            _uid(ORG_A, actor),
        )
        [row] = env.witness.invitations(ORG_A)
        assert (row["id"], row["email"], row["role"], row["invited_by_user_id"]) == (
            body["id"],
            email,
            role.value,
            _uid(ORG_A, actor),
        )
        assert row["accepted_at"] is None and row["revoked_at"] is None
        assert row["accepted_by_user_id"] is None

    def test_token_is_shown_once_and_only_its_digest_is_stored(self, env: Env):
        r = _create(env, "secret@example.com", "marketer")
        assert r.status_code == 201
        token = r.json()["token"]
        # secrets.token_urlsafe(32): 32 random bytes, 43 URL-safe characters, no padding.
        assert len(token) == 43
        assert TOKEN_SHAPE.match(token), token
        [row] = env.witness.invitations(ORG_A)
        assert row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert HEX64.match(row["token_hash"])
        assert row["token_hash"] == invitation_service.hash_invitation_token(token)
        # Nowhere in the database file -- any table, any page.
        assert token.encode("utf-8") not in env.db_file.read_bytes()
        # Not in the list, by key or by value.
        listing = _send(env, "GET", _invitations_url(), _uid(ORG_A, Role.OWNER))
        assert listing.status_code == 200
        assert token not in listing.text and row["token_hash"] not in listing.text
        assert all(set(item) == INVITATION_OUT_FIELDS for item in listing.json())

    def test_two_invitations_get_distinct_tokens(self, env: Env):
        a = _create(env, "one@example.com").json()["token"]
        b = _create(env, "two@example.com").json()["token"]
        assert a != b
        hashes = {row["token_hash"] for row in env.witness.invitations()}
        assert len(hashes) == 2

    def test_created_audit_row_is_exact_and_secret_free(self, env: Env):
        r = _create(env, "audited@example.com", "reviewer", actor=_uid(ORG_A, Role.ADMIN))
        token = r.json()["token"]
        [row] = env.witness.invitations(ORG_A)
        [audit] = env.witness.audit()
        assert (
            audit["action"],
            audit["organization_id"],
            audit["workspace_id"],
            audit["actor_user_id"],
            audit["entity_type"],
            audit["entity_id"],
            audit["previous_state"],
        ) == (
            "organization_invitation.created",
            ORG_A,
            None,
            _uid(ORG_A, Role.ADMIN),
            "organization_invitation",
            row["id"],
            None,
        )
        assert set(audit["new_state"]) == {"email", "role", "expires_at"}
        assert (audit["new_state"]["email"], audit["new_state"]["role"]) == (
            "audited@example.com",
            "reviewer",
        )
        assert _as_utc(audit["new_state"]["expires_at"]) == _as_utc(row["expires_at"])
        dumped = json.dumps(audit, default=str)
        assert token not in dumped and row["token_hash"] not in dumped

    @pytest.mark.parametrize("role", NON_ADMINS)
    def test_non_admin_roles_are_refused_and_nothing_is_written(self, env: Env, role: Role):
        r = _create(env, "nope@example.com", "viewer", actor=_uid(ORG_A, role))
        assert _error(r) == (403, "permission_denied", _role_denial(role))
        assert env.witness.invitations() == [] and env.witness.audit() == []

    @pytest.mark.parametrize(
        ("actor", "org"),
        [(OUTSIDER, ORG_A), (_uid(ORG_B, Role.OWNER), ORG_A), (_uid(ORG_A, Role.OWNER), ORG_B)],
        ids=["outsider", "owner-of-b-in-a", "owner-of-a-in-b"],
    )
    def test_non_members_are_refused_by_membership(self, env: Env, actor: str, org: str):
        r = _create(env, "x@example.com", "viewer", actor=actor, org=org)
        assert _error(r) == (403, "permission_denied", NOT_A_MEMBER)
        assert env.witness.invitations() == []

    @pytest.mark.parametrize(
        "extra", ["organization_id", "invited_by_user_id", "token", "token_hash"]
    )
    def test_body_cannot_supply_identity_or_token(self, env: Env, extra: str):
        r = _send(
            env,
            "POST",
            _invitations_url(),
            _uid(ORG_A, Role.OWNER),
            {"email": "x@example.com", "role": "viewer", extra: ORG_B},
        )
        assert _error(r)[:2] == (422, "validation_error")
        assert env.witness.invitations() == []

    def test_anonymous_is_401(self, env: Env):
        r = env.client.post(_invitations_url(), json={"email": "x@example.com", "role": "viewer"})
        assert r.status_code == 401
        assert env.witness.invitations() == []


class TestOwnerIsNeverInvitable:
    def test_schema_rejects_owner_before_the_service(self, env: Env):
        r = _create(env, "owner-wannabe@example.com", "owner")
        status, code, _ = _error(r)
        assert (status, code) == (422, "validation_error")
        assert r.json()["error"]["details"][0]["loc"] == ["body", "role"]
        assert env.witness.invitations() == [] and env.witness.audit() == []

    @pytest.mark.parametrize("role", ["superuser", "OWNER", "Owner", "", "member"])
    def test_schema_rejects_anything_outside_the_five(self, env: Env, role: str):
        assert _create(env, "x@example.com", role).status_code == 422
        assert env.witness.invitations() == []

    @pytest.mark.parametrize(
        "schema_name",
        ["InvitationCreate", "InvitationOut", "InvitationCreatedOut", "InvitationPreviewOut"],
    )
    def test_openapi_never_offers_owner_as_an_invitation_role(self, schema_name: str):
        """Request and response alike carry the five invitable roles; ``owner`` in none."""
        schemas = app.openapi()["components"]["schemas"]
        role_schema = schemas[schema_name]["properties"]["role"]
        enum = role_schema.get("enum") or schemas[role_schema["$ref"].split("/")[-1]]["enum"]
        assert sorted(enum) == sorted(r.value for r in INVITABLE)
        assert "owner" not in json.dumps(schemas[schema_name])

    @pytest.mark.parametrize("actor", ADMINS)
    def test_service_refuses_owner_even_when_called_directly(self, env: Env, actor: Role):
        with env.request_factory() as s:
            ctx = OrganizationContext(
                user=s.get(User, _uid(ORG_A, actor)),
                organization=s.get(Organization, ORG_A),
                role=actor,
            )
            with pytest.raises(PermissionDeniedError) as exc:
                invitation_service.create_invitation(
                    s, ctx=ctx, email="o@example.com", role=Role.OWNER
                )
            assert exc.value.code == "invitation_owner_forbidden"
            assert exc.value.status_code == 403
            assert s.scalar(select(func.count()).select_from(OrganizationInvitation)) == 0
            s.rollback()
        assert env.witness.invitations() == []

    def test_invitable_roles_constant(self):
        assert invitation_service.INVITABLE_ROLES == frozenset(INVITABLE)

    @pytest.mark.parametrize("ctx_role", NON_ADMINS)
    def test_service_refuses_a_non_admin_context_first(self, env: Env, ctx_role: Role):
        """Defence in depth: the service re-checks OWNER/ADMIN before any other rule."""
        with env.request_factory() as s:
            ctx = OrganizationContext(
                user=s.get(User, _uid(ORG_A, ctx_role)),
                organization=s.get(Organization, ORG_A),
                role=ctx_role,
            )
            for role in (Role.OWNER, Role.VIEWER):
                with pytest.raises(PermissionDeniedError) as exc:
                    invitation_service.create_invitation(
                        s, ctx=ctx, email="d@example.com", role=role
                    )
                assert (exc.value.code, exc.value.message) == (
                    "permission_denied",
                    _role_denial(ctx_role),
                )
            s.rollback()

    def test_ceiling_is_never_exceeded_by_an_admin_or_owner(self):
        """``invitation_role_forbidden`` guards rank(role) > rank(actor). With the actor
        restricted to OWNER/ADMIN and OWNER excluded, no invitable role outranks an ADMIN,
        so the code is defence in depth and unreachable through any consistent input. The
        property it protects -- no invitation above the inviter -- is what is pinned."""
        from app.organizations.members import role_rank

        assert max(role_rank(r) for r in INVITABLE) <= role_rank(Role.ADMIN) < role_rank(Role.OWNER)


class TestConflicts:
    def test_email_of_an_existing_member_is_409(self, env: Env):
        r = _create(env, _email(_uid(ORG_A, Role.VIEWER)), "admin")
        assert _error(r)[:2] == (409, "invitation_already_member")
        assert env.witness.invitations() == [] and env.witness.audit() == []

    def test_member_of_another_organization_can_be_invited(self, env: Env):
        r = _create(env, _email(_uid(ORG_B, Role.OWNER)), "viewer")
        assert r.status_code == 201, r.text

    def test_duplicate_pending_is_409_and_keeps_the_first(self, env: Env):
        first = _create(env, "dup@example.com", "viewer")
        second = _create(env, "dup@example.com", "admin")
        assert first.status_code == 201
        assert _error(second)[:2] == (409, "invitation_pending_exists")
        assert "token" not in second.text
        [row] = env.witness.invitations(ORG_A)
        assert (row["id"], row["role"]) == (first.json()["id"], "viewer")
        assert len(env.witness.audit("organization_invitation.created")) == 1

    def test_same_email_in_another_organization_is_independent(self, env: Env):
        a = _create(env, "shared@example.com", "viewer")
        b = _create(env, "shared@example.com", "viewer", org=ORG_B)
        assert (a.status_code, b.status_code) == (201, 201)
        assert len(env.witness.pending(ORG_A)) == len(env.witness.pending(ORG_B)) == 1

    def test_email_is_matched_exactly(self, env: Env):
        """Exact-match semantics (contract: no lower()/citext). The local part is case
        significant, so a case variant is a distinct address; pinned, not endorsed."""
        a = _create(env, "Case.Person@example.com", "viewer")
        b = _create(env, "case.person@example.com", "viewer")
        assert (a.status_code, b.status_code) == (201, 201)
        assert {r["email"] for r in env.witness.pending(ORG_A)} == {
            "Case.Person@example.com",
            "case.person@example.com",
        }

    def test_expired_invitation_is_revoked_and_replaced(self, env: Env):
        old = _create(env, "again@example.com", "viewer").json()
        _expire(env, old["id"])
        new = _create(env, "again@example.com", "marketer")
        assert new.status_code == 201, new.text
        new = new.json()
        assert new["id"] != old["id"] and new["token"] != old["token"]
        old_row, new_row = env.witness.invitation(old["id"]), env.witness.invitation(new["id"])
        assert old_row["revoked_at"] is not None and old_row["accepted_at"] is None
        assert new_row["revoked_at"] is None and new_row["role"] == "marketer"
        assert old_row["token_hash"] != new_row["token_hash"]
        [superseded] = env.witness.audit("organization_invitation.revoked")
        assert (superseded["entity_id"], superseded["reason"]) == (
            old["id"],
            "Expired invitation superseded by a new invitation.",
        )
        assert set(superseded["previous_state"]) == {"email", "role", "expires_at"}
        # The old link now reports revoked (revoked outranks expired); the new one works.
        preview = f"{API}/auth/invitations/preview"
        assert _error(_send(env, "POST", preview, None, {"token": old["token"]}))[:2] == (
            409,
            "invitation_revoked",
        )
        assert _send(env, "POST", preview, None, {"token": new["token"]}).status_code == 200

    def test_revoked_invitation_can_be_reissued(self, env: Env):
        old = _create(env, "reissue@example.com").json()
        assert (
            _send(
                env, "DELETE", f"{_invitations_url()}/{old['id']}", _uid(ORG_A, Role.OWNER)
            ).status_code
            == 204
        )
        assert _create(env, "reissue@example.com").status_code == 201
        assert len(env.witness.invitations(ORG_A)) == 2
        assert len(env.witness.pending(ORG_A)) == 1


# --------------------------------------------------------------------------- #
class TestExpiry:
    def _bracketed_create(self, env: Env, email: str) -> tuple[datetime, dict, datetime]:
        before = env.witness.db_now()
        r = _create(env, email, "viewer")
        after = env.witness.db_now()
        assert r.status_code == 201, r.text
        return before, r.json(), after

    def _assert_lifetime(self, env: Env, email: str, hours: int) -> dict:
        before, body, after = self._bracketed_create(env, email)
        expires = _as_utc(env.witness.invitation(body["id"])["expires_at"])
        lifetime = timedelta(hours=hours)
        assert before + lifetime - CLOCK_TOLERANCE <= expires <= after + lifetime + CLOCK_TOLERANCE
        # The response reports the stored value.
        assert abs(_as_utc(body["expires_at"]) - expires) < timedelta(milliseconds=1)
        return body

    def test_default_lifetime_on_the_database_clock(self, env: Env):
        assert Settings.model_fields["invitation_expire_hours"].default == DEFAULT_EXPIRE_HOURS
        assert get_settings().invitation_expire_hours == DEFAULT_EXPIRE_HOURS
        self._assert_lifetime(env, "clock@example.com", DEFAULT_EXPIRE_HOURS)

    def test_setting_overrides_the_lifetime_and_leaves_the_default(self, env: Env, monkeypatch):
        override = 5
        assert override != DEFAULT_EXPIRE_HOURS
        with monkeypatch.context() as m:
            m.setattr(get_settings(), "invitation_expire_hours", override)
            self._assert_lifetime(env, "short@example.com", override)
        # The override was scoped: the next invitation gets the default again.
        assert get_settings().invitation_expire_hours == DEFAULT_EXPIRE_HOURS
        assert Settings.model_fields["invitation_expire_hours"].default == DEFAULT_EXPIRE_HOURS
        self._assert_lifetime(env, "after-override@example.com", DEFAULT_EXPIRE_HOURS)

    def test_settings_field_is_validated(self, monkeypatch):
        monkeypatch.delenv("INVITATION_EXPIRE_HOURS", raising=False)
        assert Settings().invitation_expire_hours == DEFAULT_EXPIRE_HOURS
        monkeypatch.setenv("INVITATION_EXPIRE_HOURS", "24")
        assert Settings().invitation_expire_hours == 24
        monkeypatch.delenv("INVITATION_EXPIRE_HOURS")
        for bad in (0, -1):
            with pytest.raises(ValidationError, match="invitation_expire_hours"):
                Settings(invitation_expire_hours=bad)
        with pytest.raises(ValidationError):
            Settings(invitation_expire_hours="seventy-two")

    def test_expired_invitations_leave_the_list(self, env: Env):
        keep = _create(env, "keep@example.com").json()
        gone = _create(env, "gone@example.com").json()
        _expire(env, gone["id"])
        listing = _send(env, "GET", _invitations_url(), _uid(ORG_A, Role.ADMIN)).json()
        assert [item["id"] for item in listing] == [keep["id"]]


class TestDatabaseClockNotAppClock:
    """G3: expiry is computed from, and decided against, the DATABASE clock.

    The application host's clock is skewed by days -- ``datetime`` is replaced in every
    module of this feature that imports it -- while the database's clock is left alone. Were
    any expiry computed or decided from the host clock, these outcomes would flip.
    """

    MODULES = ("app.organizations.invitations", "app.organizations.members", "app.db.base")

    def _skew(self, monkeypatch, delta: timedelta) -> None:
        real = datetime

        class SkewedDatetime(real):
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + delta

            @classmethod
            def utcnow(cls):
                return real.now(UTC).replace(tzinfo=None) + delta

        import importlib

        for name in self.MODULES:
            module = importlib.import_module(name)
            assert module.datetime is real, name  # the time source this test replaces
            monkeypatch.setattr(module, "datetime", SkewedDatetime)
        # Positive instance: the host clock the feature sees really is skewed ...
        assert abs(invitation_service.datetime.now(UTC) - real.now(UTC) - delta) < CLOCK_TOLERANCE

    @pytest.mark.parametrize("days", [10, -10])
    def test_database_now_ignores_the_host_clock(self, env: Env, monkeypatch, days: int):
        self._skew(monkeypatch, timedelta(days=days))
        with env.request_factory() as s:
            service_now = invitation_service.database_now(s)
        # ... and the database clock the service reads is not.
        assert abs(service_now - env.witness.db_now()) < CLOCK_TOLERANCE

    def test_expiry_is_computed_from_the_database_clock(self, env: Env, monkeypatch):
        self._skew(monkeypatch, timedelta(days=10))
        before = env.witness.db_now()
        r = _create(env, "skewed@example.com")
        after = env.witness.db_now()
        assert r.status_code == 201, r.text
        expires = _as_utc(env.witness.invitation(r.json()["id"])["expires_at"])
        lifetime = timedelta(hours=DEFAULT_EXPIRE_HOURS)
        assert before + lifetime - CLOCK_TOLERANCE <= expires <= after + lifetime + CLOCK_TOLERANCE

    def test_host_clock_ahead_does_not_expire_a_pending_invitation(self, env: Env, monkeypatch):
        inv = _create(env, "ahead@example.com").json()
        skew = timedelta(days=10)
        assert skew > timedelta(hours=DEFAULT_EXPIRE_HOURS)  # the lifetime has "passed" on the host
        self._skew(monkeypatch, skew)
        preview = _send(
            env, "POST", f"{API}/auth/invitations/preview", None, {"token": inv["token"]}
        )
        assert preview.status_code == 200, preview.text
        listing = _send(env, "GET", _invitations_url(), _uid(ORG_A, Role.OWNER)).json()
        assert [i["id"] for i in listing] == [inv["id"]]
        reg = _send(
            env,
            "POST",
            f"{API}/auth/invitations/register",
            None,
            {"token": inv["token"], "full_name": "Ahead", "password": "password-123"},
        )
        assert reg.status_code == 201, reg.text

    def test_host_clock_behind_does_not_revive_an_expired_invitation(self, env: Env, monkeypatch):
        inv = _create(env, "behind@example.com").json()
        _expire(env, inv["id"])  # expired on the DATABASE clock
        self._skew(monkeypatch, timedelta(days=-10))  # still "valid" on the host
        preview = _send(
            env, "POST", f"{API}/auth/invitations/preview", None, {"token": inv["token"]}
        )
        assert _error(preview)[:2] == (409, "invitation_expired")
        revoke = _send(env, "DELETE", f"{_invitations_url()}/{inv['id']}", _uid(ORG_A, Role.OWNER))
        assert _error(revoke)[:2] == (409, "invitation_expired")
        assert _send(env, "GET", _invitations_url(), _uid(ORG_A, Role.OWNER)).json() == []
        # Re-inviting supersedes it as expired rather than refusing it as pending.
        assert _create(env, "behind@example.com").status_code == 201
        assert env.witness.invitation(inv["id"])["revoked_at"] is not None

    def _outsider_memberships(self, env: Env) -> set[tuple[str, str]]:
        with env.witness.session() as s:
            return set(
                s.execute(
                    select(OrganizationMember.organization_id, OrganizationMember.role).where(
                        OrganizationMember.user_id == OUTSIDER
                    )
                )
            )

    def test_host_clock_behind_does_not_let_an_existing_user_accept(self, env: Env, monkeypatch):
        """The signed-in acceptance path decides expiry on the database clock too."""
        inv = _create(env, _email(OUTSIDER)).json()
        _expire(env, inv["id"])  # expired on the DATABASE clock
        self._skew(monkeypatch, timedelta(days=-10))  # still "valid" on the host
        r = _send(env, "POST", f"{API}/auth/invitations/accept", OUTSIDER, {"token": inv["token"]})
        assert _error(r)[:2] == (409, "invitation_expired")
        assert self._outsider_memberships(env) == set()
        assert env.witness.invitation(inv["id"])["accepted_at"] is None

    def test_host_clock_ahead_does_not_expire_an_existing_users_acceptance(
        self, env: Env, monkeypatch
    ):
        inv = _create(env, _email(OUTSIDER), "reviewer").json()
        skew = timedelta(days=10)
        assert skew > timedelta(hours=DEFAULT_EXPIRE_HOURS)  # the lifetime has "passed" on the host
        self._skew(monkeypatch, skew)
        r = _send(env, "POST", f"{API}/auth/invitations/accept", OUTSIDER, {"token": inv["token"]})
        assert r.status_code == 200, r.text
        assert self._outsider_memberships(env) == {(ORG_A, "reviewer")}
        assert env.witness.invitation(inv["id"])["accepted_by_user_id"] == OUTSIDER


# --------------------------------------------------------------------------- #
class TestList:
    def test_only_pending_newest_first_exact_fields(self, env: Env):
        ids = [_create(env, f"p{i}@example.com").json()["id"] for i in range(3)]
        revoked = _create(env, "revoked@example.com").json()["id"]
        _send(env, "DELETE", f"{_invitations_url()}/{revoked}", _uid(ORG_A, Role.OWNER))
        r = _send(env, "GET", _invitations_url(), _uid(ORG_A, Role.OWNER))
        assert r.status_code == 200
        listing = r.json()
        assert [item["id"] for item in listing] == list(reversed(ids))
        for item in listing:
            assert set(item) == INVITATION_OUT_FIELDS
            assert item["invited_by_user_id"] == _uid(ORG_A, Role.OWNER)

    def test_list_is_scoped_to_the_path_organization(self, env: Env):
        _create(env, "a-only@example.com")
        _create(env, "b-only@example.com", org=ORG_B)
        a = _send(env, "GET", _invitations_url(), _uid(ORG_A, Role.OWNER)).json()
        b = _send(env, "GET", _invitations_url(ORG_B), _uid(ORG_B, Role.OWNER)).json()
        assert [i["email"] for i in a] == ["a-only@example.com"]
        assert [i["email"] for i in b] == ["b-only@example.com"]

    @pytest.mark.parametrize("role", NON_ADMINS)
    def test_non_admins_cannot_list(self, env: Env, role: Role):
        _create(env, "hidden@example.com")
        r = _send(env, "GET", _invitations_url(), _uid(ORG_A, role))
        assert _error(r) == (403, "permission_denied", _role_denial(role))
        assert "hidden@example.com" not in r.text


# --------------------------------------------------------------------------- #
class TestRevoke:
    def _revoke(self, env: Env, invitation_id: str, actor: str | None = None, org: str = ORG_A):
        return _send(
            env,
            "DELETE",
            f"{_invitations_url(org)}/{invitation_id}",
            actor or _uid(org, Role.OWNER),
        )

    @pytest.mark.parametrize("actor", ADMINS)
    def test_revoke_pending(self, env: Env, actor: Role):
        inv = _create(env, "revoke-me@example.com").json()
        r = self._revoke(env, inv["id"], _uid(ORG_A, actor))
        assert (r.status_code, r.content) == (204, b"")
        row = env.witness.invitation(inv["id"])
        assert row["revoked_at"] is not None and row["accepted_at"] is None
        [audit] = env.witness.audit("organization_invitation.revoked")
        assert (audit["actor_user_id"], audit["entity_id"], audit["workspace_id"]) == (
            _uid(ORG_A, actor),
            inv["id"],
            None,
        )
        assert set(audit["previous_state"]) == {"email", "role", "expires_at"}
        assert inv["token"] not in json.dumps(audit, default=str)

    def test_revoked_is_409_revoked(self, env: Env):
        inv = _create(env, "twice@example.com").json()
        assert self._revoke(env, inv["id"]).status_code == 204
        assert _error(self._revoke(env, inv["id"]))[:2] == (409, "invitation_revoked")
        assert len(env.witness.audit("organization_invitation.revoked")) == 1

    def test_expired_is_409_expired(self, env: Env):
        inv = _create(env, "late@example.com").json()
        _expire(env, inv["id"])
        assert _error(self._revoke(env, inv["id"]))[:2] == (409, "invitation_expired")
        assert env.witness.invitation(inv["id"])["revoked_at"] is None

    def test_accepted_is_409_already_used(self, env: Env):
        inv = _create(env, "taken@example.com").json()
        reg = _send(
            env,
            "POST",
            f"{API}/auth/invitations/register",
            None,
            {"token": inv["token"], "full_name": "Taken", "password": "password-123"},
        )
        assert reg.status_code == 201, reg.text
        assert _error(self._revoke(env, inv["id"]))[:2] == (409, "invitation_already_used")

    def test_unknown_id_is_404(self, env: Env):
        assert _error(self._revoke(env, "no-such-invitation"))[:2] == (404, "invitation_not_found")

    def test_another_organizations_invitation_is_404_and_untouched(self, env: Env):
        b_inv = _create(env, "b-guest@example.com", org=ORG_B).json()
        r = self._revoke(env, b_inv["id"], _uid(ORG_A, Role.OWNER), org=ORG_A)
        assert _error(r)[:2] == (404, "invitation_not_found")
        assert env.witness.invitation(b_inv["id"])["revoked_at"] is None
        # ...and the same id through B's path by A's owner is a membership refusal.
        r = self._revoke(env, b_inv["id"], _uid(ORG_A, Role.OWNER), org=ORG_B)
        assert _error(r) == (403, "permission_denied", NOT_A_MEMBER)

    @pytest.mark.parametrize("role", NON_ADMINS)
    def test_non_admins_cannot_revoke(self, env: Env, role: Role):
        inv = _create(env, "keep-me@example.com").json()
        assert _error(self._revoke(env, inv["id"], _uid(ORG_A, role))) == (
            403,
            "permission_denied",
            _role_denial(role),
        )
        assert env.witness.invitation(inv["id"])["revoked_at"] is None


# --------------------------------------------------------------------------- #
class TestExplicitCommit:
    """Under ``withheld_env`` the teardown never commits: visibility means the route did."""

    def test_positive_control_a_route_without_explicit_commit_leaves_nothing(
        self, withheld_env: Env
    ):
        r = _send(
            withheld_env,
            "POST",
            f"{API}/organizations/{ORG_A}/workspaces",
            _uid(ORG_A, Role.OWNER),
            {"name": "Ghost"},
        )
        assert r.status_code == 201  # the handler succeeded ...
        assert withheld_env.witness.count(Workspace) == 1  # ... but only the seeded row exists

    def test_create_is_committed_before_the_response(self, withheld_env: Env):
        r = _create(withheld_env, "committed@example.com")
        assert r.status_code == 201
        assert [row["id"] for row in withheld_env.witness.invitations()] == [r.json()["id"]]
        assert len(withheld_env.witness.audit("organization_invitation.created")) == 1

    def test_revoke_is_committed_before_the_response(self, withheld_env: Env):
        inv = _create(withheld_env, "revoke-committed@example.com").json()
        r = _send(
            withheld_env, "DELETE", f"{_invitations_url()}/{inv['id']}", _uid(ORG_A, Role.OWNER)
        )
        assert r.status_code == 204
        assert withheld_env.witness.invitation(inv["id"])["revoked_at"] is not None

    def test_a_refusal_commits_nothing(self, withheld_env: Env):
        _create(withheld_env, "once@example.com")
        before = withheld_env.witness.audit()
        assert _create(withheld_env, "once@example.com").status_code == 409
        assert withheld_env.witness.audit() == before
        assert len(withheld_env.witness.invitations()) == 1


# --------------------------------------------------------------------------- #
class TestErrorEnvelope:
    def test_instance_codes_reach_the_envelope_and_leave_the_classes_alone(self, env: Env):
        _create(env, "env@example.com")
        r = _create(env, "env@example.com")
        body = r.json()
        assert r.status_code == 409
        assert body["error"]["code"] == "invitation_pending_exists"
        assert (
            body["error"]["message"]
            == "A pending invitation already exists for this email address."
        )
        assert body["error"]["request_id"]
        # A per-instance code never rewrites the class default.
        assert (
            ConflictError.code,
            NotFoundError.code,
            PermissionDeniedError.code,
            SignalNestError.code,
        ) == (
            "conflict",
            "not_found",
            "permission_denied",
            "error",
        )
        assert ConflictError("plain").code == "conflict"


# --------------------------------------------------------------------------- #
class TestNoTokenInLogs:
    def test_create_and_list_log_no_token(self, env: Env, caplog):
        caplog.set_level(logging.DEBUG)
        r = _create(env, "logged@example.com")
        token = r.json()["token"]
        _send(env, "GET", _invitations_url(), _uid(ORG_A, Role.OWNER))
        [row] = env.witness.invitations()
        # Positive control: the capture really sees this app's request log for the create.
        assert any(
            rec.name == "signalnest.request"
            and "/invitations" in json.dumps(getattr(rec, "extra_fields", {}), default=str)
            for rec in caplog.records
        ), [rec.name for rec in caplog.records]
        for rec in caplog.records:
            rendered = rec.getMessage() + json.dumps(rec.__dict__, default=str)
            assert token not in rendered and row["token_hash"] not in rendered, rec.name


def test_production_session_factory_is_untouched():
    """This module overrides ``get_db`` only; the production factory stays configured."""
    assert SessionLocal.kw.get("expire_on_commit") is False
