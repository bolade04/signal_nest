"""6B-2: HTTP-boundary authorization proof for workspace creation (P6-AUTH-7).

    POST /api/v1/organizations/{organization_id}/workspaces

Before this tranche the route authenticated the caller and then checked, inside its body,
only that the caller was *a member* of the organization. Role played no part, so every
member -- VIEWER included -- could create workspaces (measured on the base route: all six
roles received 201 and each left a committed row). The route now depends on
``require_exact_organization_roles(Role.OWNER, Role.ADMIN)``: exact membership of an
explicitly named set, with no rank floor and no inheritance.

What runs for real. Only ``get_db`` is overridden, and every request helper asserts that it
is the *only* override, so bearer parsing, token decode, user lookup, the shared membership
lookup, the organization lookup and the role checker all execute. Tokens come from
``create_access_token``; memberships are real ``OrganizationMember`` rows in a temporary
SQLite *file* with foreign keys on. The request-path session factory is configured like
production's ``SessionLocal`` (``autoflush=False, expire_on_commit=False``) and the override
mirrors production ``get_db`` (commit on success, rollback on exception, close). Each test
gets a freshly seeded database, so no test depends on another's writes.

Scope of the claim. Role assignment does not exist yet (P6-AUTH-1 is open): today a
non-OWNER role is held only by rows like the ones this module seeds. The matrix proves what
the route does *if* such a membership exists; it does not claim anyone can obtain one. The
route writes no audit record, and nothing here asserts or relies on one (P6-PRIV-5 is open).

Load-bearing properties, each guarded by its own test:

* **Committed state, observed independently.** A status code says what the handler
  returned, not what the database kept. Every POST is bracketed by reads through a
  *separate* ``Engine`` on the same file -- never the request's session, never its pool --
  so a denial that nonetheless wrote a row, or a 201 whose commit was lost, is visible. The
  witness is shown to see a committed OWNER and ADMIN creation (+1, row present) and shown
  *not* to see an uncommitted flush, so "delta 0" is a measurement rather than an artefact
  of rollback or teardown.
* **No second write channel.** Because only ``get_db`` is overridden, the witness sees the
  request's database and nothing else. A guard therefore also refuses, and records, any use
  of the production ``SessionLocal``, of any engine other than the request's and the
  witness's, or of a raw ``sqlite3.connect`` to any other file -- before a session or
  connection exists, and on the record even if the code under test swallows the refusal.
  A connection another engine opened *before* the guard -- at import, say -- is refused
  statement by statement, before the driver executes anything.
  Every workspace-create request this module sends goes through ``_attempt``, inside that
  guard, and fails its test if the guard recorded anything -- whatever the caller: every
  role, operators, multi-membership users, invalid bodies, both sides of a role change,
  every rung of the error ladder. A structural test pins that no request is sent any other
  way, and the guard is shown live on each door, and nested. (A raw connection through a
  non-SQLite driver such as psycopg is outside the model: the database the tests are
  configured with is SQLite.) Also outside the model, disclosed rather than tested: a
  raw DB-API connection -- ``sqlite3`` or psycopg -- opened before every listener and
  reused later, whose driver reports its connect but never its statements
  (PREOPENED_RAW_DBAPI_CONNECTION = DISCLOSED_OUT_OF_MODEL); and a write deferred past the
  response, such as a timer that commits after the 403 has been returned, which lands after
  the witness's last read and outside the guard's window (R2-03). So, like that raw
  connection, is deliberately forging SQLAlchemy connection internals -- a connection's
  engine and pool identity -- to impersonate the request's engine
  (GUARD_INTERNALS_FORGERY = DISCLOSED_OUT_OF_MODEL).
* **A denial's source.** Membership failure and role failure are both
  ``403 / permission_denied``; only the production-owned messages tell them apart. Each
  denied role is shown to be authenticated and a member of the target organization -- the
  same token lists its workspaces, and the witness sees the membership row -- before it is
  refused with the role message for that exact role.
* **The role is read from the persisted membership on every request.** A role change takes
  effect on the next request. One user, sending one unchanged bearer header, is admitted as
  ADMIN, has that same membership row changed to VIEWER through a separate connection, and
  is refused with the VIEWER role message on the very next request; promoted from VIEWER to
  ADMIN the same way, a refused user is admitted. Neither an earlier admission nor an
  earlier refusal is carried over. Every allowed role is re-evaluated on every request,
  OWNER included -- a stale grant of the top role is the one that would matter most: OWNER
  and ADMIN are each demoted the same way to every denied role and refused on the next
  request. (No role-assignment route exists yet, so the change is a committed UPDATE of
  that row, made directly.)
* **Authorized organization == written organization.** The handler takes organization
  identity only from the context the dependency authorized. Users holding different roles
  in two organizations are what can distinguish that from a mix-up; every creation lands in
  exactly one organization, the one in the path, and every refusal lands in none. An
  ``organization_id`` smuggled into the query string or the JSON body is inert: it neither
  redirects the write nor authorizes a different organization.
* **The operator flag does not alter the role decision.** An operator who is a member of
  nothing stops at membership and so cannot exercise the role guard. Operators who hold
  each role in the path organization can: denied roles are refused with that role's message
  and write nothing, OWNER and ADMIN create -- exactly the non-operator outcome.
* **The limiter that is reset is the one that serves requests** -- located from
  ``app.middleware_stack``, never from a freshly built, detached chain.

Disclosed behaviour changes on this route, pinned here rather than hidden:

* The POST non-member message is now the shared seam's "You are not a member of this
  organization." instead of the route-local "Not a member of this organization.". The GET
  routes are untouched and still return the legacy message; both are asserted side by side.
* Authorization now precedes request-body validation. The base route checked membership in
  the handler, after FastAPI had validated the body, so any authenticated caller -- member
  or not -- sending an invalid body got 422. Dependencies are now resolved first: a
  non-member or a forbidden role gets 403 for an invalid body, an OWNER still gets 422.
  (JSON that cannot be decoded at all is still rejected with 422 before authorization; that
  is FastAPI reading the body before resolving dependencies, and is pinned too.)

Unchanged and pinned: an organization id with no membership is 403 (membership), not 404, so
existence is not disclosed to non-members; slug normalization and the single-collision
length suffix behave exactly as before.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select, text, update
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import Pool

from app.auth.dependencies import get_current_user
from app.core.config import get_settings
from app.core.enums import Role
from app.core.middleware import RateLimitMiddleware
from app.core.security import create_access_token
from app.db import session as db_session
from app.db.models import Base
from app.db.session import SessionLocal, get_db
from app.main import app
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.organizations.schemas import WorkspaceOut

API = get_settings().api_prefix
CREATE_PATH = f"{API}/organizations/{{organization_id}}/workspaces"

ORG_A, ORG_B = "org-wsc-a", "org-wsc-b"
# One pre-existing workspace per organization, so listings are never vacuously empty.
WS_A, WS_B = "ws-wsc-a", "ws-wsc-b"
#: An organization id that is never created.
ORG_MISSING = "org-wsc-missing"

# The exact policy the route must enforce.
ALLOWED = (Role.OWNER, Role.ADMIN)
DENIED = (Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER, Role.VIEWER)
ALL_ROLES = ALLOWED + DENIED

OUTSIDER = "wsc-outsider"  # active, a member of nothing
OPERATOR = "wsc-operator"  # is_operator=True, a member of nothing
GHOST = "wsc-ghost"  # a token subject with no user row

# X is OWNER in A and VIEWER in B; Y is VIEWER in A and ADMIN in B. Each can create in
# exactly one organization, and is refused by role -- not membership -- in the other.
MULTI_X, MULTI_Y = "wsc-multi-x", "wsc-multi-y"
MULTI_MEMBERSHIPS = (
    (MULTI_X, ORG_A, Role.OWNER),
    (MULTI_X, ORG_B, Role.VIEWER),
    (MULTI_Y, ORG_A, Role.VIEWER),
    (MULTI_Y, ORG_B, Role.ADMIN),
)

# Production-owned messages. Membership and role denials are both 403 / permission_denied,
# so the message is the only thing that says which gate refused the request.
NOT_A_MEMBER_MESSAGE = "You are not a member of this organization."  # shared seam
LEGACY_NOT_A_MEMBER_MESSAGE = "Not a member of this organization."  # GET routes' helper
MISSING_BEARER_MESSAGE = "Missing bearer token."
INVALID_TOKEN_MESSAGE = "Invalid or expired token."
UNKNOWN_USER_MESSAGE = "User not found or inactive."


def _role_denial_message(role: Role) -> str:
    return f"Role '{role.value}' is not permitted for this action."


# Every id column in play is String(32). Read off the mapping, so a model change surfaces here.
_ID_MAX = User.__table__.c.id.type.length


def _db_id(value: str) -> str:
    """Return ``value`` unless it cannot fit a ``String(32)`` id column.

    SQLite stores an over-long value silently; PostgreSQL rejects it. Failing at
    construction keeps this module honest without a live PostgreSQL service.
    """
    if len(value) > _ID_MAX:
        raise AssertionError(
            f"fixture id {value!r} is {len(value)} characters; the id columns are String({_ID_MAX})"
        )
    return value


def _uid(org: str, role: Role) -> str:
    """One user per role per organization: ``org-wsc-a-viewer``."""
    return _db_id(f"{org}-{role.value}")


def _mid(org: str, role: Role) -> str:
    # The six role values have distinct three-letter prefixes (own/adm/mar/rev/com/vie).
    return _db_id(f"m-{org}-{role.value[:3]}")


def _multi_mid(user_id: str, org: str) -> str:
    return _db_id(f"m-{user_id}-{org[-1]}")


def _op_uid(role: Role) -> str:
    """A platform operator who is ALSO a member of ORG_A with ``role``: ``wsc-op-vie``."""
    return _db_id(f"wsc-op-{role.value[:3]}")


# Operators holding each role in ORG_A, one user per role. Unlike OPERATOR, these clear
# membership, so they are what can show the flag neither waives nor replaces the role check.
OPERATOR_MEMBERSHIPS = tuple((_op_uid(role), ORG_A, role) for role in ALL_ROLES)


def _generated_ids() -> list[str]:
    return (
        [ORG_A, ORG_B, WS_A, WS_B, ORG_MISSING, OUTSIDER, OPERATOR, GHOST, MULTI_X, MULTI_Y]
        + [_uid(org, role) for org in (ORG_A, ORG_B) for role in ALL_ROLES]
        + [_mid(org, role) for org in (ORG_A, ORG_B) for role in ALL_ROLES]
        + [_multi_mid(user_id, org) for user_id, org, _ in MULTI_MEMBERSHIPS]
        + [_op_uid(role) for role in ALL_ROLES]
        + [_multi_mid(user_id, org) for user_id, org, _ in OPERATOR_MEMBERSHIPS]
    )


def _bearer(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _create_url(org: str) -> str:
    return f"{API}/organizations/{org}/workspaces"


# --------------------------------------------------------------------------- #
# Rate limiter -- the ACTIVE instance, never a detached one
# --------------------------------------------------------------------------- #
def _active_rate_limiter() -> RateLimitMiddleware | None:
    """Return the ``RateLimitMiddleware`` reachable from the app's own assigned stack.

    Starlette builds the stack lazily on first request and *assigns* it; this does the
    same. ``build_middleware_stack()`` alone would return a detached chain whose limiter no
    request ever consults.
    """
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    return None


def _reset_rate_limiter() -> RateLimitMiddleware:
    """Clear the active fixed-window budget and return the exact instance cleared."""
    limiter = _active_rate_limiter()
    assert limiter is not None, (
        "RateLimitMiddleware was not found in app.middleware_stack, so the shared "
        "fixed-window budget was NOT reset"
    )
    limiter._hits.clear()
    return limiter


# --------------------------------------------------------------------------- #
# Database, committed-state witness, environment
# --------------------------------------------------------------------------- #
def _sqlite_file_engine(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _add_user(s, user_id: str, *, is_operator: bool = False) -> None:
    s.add(
        User(
            id=_db_id(user_id),
            email=f"{user_id}@example.com",
            full_name=user_id,
            hashed_password="x",
            is_active=True,
            is_operator=is_operator,
        )
    )


def _seed(s) -> None:
    for org, ws in ((ORG_A, WS_A), (ORG_B, WS_B)):
        s.add(Organization(id=_db_id(org), name=f"Org {org}", slug=org))
        s.flush()
        s.add(Workspace(id=_db_id(ws), organization_id=org, name=f"WS {ws}", slug=ws))
        for role in ALL_ROLES:
            _add_user(s, _uid(org, role))
        s.flush()
        for role in ALL_ROLES:
            s.add(
                OrganizationMember(
                    id=_mid(org, role),
                    organization_id=org,
                    user_id=_uid(org, role),
                    role=role.value,
                )
            )
        s.flush()
    for user_id in (OUTSIDER, MULTI_X, MULTI_Y):
        _add_user(s, user_id)
    _add_user(s, OPERATOR, is_operator=True)
    for user_id, _, _ in OPERATOR_MEMBERSHIPS:
        _add_user(s, user_id, is_operator=True)
    s.flush()
    for user_id, org, role in MULTI_MEMBERSHIPS + OPERATOR_MEMBERSHIPS:
        s.add(
            OrganizationMember(
                id=_multi_mid(user_id, org),
                organization_id=org,
                user_id=user_id,
                role=role.value,
            )
        )
    s.commit()


#: (organization_id, id, name, slug, onboarding_completed) as committed.
WitnessRow = tuple[str, str, str, str, bool]
#: (id, user_id, organization_id, role) of a membership, as committed.
MembershipRow = tuple[str, str, str, str]


@dataclass(frozen=True)
class Witness:
    """Committed-state reader on its OWN ``Engine`` -- never the request's session or pool.

    Every read opens a fresh session on that engine, so it can only observe what the request
    path actually committed to the file.
    """

    engine: Engine

    def _session(self):
        return sessionmaker(bind=self.engine, autoflush=False, future=True)()

    def workspace_count(self, org: str | None = None) -> int:
        stmt = select(func.count()).select_from(Workspace)
        if org is not None:
            stmt = stmt.where(Workspace.organization_id == org)
        with self._session() as s:
            return s.scalar(stmt)

    def rows_with_slug(self, slug: str) -> tuple[WitnessRow, ...]:
        """Every committed workspace with ``slug``, in ANY organization."""
        with self._session() as s:
            rows = s.execute(
                select(
                    Workspace.organization_id,
                    Workspace.id,
                    Workspace.name,
                    Workspace.slug,
                    Workspace.onboarding_completed,
                )
                .where(Workspace.slug == slug)
                .order_by(Workspace.organization_id)
            ).all()
        return tuple(tuple(row) for row in rows)

    def workspace_ids(self, org: str) -> set[str]:
        with self._session() as s:
            return set(s.scalars(select(Workspace.id).where(Workspace.organization_id == org)))

    def membership_role(self, user_id: str, org: str) -> str | None:
        with self._session() as s:
            return s.scalar(
                select(OrganizationMember.role).where(
                    OrganizationMember.user_id == user_id,
                    OrganizationMember.organization_id == org,
                )
            )

    def membership_rows(self, user_id: str, org: str) -> tuple[MembershipRow, ...]:
        """Every committed membership of ``user_id`` in ``org``, primary key included."""
        with self._session() as s:
            rows = s.execute(
                select(
                    OrganizationMember.id,
                    OrganizationMember.user_id,
                    OrganizationMember.organization_id,
                    OrganizationMember.role,
                )
                .where(
                    OrganizationMember.user_id == user_id,
                    OrganizationMember.organization_id == org,
                )
                .order_by(OrganizationMember.id)
            ).all()
        return tuple(tuple(row) for row in rows)

    def memberships(self) -> set[tuple[str, str, str]]:
        """Every committed (user_id, organization_id, role)."""
        with self._session() as s:
            rows = s.execute(
                select(
                    OrganizationMember.user_id,
                    OrganizationMember.organization_id,
                    OrganizationMember.role,
                )
            )
            return {tuple(row) for row in rows}

    def membership_table(self) -> tuple[MembershipRow, ...]:
        """Every committed membership row, in every organization, primary key included."""
        with self._session() as s:
            rows = s.execute(
                select(
                    OrganizationMember.id,
                    OrganizationMember.user_id,
                    OrganizationMember.organization_id,
                    OrganizationMember.role,
                ).order_by(OrganizationMember.id)
            ).all()
        return tuple(tuple(row) for row in rows)

    def operator_ids(self) -> set[str]:
        with self._session() as s:
            return set(s.scalars(select(User.id).where(User.is_operator.is_(True))))

    def organization_exists(self, org: str) -> bool:
        with self._session() as s:
            return s.get(Organization, org) is not None

    def is_operator(self, user_id: str) -> bool:
        with self._session() as s:
            return s.get(User, user_id).is_operator


@dataclass(frozen=True)
class Env:
    client: TestClient
    witness: Witness
    request_engine: Engine
    request_factory: sessionmaker


@contextmanager
def _environment(db_file: Path) -> Iterator[Env]:
    request_engine = _sqlite_file_engine(db_file)
    witness_engine = _sqlite_file_engine(db_file)
    Base.metadata.create_all(request_engine)
    request_factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
    )
    with request_factory() as s:
        _seed(s)

    def _override_get_db():
        # Mirrors production get_db: commit on success, rollback on exception, close.
        s = request_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    _reset_rate_limiter()
    try:
        yield Env(
            client=TestClient(app),
            witness=Witness(witness_engine),
            request_engine=request_engine,
            request_factory=request_factory,
        )
    finally:
        app.dependency_overrides.clear()
        _reset_rate_limiter()
        request_engine.dispose()
        witness_engine.dispose()


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with _environment(tmp_path / "workspace_creation.db") as e:
        yield e


# --------------------------------------------------------------------------- #
# Request helpers -- every request asserts that only get_db is overridden
# --------------------------------------------------------------------------- #
def _assert_only_get_db_overridden() -> None:
    overridden = set(app.dependency_overrides)
    assert overridden == {get_db}, (
        f"dependency overrides are {overridden!r}; only get_db may be overridden, or the "
        "authorization under test is not the production path"
    )


def _get(env: Env, url: str, headers: dict[str, str]):
    _assert_only_get_db_overridden()
    return env.client.get(url, headers=headers)


@dataclass(frozen=True)
class Attempt:
    """One POST, bracketed by committed-state reads through the witness."""

    url: str  # as actually sent, query string included
    status: int
    payload: object
    before: int  # target organization's workspace count
    after: int
    total_before: int  # every organization
    total_after: int
    slug_rows_before: tuple[WitnessRow, ...]
    slug_rows_after: tuple[WitnessRow, ...]

    @property
    def delta(self) -> int:
        return self.after - self.before

    @property
    def total_delta(self) -> int:
        return self.total_after - self.total_before

    @property
    def error(self) -> tuple[str, str]:
        return self.payload["error"]["code"], self.payload["error"]["message"]


def _attempt(
    env: Env,
    org: str,
    headers: dict[str, str],
    *,
    name: str | None = None,
    slug: str | None = None,
    body: object = None,
    content: str | None = None,
    params: dict[str, str] | None = None,
) -> Attempt:
    """POST ``{"name": name}`` (or ``body`` / raw ``content``) and witness the result.

    ``slug`` is the slug the name would normalize to; its committed rows are captured in
    every organization, so a write into the wrong organization is visible too. ``params``
    is sent as the query string.

    This is the only place the module sends a request to the route, and it sends every one
    inside :func:`_production_channel_guard`, whoever the caller: the attempt fails if the
    guard recorded anything. The window spans the POST alone -- the witness's reads around
    it run outside, as does anything a test does between two attempts.
    """
    w = env.witness
    before, total_before = w.workspace_count(org), w.workspace_count()
    slug_before = w.rows_with_slug(slug) if slug is not None else ()

    _assert_only_get_db_overridden()
    with _production_channel_guard(env) as used:
        if content is not None:
            r = env.client.post(
                _create_url(org),
                content=content,
                headers={**headers, "Content-Type": "application/json"},
                params=params,
            )
        else:
            r = env.client.post(
                _create_url(org),
                json={"name": name} if body is None else body,
                headers=headers,
                params=params,
            )
    assert used == [], (
        f"POST {_create_url(org)} ({r.status_code}) used a database channel other than the "
        f"request's and the witness's: {used!r}"
    )

    return Attempt(
        url=str(r.request.url),
        status=r.status_code,
        payload=r.json(),
        before=before,
        after=w.workspace_count(org),
        total_before=total_before,
        total_after=w.workspace_count(),
        slug_rows_before=slug_before,
        slug_rows_after=w.rows_with_slug(slug) if slug is not None else (),
    )


def _assert_created(a: Attempt, *, org: str, name: str, slug: str) -> None:
    assert a.status == 201, a.payload
    assert (a.delta, a.total_delta) == (1, 1), (a.before, a.after, a.total_before, a.total_after)
    assert a.payload["organization_id"] == org
    assert a.payload["name"] == name
    assert a.payload["slug"] == slug
    # Exactly one committed row with this slug, anywhere -- in the path organization, and
    # it is the row the response describes.
    new_rows = tuple(row for row in a.slug_rows_after if row not in a.slug_rows_before)
    assert new_rows == ((org, a.payload["id"], name, slug, False),)


def _assert_refused(a: Attempt, *, status: int, code: str, message: str) -> None:
    assert a.status == status, a.payload
    assert a.error == (code, message)
    assert (a.delta, a.total_delta) == (0, 0), (a.before, a.after, a.total_before, a.total_after)
    assert a.slug_rows_after == a.slug_rows_before


# Distinct name/slug per role, so no POST in a matrix depends on the collision logic.
MATRIX_NAMES = {
    Role.OWNER: ("Matrix Owner", "matrix-owner"),
    Role.ADMIN: ("Matrix Admin", "matrix-admin"),
    Role.MARKETER: ("Matrix Marketer", "matrix-marketer"),
    Role.REVIEWER: ("Matrix Reviewer", "matrix-reviewer"),
    Role.COMPLIANCE_REVIEWER: ("Matrix Compliance Reviewer", "matrix-compliance-reviewer"),
    Role.VIEWER: ("Matrix Viewer", "matrix-viewer"),
}


class TestFixtureIntegrity:
    """The fixture's own preconditions. A defect here silently weakens every test below."""

    def test_id_columns_are_the_width_this_module_enforces(self):
        assert _ID_MAX == 32
        for model, names in (
            (Organization, ("id",)),
            (Workspace, ("id", "organization_id")),
            (User, ("id",)),
            (OrganizationMember, ("id", "organization_id", "user_id")),
        ):
            for name in names:
                assert model.__table__.c[name].type.length == _ID_MAX, (model.__name__, name)

    def test_id_guard_rejects_an_overlong_id(self):
        assert _db_id("x" * _ID_MAX) == "x" * _ID_MAX
        with pytest.raises(AssertionError):
            _db_id("x" * (_ID_MAX + 1))

    def test_every_generated_id_fits(self):
        generated = _generated_ids()
        assert generated
        assert {v: len(v) for v in generated if len(v) > _ID_MAX} == {}

    def test_membership_ids_are_distinct(self):
        extra = MULTI_MEMBERSHIPS + OPERATOR_MEMBERSHIPS
        ids = [_mid(org, role) for org in (ORG_A, ORG_B) for role in ALL_ROLES] + [
            _multi_mid(user_id, org) for user_id, org, _ in extra
        ]
        assert len(set(ids)) == len(ids) == 2 * len(ALL_ROLES) + len(extra)

    def test_operator_member_ids_are_distinct_per_role(self):
        assert len({_op_uid(role) for role in ALL_ROLES}) == len(ALL_ROLES)

    def test_witness_has_its_own_engine_and_pool(self, env: Env):
        """The witness never shares an Engine or a pooled connection with the request path."""
        assert env.witness.engine is not env.request_engine
        assert env.witness.engine.pool is not env.request_engine.pool
        assert env.witness.engine.url.database == env.request_engine.url.database

    def test_request_sessions_are_configured_like_production(self, env: Env):
        keys = ("autoflush", "expire_on_commit", "future")
        assert {k: env.request_factory.kw.get(k) for k in keys} == {
            k: SessionLocal.kw.get(k) for k in keys
        }
        assert env.request_factory.kw["autoflush"] is False

    def test_foreign_keys_are_enforced_on_both_engines(self, env: Env):
        for engine in (env.request_engine, env.witness.engine):
            with engine.connect() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1

    def test_seeded_memberships_are_what_the_tests_assume(self, env: Env):
        w = env.witness
        for org in (ORG_A, ORG_B):
            for role in ALL_ROLES:
                assert w.membership_role(_uid(org, role), org) == role.value
            other = ORG_B if org == ORG_A else ORG_A
            for role in ALL_ROLES:
                assert w.membership_role(_uid(org, role), other) is None
        for user_id, org, role in MULTI_MEMBERSHIPS + OPERATOR_MEMBERSHIPS:
            assert w.membership_role(user_id, org) == role.value
        for user_id, _, _ in OPERATOR_MEMBERSHIPS:
            assert w.membership_role(user_id, ORG_B) is None
        for user_id in (OUTSIDER, OPERATOR):
            assert w.membership_role(user_id, ORG_A) is None
            assert w.membership_role(user_id, ORG_B) is None
        assert w.is_operator(OPERATOR) is True
        assert w.is_operator(OUTSIDER) is False
        # The exact committed sets: nothing extra, nothing missing. In particular every
        # six-role matrix user is a non-operator, and the only operators are OPERATOR and
        # the per-role operator members.
        assert w.memberships() == (
            {(_uid(org, role), org, role.value) for org in (ORG_A, ORG_B) for role in ALL_ROLES}
            | {(u, org, role.value) for u, org, role in MULTI_MEMBERSHIPS + OPERATOR_MEMBERSHIPS}
        )
        assert w.operator_ids() == {OPERATOR} | {u for u, _, _ in OPERATOR_MEMBERSHIPS}
        assert w.organization_exists(ORG_A) and w.organization_exists(ORG_B)
        assert not w.organization_exists(ORG_MISSING)
        assert (w.workspace_ids(ORG_A), w.workspace_ids(ORG_B)) == ({WS_A}, {WS_B})

    def test_every_persisted_id_fits_its_column(self, env: Env):
        checked = 0
        with env.witness._session() as s:
            for model in (Organization, Workspace, User, OrganizationMember):
                for row in s.scalars(select(model)):
                    for column in model.__table__.c:
                        if column.name == "id" or column.name.endswith("_id"):
                            value = getattr(row, column.key)
                            assert len(value) <= column.type.length, (model.__name__, value)
                            checked += 1
        assert checked > 0

    def test_only_get_db_is_overridden_and_the_guard_is_live(self, env: Env):
        _assert_only_get_db_overridden()
        app.dependency_overrides[get_current_user] = lambda: None
        try:
            with pytest.raises(AssertionError, match="only get_db may be overridden"):
                _assert_only_get_db_overridden()
        finally:
            del app.dependency_overrides[get_current_user]
        _assert_only_get_db_overridden()

    def test_limiter_reset_targets_the_active_instance(self, env: Env):
        """Identity with the limiter found by this test's OWN walk, plus a planted probe."""
        node = app.middleware_stack
        while node is not None and not isinstance(node, RateLimitMiddleware):
            node = getattr(node, "app", None)
        assert isinstance(node, RateLimitMiddleware)

        node._hits["phase6b2-active-instance-proof"] = [123.0]
        assert _reset_rate_limiter() is node
        assert "phase6b2-active-instance-proof" not in node._hits

    def test_witness_cannot_see_an_uncommitted_write(self, env: Env):
        """The witness reads committed state only -- flushed-but-uncommitted is invisible.

        Without this, "delta 0" after a denial could be an artefact of the witness reading
        through the request's own transaction.
        """
        w = env.witness
        s = env.request_factory()
        try:
            s.add(Workspace(id="ws-wsc-uncommitted", organization_id=ORG_A, name="U", slug="u"))
            s.flush()
            assert w.workspace_count(ORG_A) == 1
            assert w.rows_with_slug("u") == ()
            s.commit()
            assert w.workspace_count(ORG_A) == 2
            assert w.rows_with_slug("u") == ((ORG_A, "ws-wsc-uncommitted", "U", "u", False),)
        finally:
            s.close()


class TestWitnessPositiveControl:
    """The witness demonstrably sees a committed creation, so its zeros mean something."""

    @pytest.mark.parametrize("role", ALLOWED)
    def test_committed_creation_is_visible_to_the_witness(self, env: Env, role: Role):
        name, slug = f"Control {role.value}", f"control-{role.value}"
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, role)), name=name, slug=slug)
        assert (a.before, a.after) == (1, 2)
        _assert_created(a, org=ORG_A, name=name, slug=slug)
        assert a.payload["id"] in env.witness.workspace_ids(ORG_A)


class TestSixRoleMatrix:
    """The load-bearing P6-AUTH-7 matrix on the target organization, with committed deltas."""

    @pytest.mark.parametrize("role", ALLOWED)
    def test_allowed_role_creates_a_committed_workspace(self, env: Env, role: Role):
        name, slug = MATRIX_NAMES[role]
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, role)), name=name, slug=slug)
        _assert_created(a, org=ORG_A, name=name, slug=slug)

    @pytest.mark.parametrize("role", DENIED)
    def test_denied_role_is_refused_and_writes_nothing(self, env: Env, role: Role):
        name, slug = MATRIX_NAMES[role]
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, role)), name=name, slug=slug)
        _assert_refused(a, status=403, code="permission_denied", message=_role_denial_message(role))
        assert a.slug_rows_after == ()

    def test_full_matrix_in_one_assertion(self, env: Env):
        """All six roles against one database, in one table -- a partial fix cannot pass."""
        observed = {}
        for role in ALL_ROLES:
            name, slug = MATRIX_NAMES[role]
            a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, role)), name=name, slug=slug)
            observed[role] = (a.status, a.delta, a.total_delta, len(a.slug_rows_after))
        assert observed == {
            Role.OWNER: (201, 1, 1, 1),
            Role.ADMIN: (201, 1, 1, 1),
            Role.MARKETER: (403, 0, 0, 0),
            Role.REVIEWER: (403, 0, 0, 0),
            Role.COMPLIANCE_REVIEWER: (403, 0, 0, 0),
            Role.VIEWER: (403, 0, 0, 0),
        }
        assert env.witness.workspace_count(ORG_A) == 1 + len(ALLOWED)

    def test_denial_message_does_not_disclose_the_policy(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, Role.VIEWER)), name="Leak", slug="leak")
        _, message = a.error
        for leaked in ("owner", "admin", ORG_A):
            assert leaked not in message


class TestDenialSource:
    """*Which* gate refused the request -- 403 alone cannot say."""

    @pytest.mark.parametrize("role", DENIED)
    def test_denied_role_is_an_authenticated_member_refused_by_the_role_guard(
        self, env: Env, role: Role
    ):
        user_id = _uid(ORG_A, role)
        headers = _bearer(user_id)

        # Member of the target organization, per committed state...
        assert env.witness.membership_role(user_id, ORG_A) == role.value
        # ... and per the running app: the same token authenticates and clears membership.
        listing = _get(env, _create_url(ORG_A), headers)
        assert listing.status_code == 200, listing.text

        name, slug = MATRIX_NAMES[role]
        a = _attempt(env, ORG_A, headers, name=name, slug=slug)
        _assert_refused(a, status=403, code="permission_denied", message=_role_denial_message(role))
        assert a.error[1] not in (NOT_A_MEMBER_MESSAGE, LEGACY_NOT_A_MEMBER_MESSAGE)

    def test_the_two_denials_differ_only_by_message(self, env: Env):
        role_denied = _attempt(
            env, ORG_A, _bearer(_uid(ORG_A, Role.VIEWER)), name="Src Role", slug="src-role"
        )
        member_denied = _attempt(env, ORG_A, _bearer(OUTSIDER), name="Src Mem", slug="src-mem")
        assert role_denied.status == member_denied.status == 403
        assert role_denied.error[0] == member_denied.error[0] == "permission_denied"
        assert role_denied.error[1] == _role_denial_message(Role.VIEWER)
        assert member_denied.error[1] == NOT_A_MEMBER_MESSAGE

    @pytest.mark.parametrize("role", DENIED)
    def test_one_token_two_denial_sources(self, env: Env, role: Role):
        """Role denial at home, membership denial away -- the same token."""
        headers = _bearer(_uid(ORG_A, role))
        home = _attempt(env, ORG_A, headers, name="Home Try", slug="home-try")
        away = _attempt(env, ORG_B, headers, name="Away Try", slug="away-try")
        _assert_refused(
            home, status=403, code="permission_denied", message=_role_denial_message(role)
        )
        _assert_refused(away, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)


def _persist_role(env: Env, membership_id: str, role: Role) -> None:
    """Commit ``role`` onto the existing membership row ``membership_id``, in place.

    Written on the witness's ``Engine`` -- its own connection and transaction, never a
    request's session or pool -- so the change reaches the app only through the committed
    file, as any other writer's would. Exactly one row must match; nothing is inserted.
    """
    with env.witness.engine.begin() as conn:
        result = conn.execute(
            update(OrganizationMember)
            .where(OrganizationMember.id == membership_id)
            .values(role=role.value)
        )
        assert result.rowcount == 1, (membership_id, result.rowcount)


def _delete_membership(env: Env, membership_id: str) -> None:
    """Commit the removal of membership row ``membership_id``, on the witness's ``Engine``."""
    with env.witness.engine.begin() as conn:
        result = conn.execute(
            delete(OrganizationMember).where(OrganizationMember.id == membership_id)
        )
        assert result.rowcount == 1, (membership_id, result.rowcount)


class TestRoleIsReadFreshOnEveryRequest:
    """A role change on the persisted membership decides the very next request.

    In the demotion and promotion tests exactly one thing differs between the two requests:
    the committed role on the user's one membership row in the path organization. The user,
    the organization, the endpoint and the bearer header -- one dict, one token, minted once
    -- are the same both times, and the row is updated in place, so its id, user and
    organization are identical before and after. An admit/deny decision remembered from the
    first request would repeat the first outcome; a role read afresh cannot. Both directions
    are pinned, so neither a stale admission nor a stale refusal can pass. Every allowed
    role is demoted, OWNER included: a grant remembered for the top role alone would pass an
    ADMIN-only demotion, and a stale OWNER grant is the one that would matter most.
    Membership removal is a separate property with its own test.
    """

    @staticmethod
    def _across_a_role_change(
        env: Env, held: Role, changed_to: Role, label: str
    ) -> tuple[list[tuple], Attempt, Attempt]:
        """Two POSTs by ORG_A's seeded ``held`` member, its row set to ``changed_to`` between.

        Returns, per request, (committed membership rows it was sent under, status, delta,
        total delta, committed rows with its slug), then the two attempts.
        """
        w = env.witness
        user_id, membership_id = _uid(ORG_A, held), _mid(ORG_A, held)
        headers = _bearer(user_id)  # minted once: the same dict, and token, for both requests
        sent = dict(headers)
        memberships_before = w.memberships()

        held_first = w.membership_rows(user_id, ORG_A)
        first = _attempt(env, ORG_A, headers, name=f"{label}-first", slug=f"{label}-first")
        _persist_role(env, membership_id, changed_to)
        held_second = w.membership_rows(user_id, ORG_A)
        second = _attempt(env, ORG_A, headers, name=f"{label}-second", slug=f"{label}-second")

        assert headers == sent
        # Updated in place: no membership was added, replaced or removed, here or anywhere.
        assert w.memberships() == (memberships_before - {(user_id, ORG_A, held.value)}) | {
            (user_id, ORG_A, changed_to.value)
        }
        table = [
            (held_first, first.status, first.delta, first.total_delta, len(first.slug_rows_after)),
            (
                held_second,
                second.status,
                second.delta,
                second.total_delta,
                len(second.slug_rows_after),
            ),
        ]
        return table, first, second

    def test_demotion_to_viewer_is_refused_on_the_next_request(self, env: Env):
        """ADMIN creates; the same row becomes VIEWER; the same header is refused by role."""
        user_id, membership_id = _uid(ORG_A, Role.ADMIN), _mid(ORG_A, Role.ADMIN)
        admin_row = (membership_id, user_id, ORG_A, Role.ADMIN.value)
        viewer_row = (membership_id, user_id, ORG_A, Role.VIEWER.value)

        table, first, second = self._across_a_role_change(env, Role.ADMIN, Role.VIEWER, "demoted")
        assert table == [
            ((admin_row,), 201, 1, 1, 1),
            ((viewer_row,), 403, 0, 0, 0),
        ]
        _assert_created(first, org=ORG_A, name="demoted-first", slug="demoted-first")
        _assert_refused(
            second, status=403, code="permission_denied", message=_role_denial_message(Role.VIEWER)
        )
        assert second.error[1] not in (NOT_A_MEMBER_MESSAGE, LEGACY_NOT_A_MEMBER_MESSAGE)
        assert env.witness.membership_rows(user_id, ORG_A) == (viewer_row,)

    def test_owner_demotion_to_viewer_is_refused_on_the_next_request(self, env: Env):
        """OWNER creates; the same row becomes VIEWER; the same header is refused by role.

        The ADMIN demotion above does not cover this: a grant remembered only for OWNER
        would pass it.
        """
        w = env.witness
        user_id, membership_id = _uid(ORG_A, Role.OWNER), _mid(ORG_A, Role.OWNER)
        owner_row = (membership_id, user_id, ORG_A, Role.OWNER.value)
        viewer_row = (membership_id, user_id, ORG_A, Role.VIEWER.value)
        table_before = w.membership_table()

        table, first, second = self._across_a_role_change(
            env, Role.OWNER, Role.VIEWER, "owner-demoted"
        )
        assert table == [
            ((owner_row,), 201, 1, 1, 1),
            ((viewer_row,), 403, 0, 0, 0),
        ]
        # Every committed membership row, ids included, is as it was but for this row's role:
        # nothing added, removed or re-keyed, in any organization.
        assert owner_row in table_before
        assert w.membership_table() == tuple(
            viewer_row if row == owner_row else row for row in table_before
        )
        _assert_created(first, org=ORG_A, name="owner-demoted-first", slug="owner-demoted-first")
        _assert_refused(
            second, status=403, code="permission_denied", message=_role_denial_message(Role.VIEWER)
        )
        assert second.error[1] not in (NOT_A_MEMBER_MESSAGE, LEGACY_NOT_A_MEMBER_MESSAGE)
        assert second.slug_rows_after == ()
        assert w.membership_rows(user_id, ORG_A) == (viewer_row,)

    @pytest.mark.parametrize("denied", DENIED)
    @pytest.mark.parametrize("held", ALLOWED)
    def test_every_allowed_role_demoted_to_any_denied_role_is_refused_on_the_next_request(
        self, env: Env, held: Role, denied: Role
    ):
        """Each allowed role, demoted to each denied role, loses authority on the next request."""
        w = env.witness
        user_id, membership_id = _uid(ORG_A, held), _mid(ORG_A, held)
        held_row = (membership_id, user_id, ORG_A, held.value)
        denied_row = (membership_id, user_id, ORG_A, denied.value)
        label = f"{held.value}-to-{denied.value.replace('_', '-')}"
        table_before = w.membership_table()

        table, first, second = self._across_a_role_change(env, held, denied, label)
        assert table == [
            ((held_row,), 201, 1, 1, 1),
            ((denied_row,), 403, 0, 0, 0),
        ]
        assert held_row in table_before
        assert w.membership_table() == tuple(
            denied_row if row == held_row else row for row in table_before
        )
        _assert_created(first, org=ORG_A, name=f"{label}-first", slug=f"{label}-first")
        _assert_refused(
            second, status=403, code="permission_denied", message=_role_denial_message(denied)
        )
        assert second.error[1] not in (NOT_A_MEMBER_MESSAGE, LEGACY_NOT_A_MEMBER_MESSAGE)
        assert second.slug_rows_after == ()

    def test_promotion_to_admin_is_admitted_on_the_next_request(self, env: Env):
        """VIEWER is refused by role; the same row becomes ADMIN; the same header creates."""
        user_id, membership_id = _uid(ORG_A, Role.VIEWER), _mid(ORG_A, Role.VIEWER)
        viewer_row = (membership_id, user_id, ORG_A, Role.VIEWER.value)
        admin_row = (membership_id, user_id, ORG_A, Role.ADMIN.value)

        table, first, second = self._across_a_role_change(env, Role.VIEWER, Role.ADMIN, "promoted")
        assert table == [
            ((viewer_row,), 403, 0, 0, 0),
            ((admin_row,), 201, 1, 1, 1),
        ]
        _assert_refused(
            first, status=403, code="permission_denied", message=_role_denial_message(Role.VIEWER)
        )
        _assert_created(second, org=ORG_A, name="promoted-second", slug="promoted-second")
        assert env.witness.membership_rows(user_id, ORG_A) == (admin_row,)

    def test_removed_membership_is_refused_on_the_next_request(self, env: Env):
        """A separate property: revoking the membership itself, not changing its role.

        It does not stand in for the two tests above. The membership gate runs before the
        role checker, so here a remembered role decision is never consulted.
        """
        w = env.witness
        user_id, membership_id = _uid(ORG_A, Role.OWNER), _mid(ORG_A, Role.OWNER)
        headers = _bearer(user_id)

        held_first = w.membership_rows(user_id, ORG_A)
        first = _attempt(env, ORG_A, headers, name="removed-first", slug="removed-first")
        _delete_membership(env, membership_id)
        held_second = w.membership_rows(user_id, ORG_A)
        second = _attempt(env, ORG_A, headers, name="removed-second", slug="removed-second")

        assert [
            (held_first, first.status, first.delta, first.total_delta),
            (held_second, second.status, second.delta, second.total_delta),
        ] == [
            (((membership_id, user_id, ORG_A, Role.OWNER.value),), 201, 1, 1),
            ((), 403, 0, 0),
        ]
        _assert_created(first, org=ORG_A, name="removed-first", slug="removed-first")
        _assert_refused(second, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)
        assert second.slug_rows_after == ()


# (label, header factory, organization, status, code, message). A factory, because tokens
# are minted at request time.
_LADDER = (
    ("anonymous", lambda: {}, ORG_A, 401, "unauthorized", MISSING_BEARER_MESSAGE),
    (
        "malformed_bearer",
        lambda: {"Authorization": "Bearer not-a-jwt"},
        ORG_A,
        401,
        "unauthorized",
        INVALID_TOKEN_MESSAGE,
    ),
    (
        # An OWNER's valid token under the wrong scheme: the scheme alone is refused.
        "non_bearer_scheme",
        lambda: {"Authorization": f"Token {create_access_token(_uid(ORG_A, Role.OWNER))}"},
        ORG_A,
        401,
        "unauthorized",
        MISSING_BEARER_MESSAGE,
    ),
    ("unknown_subject", lambda: _bearer(GHOST), ORG_A, 401, "unauthorized", UNKNOWN_USER_MESSAGE),
    (
        "non_member_existing_org",
        lambda: _bearer(OUTSIDER),
        ORG_A,
        403,
        "permission_denied",
        NOT_A_MEMBER_MESSAGE,
    ),
    (
        "non_member_missing_org",
        lambda: _bearer(OUTSIDER),
        ORG_MISSING,
        403,
        "permission_denied",
        NOT_A_MEMBER_MESSAGE,
    ),
    (
        "operator_without_membership",
        lambda: _bearer(OPERATOR),
        ORG_A,
        403,
        "permission_denied",
        NOT_A_MEMBER_MESSAGE,
    ),
    (
        "member_with_forbidden_role",
        lambda: _bearer(_uid(ORG_A, Role.VIEWER)),
        ORG_A,
        403,
        "permission_denied",
        _role_denial_message(Role.VIEWER),
    ),
    ("owner", lambda: _bearer(_uid(ORG_A, Role.OWNER)), ORG_A, 201, None, None),
    ("admin", lambda: _bearer(_uid(ORG_A, Role.ADMIN)), ORG_A, 201, None, None),
)


class TestErrorOrder:
    """401 (identity) before 403 (membership) before 403 (role) before 201.

    Every refusal is checked for a zero committed delta, in the target organization and in
    every organization.
    """

    @pytest.mark.parametrize(
        ("label", "headers", "org", "status", "code", "message"),
        _LADDER,
        ids=[rung[0] for rung in _LADDER],
    )
    def test_rung(self, env: Env, label, headers, org, status, code, message):
        name, slug = f"Rung {label}", f"rung-{label.replace('_', '-')}"
        a = _attempt(env, org, headers(), name=name, slug=slug)
        if status == 201:
            _assert_created(a, org=org, name=name, slug=slug)
        else:
            _assert_refused(a, status=status, code=code, message=message)

    def test_the_ladder_in_one_assertion(self, env: Env):
        observed, expected = {}, {}
        for label, headers, org, status, code, message in _LADDER:
            a = _attempt(
                env, org, headers(), name=f"L {label}", slug=f"l-{label.replace('_', '-')}"
            )
            observed[label] = (a.status, a.error if a.status != 201 else None, a.total_delta)
            expected[label] = (
                status,
                (code, message) if status != 201 else None,
                int(status == 201),
            )
        assert observed == expected

    def test_missing_organization_is_403_not_404_even_for_an_owner_elsewhere(self, env: Env):
        """Membership is resolved before existence, so existence is not disclosed."""
        a = _attempt(
            env, ORG_MISSING, _bearer(_uid(ORG_A, Role.OWNER)), name="Nowhere", slug="nowhere"
        )
        _assert_refused(a, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)
        assert (a.before, a.after) == (0, 0)
        assert not env.witness.organization_exists(ORG_MISSING)

    def test_anonymous_on_a_missing_organization_is_still_401(self, env: Env):
        """Identity is resolved first: 401 outranks both membership and existence."""
        a = _attempt(env, ORG_MISSING, {}, name="Anon Missing", slug="anon-missing")
        _assert_refused(a, status=401, code="unauthorized", message=MISSING_BEARER_MESSAGE)

    def test_operator_flag_confers_no_organization_authority(self, env: Env):
        """``is_operator`` is a platform attribute; it is not membership and not a role."""
        assert env.witness.is_operator(OPERATOR) is True
        assert env.witness.membership_role(OPERATOR, ORG_A) is None
        for org in (ORG_A, ORG_B, ORG_MISSING):
            a = _attempt(env, org, _bearer(OPERATOR), name=f"Op {org}", slug=f"op-{org}")
            _assert_refused(a, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)


_APP_DIR = Path(__file__).resolve().parents[1]


def _app_call_site() -> str:
    """The innermost non-test frame of the ``app`` package on the stack, for the record."""
    for frame in reversed(traceback.extract_stack()):
        path = Path(frame.filename).resolve()
        if path.is_relative_to(_APP_DIR) and not path.is_relative_to(_APP_DIR / "tests"):
            return f"{path.relative_to(_APP_DIR.parent)}:{frame.lineno} in {frame.name}"
    return "test code"


class _ChannelRefused(AssertionError):
    """Raised at a refused channel. Code under test may swallow it; the record remains."""


class _RefusingSessionFactory:
    """Stands in for ``app.db.session.SessionLocal``: every use is recorded, then refused."""

    def __init__(self, refuse: Callable[[str], None]) -> None:
        self._refuse = refuse

    def __call__(self, *args, **kwargs):
        self._refuse("app.db.session.SessionLocal()")

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        self._refuse(f"app.db.session.SessionLocal.{name}")


class _RawSqliteDoor:
    """A ``sys.addaudithook`` hook on ``sqlite3.connect`` -- the driver, below SQLAlchemy.

    An audit hook cannot be removed, so this one is installed once per process, on first use,
    and does nothing unless a guard has opened a window: then it refuses any database but the
    test's own file, which the request's and the witness's engines both use.
    """

    installed = False
    window: tuple[Path, Callable[[str], None]] | None = None

    @classmethod
    def hook(cls, event: str, args: tuple) -> None:
        if event != "sqlite3.connect" or cls.window is None:
            return
        allowed, refuse = cls.window
        if Path(os.fsdecode(args[0])).resolve() != allowed:
            refuse("raw sqlite3.connect")


@contextmanager
def _production_channel_guard(env: Env) -> Iterator[list[str]]:
    """Refuse, and record, every database channel but the request's and the witness's.

    The witness reads the request's database, so a write through a *second* channel -- the
    production session factory or any other engine -- would not show in its counts. While
    the guard is open:

    * ``app.db.session.SessionLocal`` is a refusing factory, so a call-time lookup of the
      production factory is caught before any session exists;
    * a new DBAPI connection for any other engine -- production's, one reached through an
      alias bound at import, one created on the spot -- is refused in ``do_connect``, before
      the driver opens anything;
    * a connection already pooled by any other engine is refused at checkout, before use;
    * a raw ``sqlite3.connect`` to any file but the test's own is refused by
      :class:`_RawSqliteDoor` before the file is opened;
    * a statement on a connection that is already open -- one another engine checked out
      before the window, at import say, which never passes the doors above again -- is
      refused in ``before_cursor_execute``, before the driver executes it. The request's
      and the witness's connections pass by *identity* -- the ``Engine`` object and that
      engine's own pool, both -- never by URL or file: a second engine on the request's own
      file is refused too.

    Each refusal is appended to the yielded list before it is raised, so code that swallows
    the exception still leaves the evidence. Everything is restored on exit.

    Windows nest: ``_attempt`` opens one around every request, inside any window its test
    already opened. The inner window swaps in its own ``SessionLocal`` stand-in and raw
    ``sqlite3`` window and puts the outer's back on exit; both windows' listeners stay live,
    so a refusal lands in one of the two records -- which one depends on the door -- and each
    opener asserts its own record empty.
    """
    used: list[str] = []
    engines = (env.request_engine, env.witness.engine)
    pools = (env.request_engine.pool, env.witness.engine.pool)
    dialects = (env.request_engine.dialect, env.witness.engine.dialect)
    own_file = Path(env.request_engine.url.database).resolve()
    assert Path(env.witness.engine.url.database).resolve() == own_file

    def _refuse(channel: str) -> None:
        used.append(f"{channel} at {_app_call_site()}")
        raise _ChannelRefused(f"{channel} is not a channel of this request")

    def _on_do_connect(dialect, _record, _cargs, _cparams):
        if not any(dialect is allowed for allowed in dialects):
            _refuse(f"new {dialect.name} connection")

    def _on_checkout(_dbapi_connection, _record, proxy):
        if not any(proxy._pool is allowed for allowed in pools):
            _refuse("pooled connection checkout")

    def _on_cursor_execute(conn, _cursor, statement, _parameters, _context, _executemany):
        # Both identities, so re-pointing ``conn.engine`` alone cannot borrow admission: the
        # checked-out connection must also come from that engine's own pool.
        if not any(
            conn.engine is allowed and conn.connection._pool is allowed.pool for allowed in engines
        ):
            verb = statement.split(None, 1)[0].upper() if statement.strip() else "statement"
            _refuse(f"{verb} on another engine's open connection")

    if not _RawSqliteDoor.installed:
        sys.addaudithook(_RawSqliteDoor.hook)
        _RawSqliteDoor.installed = True

    factory = db_session.SessionLocal
    had_dialect_events = Dialect.__dict__["_has_events"]
    had_engine_events = Engine.__dict__["_has_events"]
    outer_window = _RawSqliteDoor.window
    event.listen(Dialect, "do_connect", _on_do_connect)
    event.listen(Pool, "checkout", _on_checkout)
    # At class level, so it also reaches connections opened before this line: every execute
    # consults the Engine class's current listeners, not a copy taken at connect.
    event.listen(Engine, "before_cursor_execute", _on_cursor_execute)
    db_session.SessionLocal = _RefusingSessionFactory(_refuse)
    _RawSqliteDoor.window = (own_file, _refuse)
    try:
        yield used
    finally:
        _RawSqliteDoor.window = outer_window
        db_session.SessionLocal = factory
        event.remove(Engine, "before_cursor_execute", _on_cursor_execute)
        event.remove(Pool, "checkout", _on_checkout)
        event.remove(Dialect, "do_connect", _on_do_connect)
        # Listening at class level set this flag on the base Dialect; put it back.
        Dialect._has_events = had_dialect_events
        # ... and this one on the base Engine.
        Engine._has_events = had_engine_events


class TestNoProductionSessionEscape:
    """A denial writes nothing through a second channel the witness cannot see.

    ``get_db`` is overridden, so the witness observes the request's database only. A role
    checker that refused correctly while committing through the production ``SessionLocal``
    (or any other engine) would pass every committed-state assertion in this module. Under
    the guard such a write is refused before it reaches a database, and recorded even if
    swallowed.
    """

    def test_the_guard_is_live_and_restored(self, env: Env, tmp_path: Path):
        pooled = _sqlite_file_engine(tmp_path / "foreign-pooled.db")
        with pooled.connect() as conn:  # pooled BEFORE the guard opens
            conn.execute(text("select 1"))
        fresh_file = tmp_path / "foreign-fresh.db"
        fresh = _sqlite_file_engine(fresh_file)
        raw_file = tmp_path / "foreign-raw.db"
        # Opened BEFORE the guard, as a connection checked out at import would be: one on a
        # foreign file, and one on the request's own file through a second Engine object.
        early_file = tmp_path / "foreign-early.db"
        early = _sqlite_file_engine(early_file)
        early_conn = early.connect()
        early_conn.execute(text("create table probe (v text)"))
        early_conn.execute(text("insert into probe (v) values ('seed')"))
        early_conn.commit()
        early_bytes = early_file.read_bytes()
        twin = _sqlite_file_engine(Path(env.request_engine.url.database))
        twin_conn = twin.connect()
        dialect_events = Dialect.__dict__["_has_events"]
        engine_events = Engine.__dict__["_has_events"]
        try:
            with _production_channel_guard(env) as used:
                # The request's and the witness's channels stay open.
                assert env.witness.workspace_count(ORG_A) == 1
                with env.request_factory() as s:
                    assert s.scalar(text("select 1")) == 1
                assert used == []

                # A swallowed refusal still leaves its record.
                try:
                    db_session.SessionLocal()
                except Exception:
                    pass
                with pytest.raises(_ChannelRefused):
                    db_session.SessionLocal.begin()
                # A new connection is refused before the driver runs: no file appears.
                with pytest.raises(_ChannelRefused):
                    fresh.connect()
                assert not fresh_file.exists()
                # A connection another engine already pooled is refused at checkout.
                with pytest.raises(_ChannelRefused):
                    pooled.connect()
                # The driver itself, bypassing SQLAlchemy: refused before the file is opened.
                with pytest.raises(_ChannelRefused):
                    sqlite3.connect(raw_file)
                assert not raw_file.exists()
                assert [entry.split(" at ")[0] for entry in used] == [
                    "app.db.session.SessionLocal()",
                    "app.db.session.SessionLocal.begin",
                    "new sqlite connection",
                    "pooled connection checkout",
                    "raw sqlite3.connect",
                ]
                # Only once every door is shown closed: the production engine itself.
                with pytest.raises(_ChannelRefused):
                    db_session.engine.connect()
                assert len(used) == 6
                # A connection opened before the window never reaches do_connect or checkout
                # again. Its statement is refused before the driver runs it, and recorded even
                # when the refusal is swallowed and the commit still follows: nothing lands.
                try:
                    early_conn.execute(text("insert into probe (v) values ('in-window')"))
                    early_conn.commit()
                except Exception:
                    pass
                assert early_file.read_bytes() == early_bytes
                # Allowed by Engine identity, never by file: a second engine on the request's
                # own file is refused, while the request's and the witness's still pass.
                with pytest.raises(_ChannelRefused):
                    twin_conn.execute(
                        Workspace.__table__.insert().values(
                            id="ws-wsc-twin", organization_id=ORG_A, name="Twin", slug="twin"
                        )
                    )
                assert env.witness.workspace_count(ORG_A) == 1
                with env.request_factory() as s:
                    assert s.scalar(text("select 1")) == 1
                assert [entry.split(" at ")[0] for entry in used[6:]] == [
                    "INSERT on another engine's open connection",
                    "INSERT on another engine's open connection",
                ]
                # Engine AND pool: with only ``.engine`` re-pointed at the request's, the early
                # connection -- still checked out of its own engine's pool -- is refused too.
                early_engine = early_conn.engine
                early_conn.engine = env.request_engine
                try:
                    early_conn.execute(text("insert into probe (v) values ('forged')"))
                    early_conn.commit()
                except Exception:
                    pass
                finally:
                    early_conn.engine = early_engine
                assert early_file.read_bytes() == early_bytes
                assert [entry.split(" at ")[0] for entry in used[8:]] == [
                    "INSERT on another engine's open connection",
                ]
            # Everything is restored on exit; the audit hook stays installed but inert.
            assert db_session.SessionLocal is SessionLocal
            assert Dialect.__dict__["_has_events"] is dialect_events
            assert Engine.__dict__["_has_events"] is engine_events
            assert _RawSqliteDoor.installed and _RawSqliteDoor.window is None
            # Inert once closed, and removed rather than merely masked by the restored flag:
            # even with the flag forced on, nothing refuses the early connection's next write.
            Engine._has_events = True
            try:
                early_conn.execute(text("insert into probe (v) values ('after')"))
            finally:
                Engine._has_events = engine_events
            early_conn.commit()
            # It lands; the refused one never did.
            assert early_conn.execute(text("select v from probe")).scalars().all() == [
                "seed",
                "after",
            ]
            # The do_connect door likewise: with the Dialect flag forced on, a brand-new
            # connection of a foreign engine opens -- no listener of the guard's is left.
            late = _sqlite_file_engine(tmp_path / "foreign-late.db")
            Dialect._has_events = True
            try:
                with late.connect() as conn:
                    assert conn.execute(text("select 1")).scalar() == 1
            finally:
                Dialect._has_events = dialect_events
                late.dispose()
            for engine in (pooled, fresh):
                with engine.connect() as conn:
                    assert conn.execute(text("select 1")).scalar() == 1
            sqlite3.connect(raw_file).close()
            assert fresh_file.exists() and raw_file.exists()
            assert len(used) == 9
        finally:
            early_conn.close()
            twin_conn.close()
            pooled.dispose()
            fresh.dispose()
            early.dispose()
            twin.dispose()

    def test_a_nested_window_restores_the_outer_one(self, env: Env, tmp_path: Path):
        """What the tests below rely on: ``_attempt``'s window inside one of their own.

        Each door refuses inside the inner window, and still refuses once it has closed and
        only the outer is open -- the inner's exit leaves the outer's stand-in, raw
        ``sqlite3`` window, listeners and class flags in place.
        """
        foreign = _sqlite_file_engine(tmp_path / "foreign-nested.db")
        raw_file = tmp_path / "foreign-nested-raw.db"
        early = _sqlite_file_engine(tmp_path / "foreign-nested-early.db")
        early_conn = early.connect()  # opened BEFORE either window
        early_conn.execute(text("select 1"))
        doors = (
            ("app.db.session.SessionLocal()", lambda: db_session.SessionLocal()),
            ("new sqlite connection", foreign.connect),
            ("raw sqlite3.connect", lambda: sqlite3.connect(raw_file)),
            (
                "SELECT on another engine's open connection",
                lambda: early_conn.execute(text("select 1")),
            ),
        )
        dialect_events = Dialect.__dict__["_has_events"]
        engine_events = Engine.__dict__["_has_events"]
        try:
            with _production_channel_guard(env) as outer:
                outer_factory, outer_door = db_session.SessionLocal, _RawSqliteDoor.window
                with _production_channel_guard(env) as inner:
                    assert db_session.SessionLocal is not outer_factory
                    assert _RawSqliteDoor.window is not outer_door
                    for _, door in doors:
                        with pytest.raises(_ChannelRefused):
                            door()
                    assert env.witness.workspace_count(ORG_A) == 1
                    with env.request_factory() as s:
                        assert s.scalar(text("select 1")) == 1
                during = list(outer)
                assert db_session.SessionLocal is outer_factory
                assert _RawSqliteDoor.window is outer_door
                for _, door in doors:
                    with pytest.raises(_ChannelRefused):
                        door()
            assert sorted(entry.split(" at ")[0] for entry in inner + during) == sorted(
                label for label, _ in doors
            )
            assert [entry.split(" at ")[0] for entry in outer[len(during) :]] == [
                label for label, _ in doors
            ]
            assert db_session.SessionLocal is SessionLocal
            assert _RawSqliteDoor.window is None
            assert Dialect.__dict__["_has_events"] is dialect_events
            assert Engine.__dict__["_has_events"] is engine_events
            assert not raw_file.exists()
        finally:
            early_conn.close()
            foreign.dispose()
            early.dispose()

    def test_every_request_is_sent_inside_attempts_guard_window(self):
        """Structural: no request in this module can bypass the guard ``_attempt`` opens.

        Every ``.post`` reference, and every ``.request`` or ``.stream`` call, must sit inside
        a ``with _production_channel_guard(...)`` block that is itself a top-level statement
        of ``_attempt`` -- so it runs on every call, for every caller.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def is_guard_window(node: ast.AST) -> bool:
            return isinstance(node, ast.With) and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "_production_channel_guard"
                for item in node.items
            )

        def in_attempts_window(node: ast.AST) -> bool:
            while node in parents:
                node = parents[node]
                if is_guard_window(node):
                    owner = parents.get(node)
                    return isinstance(owner, ast.FunctionDef) and owner.name == "_attempt"
            return False

        sends = [
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.Attribute) and node.attr == "post")
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("request", "stream")
            )
        ]
        assert sends, "found no request at all: the scan is not seeing this module"
        outside = [node.lineno for node in sends if not in_attempts_window(node)]
        assert outside == [], f"requests sent outside _attempt's guard window, at lines {outside}"

    @pytest.mark.parametrize("role", DENIED)
    def test_a_denied_member_writes_through_no_other_channel(self, env: Env, role: Role):
        """A valid member of an existing organization, a valid body, a real POST."""
        user_id = _uid(ORG_A, role)
        assert env.witness.membership_role(user_id, ORG_A) == role.value
        assert env.witness.organization_exists(ORG_A)
        name, slug = f"Escape {role.value}", f"escape-{role.value.replace('_', '-')}"
        with _production_channel_guard(env) as used:
            a = _attempt(env, ORG_A, _bearer(user_id), name=name, slug=slug)
        assert used == []
        _assert_refused(a, status=403, code="permission_denied", message=_role_denial_message(role))
        assert a.slug_rows_after == ()

    @pytest.mark.parametrize("role", ALLOWED)
    def test_an_allowed_member_creates_through_the_request_channel_only(self, env: Env, role: Role):
        name, slug = f"Guarded {role.value}", f"guarded-{role.value}"
        with _production_channel_guard(env) as used:
            a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, role)), name=name, slug=slug)
        assert used == []
        _assert_created(a, org=ORG_A, name=name, slug=slug)
        assert a.payload["id"] in env.witness.workspace_ids(ORG_A)

    def test_no_rung_of_the_ladder_opens_another_channel(self, env: Env):
        observed = {}
        with _production_channel_guard(env) as used:
            for label, headers, org, *_ in _LADDER:
                slug = f"guard-{label.replace('_', '-')}"
                observed[label] = _attempt(env, org, headers(), name=slug, slug=slug).status
        assert used == []
        assert observed == {rung[0]: rung[3] for rung in _LADDER}


class TestOperatorFlagDoesNotAlterTheRoleDecision:
    """``is_operator`` neither waives nor replaces the role check for an organization member.

    The operator in ``TestErrorOrder`` is a member of nothing, so it always stops at
    membership and says nothing about the role guard. These operators ARE members of the path
    organization, one per role. A checker that exempted operators would let every denied one
    through here; one that excluded them would refuse the allowed ones.
    """

    @pytest.mark.parametrize("role", DENIED)
    def test_operator_holding_a_denied_role_is_refused_by_the_role_guard(
        self, env: Env, role: Role
    ):
        user_id = _op_uid(role)
        assert env.witness.is_operator(user_id) is True
        assert env.witness.membership_role(user_id, ORG_A) == role.value

        name, slug = f"Op Denied {role.value}", f"op-denied-{role.value.replace('_', '-')}"
        a = _attempt(env, ORG_A, _bearer(user_id), name=name, slug=slug)
        _assert_refused(a, status=403, code="permission_denied", message=_role_denial_message(role))
        assert a.error[1] not in (NOT_A_MEMBER_MESSAGE, LEGACY_NOT_A_MEMBER_MESSAGE)
        assert a.slug_rows_after == ()

    @pytest.mark.parametrize("role", ALLOWED)
    def test_operator_holding_an_allowed_role_creates(self, env: Env, role: Role):
        user_id = _op_uid(role)
        assert env.witness.is_operator(user_id) is True
        name, slug = f"Op Allowed {role.value}", f"op-allowed-{role.value}"
        a = _attempt(env, ORG_A, _bearer(user_id), name=name, slug=slug)
        _assert_created(a, org=ORG_A, name=name, slug=slug)

    def test_operator_matrix_equals_the_non_operator_matrix(self, env: Env):
        """Same six roles, same organization: the flag changes no outcome."""
        observed = {}
        for role in ALL_ROLES:
            for label, user_id in (("member", _uid(ORG_A, role)), ("operator", _op_uid(role))):
                slug = f"eq-{label}-{role.value.replace('_', '-')}"
                a = _attempt(env, ORG_A, _bearer(user_id), name=slug, slug=slug)
                observed[(label, role)] = (a.status, a.delta, a.total_delta)
        for role in ALL_ROLES:
            assert observed[("operator", role)] == observed[("member", role)], role
        assert {observed[("operator", role)] for role in ALLOWED} == {(201, 1, 1)}
        assert {observed[("operator", role)] for role in DENIED} == {(403, 0, 0)}


class TestCrossOrganization:
    """An allowed role in organization A authorizes nothing in organization B."""

    @pytest.mark.parametrize("role", ALLOWED)
    def test_allowed_role_in_a_is_refused_in_b_and_writes_nowhere(self, env: Env, role: Role):
        user_id = _uid(ORG_A, role)
        assert env.witness.membership_role(user_id, ORG_B) is None
        a_before = env.witness.workspace_count(ORG_A)

        away = _attempt(env, ORG_B, _bearer(user_id), name="Cross Away", slug="cross-away")
        _assert_refused(away, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)
        assert env.witness.workspace_count(ORG_A) == a_before  # ORG_A delta 0 too

        home = _attempt(env, ORG_A, _bearer(user_id), name="Cross Home", slug="cross-home")
        _assert_created(home, org=ORG_A, name="Cross Home", slug="cross-home")
        assert env.witness.workspace_count(ORG_B) == 1

    def test_org_b_owner_creates_in_b_and_is_refused_in_a(self, env: Env):
        user_id = _uid(ORG_B, Role.OWNER)
        own = _attempt(env, ORG_B, _bearer(user_id), name="B Own", slug="b-own")
        foreign = _attempt(env, ORG_A, _bearer(user_id), name="B Foreign", slug="b-foreign")
        _assert_created(own, org=ORG_B, name="B Own", slug="b-own")
        _assert_refused(foreign, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)


class TestAuthorizedOrganizationIsTheWrittenOrganization:
    """Multi-membership users: the role that counts is the one held in the path organization.

    X is OWNER in A and VIEWER in B; Y is VIEWER in A and ADMIN in B. A route that read the
    role from one organization and wrote into another would let X create in B or Y in A.
    """

    def test_x_is_refused_in_b_by_role_and_writes_nothing(self, env: Env):
        a = _attempt(env, ORG_B, _bearer(MULTI_X), name="X In B", slug="x-in-b")
        _assert_refused(
            a, status=403, code="permission_denied", message=_role_denial_message(Role.VIEWER)
        )
        assert a.slug_rows_after == ()

    def test_x_creates_in_a_only(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(MULTI_X), name="X In A", slug="x-in-a")
        _assert_created(a, org=ORG_A, name="X In A", slug="x-in-a")
        assert [row[0] for row in a.slug_rows_after] == [ORG_A]
        assert env.witness.workspace_count(ORG_B) == 1

    def test_y_is_refused_in_a_by_role_and_writes_nothing(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(MULTI_Y), name="Y In A", slug="y-in-a")
        _assert_refused(
            a, status=403, code="permission_denied", message=_role_denial_message(Role.VIEWER)
        )
        assert a.slug_rows_after == ()

    def test_y_creates_in_b_only(self, env: Env):
        a = _attempt(env, ORG_B, _bearer(MULTI_Y), name="Y In B", slug="y-in-b")
        _assert_created(a, org=ORG_B, name="Y In B", slug="y-in-b")
        assert [row[0] for row in a.slug_rows_after] == [ORG_B]
        assert env.witness.workspace_count(ORG_A) == 1

    def test_all_four_in_sequence(self, env: Env):
        """Response org == path org == committed row org, and nothing lands anywhere else."""
        sequence = (
            (MULTI_X, ORG_B, "Seq X B", "seq-x-b", 403),
            (MULTI_X, ORG_A, "Seq X A", "seq-x-a", 201),
            (MULTI_Y, ORG_A, "Seq Y A", "seq-y-a", 403),
            (MULTI_Y, ORG_B, "Seq Y B", "seq-y-b", 201),
        )
        for user_id, org, name, slug, status in sequence:
            a = _attempt(env, org, _bearer(user_id), name=name, slug=slug)
            assert a.status == status, (user_id, org, a.payload)
            if status == 201:
                assert a.payload["organization_id"] == org
                assert [row[0] for row in a.slug_rows_after] == [org]
                assert a.slug_rows_after[0][1] == a.payload["id"]
            else:
                assert a.error[1] == _role_denial_message(Role.VIEWER)
                assert a.slug_rows_after == ()
        assert env.witness.workspace_count(ORG_A) == 2
        assert env.witness.workspace_count(ORG_B) == 2
        assert env.witness.rows_with_slug("seq-x-b") == env.witness.rows_with_slug("seq-y-a") == ()


class TestClientSuppliedOrganizationIdentityIsInert:
    """An ``organization_id`` in the query string or body decides nothing.

    ``TestRouteContract`` shows the route declares no such input; these show behaviourally
    that one smuggled in can neither redirect the write nor authorize another organization.
    """

    def test_query_organization_id_does_not_redirect_the_write(self, env: Env):
        b_before = env.witness.workspace_count(ORG_B)
        a = _attempt(
            env,
            ORG_A,
            _bearer(_uid(ORG_A, Role.OWNER)),
            name="Query Pollute A",
            slug="query-pollute-a",
            params={"organization_id": ORG_B},
        )
        assert f"organization_id={ORG_B}" in a.url  # the pollution was really sent
        _assert_created(a, org=ORG_A, name="Query Pollute A", slug="query-pollute-a")
        assert [row[0] for row in a.slug_rows_after] == [ORG_A]
        assert env.witness.workspace_count(ORG_B) == b_before

    def test_query_organization_id_does_not_authorize_another_organization(self, env: Env):
        a_before = env.witness.workspace_count(ORG_A)
        a = _attempt(
            env,
            ORG_B,
            _bearer(_uid(ORG_A, Role.OWNER)),
            name="Query Pollute B",
            slug="query-pollute-b",
            params={"organization_id": ORG_A},
        )
        assert f"organization_id={ORG_A}" in a.url
        _assert_refused(a, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)
        assert env.witness.workspace_count(ORG_A) == a_before
        assert a.slug_rows_after == ()

    def test_body_organization_id_does_not_redirect_the_write(self, env: Env):
        b_before = env.witness.workspace_count(ORG_B)
        a = _attempt(
            env,
            ORG_A,
            _bearer(_uid(ORG_A, Role.OWNER)),
            body={"name": "Body Pollute", "organization_id": ORG_B},
            slug="body-pollute",
        )
        _assert_created(a, org=ORG_A, name="Body Pollute", slug="body-pollute")
        assert [row[0] for row in a.slug_rows_after] == [ORG_A]
        assert env.witness.workspace_count(ORG_B) == b_before


class TestReadPreservation:
    """The GET routes keep membership-only authorization and their legacy message."""

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_every_role_can_still_list_workspaces(self, env: Env, role: Role):
        r = _get(env, _create_url(ORG_A), _bearer(_uid(ORG_A, role)))
        assert r.status_code == 200, r.text
        assert {row["id"] for row in r.json()} == env.witness.workspace_ids(ORG_A) == {WS_A}
        assert {row["organization_id"] for row in r.json()} == {ORG_A}

    @pytest.mark.parametrize("role", (Role.VIEWER, Role.REVIEWER))
    def test_listing_includes_a_workspace_created_by_an_owner(self, env: Env, role: Role):
        created = _attempt(
            env, ORG_A, _bearer(_uid(ORG_A, Role.OWNER)), name="Listed", slug="listed"
        )
        _assert_created(created, org=ORG_A, name="Listed", slug="listed")
        r = _get(env, _create_url(ORG_A), _bearer(_uid(ORG_A, role)))
        assert r.status_code == 200
        assert {row["id"] for row in r.json()} == {WS_A, created.payload["id"]}

    def test_non_member_list_keeps_the_legacy_message(self, env: Env):
        r = _get(env, _create_url(ORG_A), _bearer(OUTSIDER))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "permission_denied"
        assert r.json()["error"]["message"] == LEGACY_NOT_A_MEMBER_MESSAGE

    def test_cross_org_list_keeps_the_legacy_message(self, env: Env):
        r = _get(env, _create_url(ORG_B), _bearer(_uid(ORG_A, Role.OWNER)))
        assert r.status_code == 403
        assert r.json()["error"]["message"] == LEGACY_NOT_A_MEMBER_MESSAGE

    def test_post_and_get_non_member_messages_differ(self, env: Env):
        """The disclosed normalization: POST uses the shared seam, GET the legacy helper."""
        headers = _bearer(OUTSIDER)
        post = _attempt(env, ORG_A, headers, name="Outsider Post", slug="outsider-post")
        get = _get(env, _create_url(ORG_A), headers)
        assert post.status == get.status_code == 403
        assert post.error[1] == NOT_A_MEMBER_MESSAGE
        assert get.json()["error"]["message"] == LEGACY_NOT_A_MEMBER_MESSAGE
        assert NOT_A_MEMBER_MESSAGE != LEGACY_NOT_A_MEMBER_MESSAGE

    @pytest.mark.parametrize("role", (Role.VIEWER, Role.REVIEWER))
    def test_single_workspace_read_is_unchanged(self, env: Env, role: Role):
        r = _get(env, f"{API}/workspaces/{WS_A}", _bearer(_uid(ORG_A, role)))
        assert r.status_code == 200, r.text
        assert (r.json()["id"], r.json()["organization_id"]) == (WS_A, ORG_A)


class TestCreationFields:
    """Everything the handler did before, it still does -- only who may call it changed."""

    def test_name_round_trips_and_slug_is_normalized(self, env: Env):
        name = "  Growth Team!! 2026 "
        a = _attempt(
            env, ORG_A, _bearer(_uid(ORG_A, Role.OWNER)), name=name, slug="growth-team-2026"
        )
        _assert_created(a, org=ORG_A, name=name, slug="growth-team-2026")

    def test_name_without_alphanumerics_falls_back_to_workspace(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, Role.ADMIN)), name="!!!", slug="workspace")
        _assert_created(a, org=ORG_A, name="!!!", slug="workspace")

    def test_first_collision_gets_the_length_suffix(self, env: Env):
        headers = _bearer(_uid(ORG_A, Role.OWNER))
        first = _attempt(env, ORG_A, headers, name="Launch Pad", slug="launch-pad")
        _assert_created(first, org=ORG_A, name="Launch Pad", slug="launch-pad")
        # f"{slug}-{len(slug)}" -- len("launch-pad") == 10.
        second = _attempt(env, ORG_A, headers, name="Launch Pad", slug="launch-pad-10")
        _assert_created(second, org=ORG_A, name="Launch Pad", slug="launch-pad-10")
        assert env.witness.rows_with_slug("launch-pad") == first.slug_rows_after

    def test_collision_check_is_scoped_to_the_authorized_organization(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, Role.OWNER)), name="Shared", slug="shared")
        b = _attempt(env, ORG_B, _bearer(_uid(ORG_B, Role.OWNER)), name="Shared", slug="shared")
        _assert_created(a, org=ORG_A, name="Shared", slug="shared")
        _assert_created(b, org=ORG_B, name="Shared", slug="shared")
        assert [row[0] for row in env.witness.rows_with_slug("shared")] == [ORG_A, ORG_B]

    def test_response_is_the_workspace_out_shape(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, Role.ADMIN)), name="Shape", slug="shape")
        _assert_created(a, org=ORG_A, name="Shape", slug="shape")
        assert set(a.payload) == set(WorkspaceOut.model_fields)
        assert a.payload["onboarding_completed"] is False
        assert len(a.payload["id"]) <= _ID_MAX
        datetime.fromisoformat(a.payload["created_at"])


# The create operation exactly as committed in apps/api/openapi.json at the base commit.
_BASE_CREATE_OPERATION = {
    "operationId": "create_workspace_api_v1_organizations__organization_id__workspaces_post",
    "parameters": [
        {
            "in": "path",
            "name": "organization_id",
            "required": True,
            "schema": {"title": "Organization Id", "type": "string"},
        },
        {
            "in": "header",
            "name": "authorization",
            "required": False,
            "schema": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Authorization"},
        },
    ],
    "requestBody": {
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/WorkspaceCreate"}}
        },
        "required": True,
    },
    "responses": {
        "201": {
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/WorkspaceOut"}}
            },
            "description": "Successful Response",
        },
        "422": {
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}
            },
            "description": "Validation Error",
        },
    },
    "summary": "Create Workspace",
    "tags": ["organizations"],
}


class TestRouteContract:
    """The public contract is unchanged: the dependency adds no client-supplied input."""

    @staticmethod
    def _operation() -> dict:
        return app.openapi()["paths"][CREATE_PATH]["post"]

    def test_parameters_are_exactly_the_path_organization_and_authorization_header(self):
        params = [(p["in"], p["name"], p["required"]) for p in self._operation()["parameters"]]
        assert params == [("path", "organization_id", True), ("header", "authorization", False)]

    def test_no_identity_or_role_input_is_exposed(self):
        names = {p["name"] for p in self._operation()["parameters"]}
        for forbidden in ("workspace_id", "role", "membership", "user_id"):
            assert forbidden not in names

    def test_request_and_response_models(self):
        op = self._operation()
        body_schema = op["requestBody"]["content"]["application/json"]["schema"]
        assert body_schema == {"$ref": "#/components/schemas/WorkspaceCreate"}
        assert op["requestBody"]["required"] is True
        ok_schema = op["responses"]["201"]["content"]["application/json"]["schema"]
        assert ok_schema == {"$ref": "#/components/schemas/WorkspaceOut"}
        assert "200" not in op["responses"]

    def test_operation_is_identical_to_base(self):
        assert self._operation() == _BASE_CREATE_OPERATION


class TestBodyValidationOrdering:
    """Measured, not assumed: authorization now runs before body validation.

    Base route: membership was checked in the handler, after body validation, so OWNER,
    VIEWER and a non-member all received 422 for ``{"name": ""}``. Now the dependency runs
    first; only a caller who clears authorization reaches body validation.
    """

    def test_owner_with_invalid_body_gets_422(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, Role.OWNER)), body={"name": ""})
        _assert_refused(a, status=422, code="validation_error", message="Request validation failed")

    def test_forbidden_role_with_invalid_body_gets_the_role_denial(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(_uid(ORG_A, Role.VIEWER)), body={"name": ""})
        _assert_refused(
            a, status=403, code="permission_denied", message=_role_denial_message(Role.VIEWER)
        )

    def test_non_member_with_invalid_body_gets_the_membership_denial(self, env: Env):
        a = _attempt(env, ORG_A, _bearer(OUTSIDER), body={"name": ""})
        _assert_refused(a, status=403, code="permission_denied", message=NOT_A_MEMBER_MESSAGE)

    def test_anonymous_with_invalid_body_gets_401(self, env: Env):
        a = _attempt(env, ORG_A, {}, body={"name": ""})
        _assert_refused(a, status=401, code="unauthorized", message=MISSING_BEARER_MESSAGE)

    def test_undecodable_json_is_still_rejected_before_authorization(self, env: Env):
        """FastAPI decodes the body before resolving dependencies; pinned, not endorsed."""
        a = _attempt(env, ORG_A, _bearer(OUTSIDER), content="{not json")
        assert a.status == 422, a.payload
        assert a.error[0] == "validation_error"
        assert (a.delta, a.total_delta) == (0, 0)
