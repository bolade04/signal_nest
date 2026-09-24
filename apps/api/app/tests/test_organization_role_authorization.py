"""6B-2: dependency-level proof for organization-scoped exact authorization (P6-AUTH-7).

Workspace creation (``POST /organizations/{organization_id}/workspaces``) is an
organization-administration action with no workspace to resolve, so the 6B-1 primitive
``require_exact_roles`` cannot guard it: it is built on ``get_tenant_context``, which
takes a ``workspace_id``. 6B-2 adds an organization-scoped seam to
``app.auth.dependencies``:

    get_organization_context(organization_id)   -- authentication, then membership via
                                                   the shared ``_membership``, then
                                                   organization existence; the role is
                                                   read from the persisted membership.
    require_exact_organization_roles(*allowed)  -- exact membership of ``ctx.role`` in
                                                   ``allowed``; no rank, no floor.

This module calls both directly, against a real temporary SQLite database with foreign
keys enforced, so a failure localises to the primitive rather than to routing or the
test harness. It proves:

* the role is derived from the persisted membership of the *requested* organization --
  for all six roles, for two users who each hold different roles in two organizations
  (both directions), and for a role rewritten in the database between two resolutions;
* membership is checked before existence: a non-member receives the shared membership
  403 for an existing and a nonexistent organization alike, and the organization lookup
  is never performed without a membership, so there is no existence oracle;
* the post-membership 404 limb, pinned with a stub session. A membership whose
  organization is absent is exactly what the foreign key refuses to store (a fixture
  test shows the refusal), so the stub is the only way to reach that branch;
* exact-set semantics: a non-contiguous probe policy, the production (OWNER, ADMIN)
  policy, every single-role policy -- ``(ADMIN,)`` denying OWNER above all --,
  construction-time rejection of an empty policy, harmless duplicates, and context
  identity;
* the seam's shape: the checker depends on ``get_organization_context``, and a route
  guarded by it publishes only the path ``organization_id`` and the ``authorization``
  header -- no ``workspace_id`` and no client-supplied role.

Why the non-contiguous probe is mandatory: the production policy (OWNER, ADMIN) is
contiguous at the top of the rank order, so over the six roles it admits exactly the
same set as ``rank >= ADMIN``. On its own it cannot tell an exact-set checker from a rank
floor. ``(OWNER, COMPLIANCE_REVIEWER)`` -- a policy no production route mounts -- is not
closed upward in rank, so no rank threshold yields it, and it separates
COMPLIANCE_REVIEWER from REVIEWER, which share a rank.

Why single-role policies are probed as well: a checker that applied a rank floor only
when every named role ranks at or above ADMIN, and exact membership otherwise, would pass
both policies above -- (OWNER, ADMIN) *is* the ADMIN floor, and the probe names a role
below ADMIN, so it would be judged exactly. ``(ADMIN,)`` separates the two: OWNER
outranks ADMIN, is not named, and must be denied. Each of the six single-role policies
is checked against all six roles, so none can admit anything but its own role unnoticed.

The HTTP-boundary proof for the create route lives in
``test_workspace_creation_authorization``. Role assignment does not exist yet
(P6-AUTH-1 is open): the only membership writers outside tests, ``register()`` and the
demo seed, assign OWNER. Every non-OWNER membership here is a fixture row written
directly, not the product of a supported flow.
"""

from __future__ import annotations

import dataclasses
import inspect
import typing
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.auth.dependencies as deps
from app.auth.dependencies import (
    _ROLE_RANK,
    OrganizationContext,
    get_current_user,
    get_organization_context,
    get_tenant_context,
    require_exact_organization_roles,
    require_exact_roles,
)
from app.core.enums import Role
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.models import Base
from app.db.session import get_db
from app.organizations.models import Organization, OrganizationMember, User

ALL_ROLES = (
    Role.OWNER,
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
)

# The policy the workspace-create route enforces (P6-AUTH-7).
ROUTE_POLICY = (Role.OWNER, Role.ADMIN)
ROUTE_DENIED = (Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER, Role.VIEWER)

# The non-contiguous probe. No production route mounts this policy.
PROBE_POLICY = (Role.OWNER, Role.COMPLIANCE_REVIEWER)
PROBE_DENIED = (Role.ADMIN, Role.MARKETER, Role.REVIEWER, Role.VIEWER)

# Single-role policies. No production route mounts either. OWNER leads the ADMIN list:
# it outranks ADMIN, so it is the denial a rank floor would turn into an admission.
ADMIN_ONLY_DENIED = (
    Role.OWNER,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
)
OWNER_ONLY_DENIED = (
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
)

ORG_A, ORG_B = "org-auth7-a", "org-auth7-b"
# Never written: an arbitrary organization id with no row behind it.
MISSING_ORG = "org-auth7-missing"
# Active user belonging to no organization at all.
OUTSIDER = "auth7-outsider"

# One user, two organizations, two different roles -- and its mirror image.
SPLIT_OWNER_VIEWER = "auth7-split-own-vie"
SPLIT_VIEWER_ADMIN = "auth7-split-vie-adm"
SPLIT_ROLES = {
    SPLIT_OWNER_VIEWER: {ORG_A: Role.OWNER, ORG_B: Role.VIEWER},
    SPLIT_VIEWER_ADMIN: {ORG_A: Role.VIEWER, ORG_B: Role.ADMIN},
}

# The two production-owned 403 messages, quoted from ``app.auth.dependencies``:
# ``_membership`` raises the first, the exact-role checker the second.
NOT_A_MEMBER_MESSAGE = "You are not a member of this organization."
# The route-local message of ``organizations.routes._assert_member``. The seam must not
# produce it: it would mean the membership check is not the shared one.
LEGACY_NOT_A_MEMBER_MESSAGE = "Not a member of this organization."
ORG_NOT_FOUND_MESSAGE = "Organization not found."


def _role_denial_message(role: Role) -> str:
    return f"Role '{role.value}' is not permitted for this action."


# Width of every id column written here (``UUIDPrimaryKeyMixin`` and the foreign keys
# are ``String(32)``). Read off the mapping so a model change surfaces here.
_ID_MAX = User.__table__.c.id.type.length

_ID_COLUMNS = (
    (Organization, ("id",)),
    (User, ("id",)),
    (OrganizationMember, ("id", "organization_id", "user_id")),
)


def _db_id(value: str) -> str:
    """Return ``value`` unless it cannot fit a ``String(32)`` id column.

    SQLite stores an over-long ``VARCHAR(n)`` value silently; PostgreSQL rejects it.
    Failing at construction keeps this module honest without a live PostgreSQL.
    """
    if len(value) > _ID_MAX:
        raise AssertionError(
            f"fixture id {value!r} is {len(value)} characters; the id columns are String({_ID_MAX})"
        )
    return value


# One user per role in ORG_A: user id "org-auth7-a-owner", ... (at most 31 characters).
def _uid(role: Role) -> str:
    return _db_id(f"{ORG_A}-{role.value}")


def _mid(role: Role) -> str:
    """Membership id keyed on a three-letter role code (own/adm/mar/rev/vie/com)."""
    return _db_id(f"m-{ORG_A}-{role.value[:3]}")


# (membership id, organization, user, role) for every membership this module writes.
_MEMBERSHIPS = tuple((_mid(r), ORG_A, _uid(r), r) for r in ALL_ROLES) + (
    ("m-auth7-ov-a", ORG_A, SPLIT_OWNER_VIEWER, Role.OWNER),
    ("m-auth7-ov-b", ORG_B, SPLIT_OWNER_VIEWER, Role.VIEWER),
    ("m-auth7-va-a", ORG_A, SPLIT_VIEWER_ADMIN, Role.VIEWER),
    ("m-auth7-va-b", ORG_B, SPLIT_VIEWER_ADMIN, Role.ADMIN),
)
_USERS = tuple(_uid(r) for r in ALL_ROLES) + (SPLIT_OWNER_VIEWER, SPLIT_VIEWER_ADMIN, OUTSIDER)


@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("organization_role_authorization")
    engine = create_engine(
        f"sqlite:///{tmp / 'org_role_auth.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    # Configured like production's ``SessionLocal``.
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    with make() as s:
        for org in (ORG_A, ORG_B):
            s.add(Organization(id=_db_id(org), name=f"Org {org}", slug=org))
        for uid in _USERS:
            s.add(
                User(
                    id=_db_id(uid),
                    email=f"{uid}@example.com",
                    full_name=uid,
                    hashed_password="x",
                    is_active=True,
                )
            )
        s.flush()
        for mid, org, uid, role in _MEMBERSHIPS:
            s.add(
                OrganizationMember(
                    id=_db_id(mid), organization_id=org, user_id=uid, role=role.value
                )
            )
        s.commit()
    try:
        yield make
    finally:
        engine.dispose()


@pytest.fixture
def db(factory):
    s = factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


def _user(db, uid: str) -> User:
    user = db.get(User, uid)
    assert user is not None and user.is_active, uid
    return user


def _resolve(db, org_id: str, uid: str) -> OrganizationContext:
    return get_organization_context(org_id, db=db, user=_user(db, uid))


def _admitted(checker, db) -> set[Role]:
    """The set of ORG_A roles ``checker`` admits, each resolved from its persisted row.

    Every denial must be the role guard's, carrying the requester's own role: these
    users are all members of ORG_A, so a membership denial here would be a fixture
    defect masquerading as a policy result. Every admission must return the resolved
    context itself.
    """
    admitted = set()
    for role in ALL_ROLES:
        ctx = _resolve(db, ORG_A, _uid(role))
        assert ctx.role is role
        try:
            returned = checker(ctx)
        except PermissionDeniedError as exc:
            assert exc.message == _role_denial_message(role)
            continue
        assert returned is ctx
        admitted.add(role)
    return admitted


def _denial(fn):
    with pytest.raises(PermissionDeniedError) as exc:
        fn()
    return exc.value


class TestFixtureIntegrity:
    """The fixture's own preconditions. A defect here silently weakens every test below."""

    def test_id_columns_are_the_width_this_module_enforces(self):
        assert _ID_MAX == 32
        for model, names in _ID_COLUMNS:
            for name in names:
                assert model.__table__.c[name].type.length == _ID_MAX, (model.__name__, name)

    def test_id_guard_rejects_an_overlong_id(self):
        assert _db_id("x" * _ID_MAX) == "x" * _ID_MAX
        with pytest.raises(AssertionError):
            _db_id("x" * (_ID_MAX + 1))

    def test_every_persisted_id_fits_its_column(self, db):
        checked = 0
        for model, names in _ID_COLUMNS:
            for row in db.scalars(select(model)):
                for name in names:
                    assert len(getattr(row, name)) <= _ID_MAX, (model.__name__, name)
                    checked += 1
        assert checked > 0, "no persisted ids inspected -- the loop above was vacuous"

    def test_persisted_memberships_are_exactly_the_seeded_ones(self, db):
        rows = {
            (m.organization_id, m.user_id, m.role) for m in db.scalars(select(OrganizationMember))
        }
        assert rows == {(org, uid, role.value) for _, org, uid, role in _MEMBERSHIPS}

    def test_missing_org_really_is_missing(self, db):
        assert db.get(Organization, MISSING_ORG) is None
        assert db.get(Organization, ORG_A) is not None
        assert db.get(Organization, ORG_B) is not None

    def test_foreign_keys_are_enforced(self, db):
        assert db.execute(text("PRAGMA foreign_keys")).scalar() == 1

    def test_foreign_key_refuses_a_membership_without_its_organization(self, db):
        """Why the 404 limb below is pinned with a stub, not a row.

        The state that reaches ``Organization not found.`` -- a membership whose
        organization is absent -- is refused by the database. The insert is attempted
        only to observe the refusal, and is rolled back.
        """
        db.add(
            OrganizationMember(
                id="m-auth7-orphan",
                organization_id=MISSING_ORG,
                user_id=OUTSIDER,
                role=Role.OWNER.value,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        orphan = select(OrganizationMember).where(OrganizationMember.user_id == OUTSIDER)
        assert db.scalar(orphan) is None


class TestPersistedRoleDerivation:
    """``ctx.role`` is the persisted membership role of the REQUESTED organization."""

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_role_is_the_persisted_membership_role(self, factory, db, role: Role):
        user = _user(db, _uid(role))
        ctx = get_organization_context(ORG_A, db=db, user=user)

        # Read back through an independent session: what the database holds, not what
        # the resolving session's identity map happens to carry.
        with factory() as other:
            persisted = other.scalar(
                select(OrganizationMember.role).where(
                    OrganizationMember.organization_id == ORG_A,
                    OrganizationMember.user_id == user.id,
                )
            )
        # ``Role`` is a StrEnum, so a raw "owner" string would also satisfy ``==``.
        # Identity with the enum member proves the value was converted to a Role.
        assert ctx.role is Role(persisted)
        assert ctx.role is role
        assert type(ctx) is OrganizationContext
        assert ctx.user is user
        assert isinstance(ctx.organization, Organization)
        assert ctx.organization.id == ORG_A

    @pytest.mark.parametrize(
        ("uid", "org"),
        [(uid, org) for uid, roles in SPLIT_ROLES.items() for org in roles],
    )
    def test_role_comes_from_the_requested_organization(self, db, uid: str, org: str):
        ctx = _resolve(db, org, uid)
        assert ctx.role is SPLIT_ROLES[uid][org]
        assert ctx.organization.id == org

    def test_split_fixture_discriminates_in_both_directions(self, db):
        """The same user resolves to different roles per organization, and mirror-wise.

        Were the role taken from any membership other than the requested one, at least
        one of these four resolutions would come back wrong.
        """
        observed = {
            (uid, org): _resolve(db, org, uid).role
            for uid, roles in SPLIT_ROLES.items()
            for org in roles
        }
        assert observed == {
            (SPLIT_OWNER_VIEWER, ORG_A): Role.OWNER,
            (SPLIT_OWNER_VIEWER, ORG_B): Role.VIEWER,
            (SPLIT_VIEWER_ADMIN, ORG_A): Role.VIEWER,
            (SPLIT_VIEWER_ADMIN, ORG_B): Role.ADMIN,
        }
        for roles in SPLIT_ROLES.values():
            assert roles[ORG_A] != roles[ORG_B]
        assert SPLIT_ROLES[SPLIT_OWNER_VIEWER][ORG_A] != SPLIT_ROLES[SPLIT_VIEWER_ADMIN][ORG_A]
        assert SPLIT_ROLES[SPLIT_OWNER_VIEWER][ORG_B] != SPLIT_ROLES[SPLIT_VIEWER_ADMIN][ORG_B]

    def test_role_follows_a_rewrite_of_the_persisted_row(self, factory, db):
        """Rewrite the stored role inside a transaction; the next resolution follows it.

        The rewrite is rolled back, and an independent session confirms the stored row
        is back to MARKETER, so no other test observes it.
        """
        uid = _uid(Role.MARKETER)
        assert _resolve(db, ORG_A, uid).role is Role.MARKETER
        db.rollback()

        db.execute(
            update(OrganizationMember)
            .where(OrganizationMember.id == _mid(Role.MARKETER))
            .values(role=Role.ADMIN.value)
        )
        assert _resolve(db, ORG_A, uid).role is Role.ADMIN
        db.rollback()

        assert _resolve(db, ORG_A, uid).role is Role.MARKETER
        with factory() as other:
            stored = other.scalar(
                select(OrganizationMember.role).where(OrganizationMember.id == _mid(Role.MARKETER))
            )
        assert stored == Role.MARKETER.value


class TestMembershipBeforeExistence:
    """A caller with no membership learns nothing about whether the organization exists."""

    def test_non_member_of_an_existing_org_gets_the_shared_membership_denial(self, db):
        denial = _denial(lambda: _resolve(db, ORG_A, OUTSIDER))
        assert denial.message == NOT_A_MEMBER_MESSAGE
        assert denial.message != LEGACY_NOT_A_MEMBER_MESSAGE
        assert denial.status_code == 403
        assert denial.code == "permission_denied"

    @pytest.mark.parametrize("uid", (OUTSIDER, _uid(Role.OWNER)))
    def test_nonexistent_org_without_membership_is_403_not_404(self, db, uid: str):
        """An arbitrary unknown id is refused at membership, never at existence."""
        assert db.get(Organization, MISSING_ORG) is None
        try:
            _resolve(db, MISSING_ORG, uid)
        except NotFoundError:  # pragma: no cover - the defect this test exists to catch
            pytest.fail("nonexistent organization disclosed as 404 to a non-member")
        except PermissionDeniedError as exc:
            assert exc.message == NOT_A_MEMBER_MESSAGE
        else:  # pragma: no cover
            pytest.fail("nonexistent organization resolved to a context")

    def test_member_elsewhere_is_not_a_member_here(self, db):
        """ORG_A's OWNER holds no membership in ORG_B, which exists."""
        denial = _denial(lambda: _resolve(db, ORG_B, _uid(Role.OWNER)))
        assert denial.message == NOT_A_MEMBER_MESSAGE

    @pytest.mark.parametrize("uid", (OUTSIDER, _uid(Role.OWNER)))
    def test_existing_and_missing_org_are_indistinguishable_to_a_non_member(self, db, uid: str):
        def outcome(org_id):
            denial = _denial(lambda: _resolve(db, org_id, uid))
            return type(denial), denial.status_code, denial.code, denial.message

        assert db.get(Organization, ORG_B) is not None
        assert db.get(Organization, MISSING_ORG) is None
        assert outcome(ORG_B) == outcome(MISSING_ORG)

    def test_membership_is_delegated_to_the_shared_helper(self, db, monkeypatch):
        """The seam calls ``_membership`` itself rather than a copy of its predicate."""
        calls = []
        real = deps._membership

        def spy(session, user_id, org_id):
            calls.append((session, user_id, org_id))
            return real(session, user_id, org_id)

        monkeypatch.setattr(deps, "_membership", spy)

        owner = _uid(Role.OWNER)
        assert _resolve(db, ORG_A, owner).role is Role.OWNER
        _denial(lambda: _resolve(db, MISSING_ORG, OUTSIDER))
        assert calls == [(db, owner, ORG_A), (db, OUTSIDER, MISSING_ORG)]


class _OrderingStubSession:
    """STRUCTURAL-ORDERING STUB. Not a database, and no evidence about SQL.

    ``get_organization_context`` makes exactly two session calls: ``scalar`` (the
    membership query, issued inside ``_membership``) and ``get`` (the organization
    lookup). This stub answers each with a canned value and records the order of the
    calls. It exists to reach a state a real database with foreign keys will not hold --
    a membership whose organization is absent -- and to show which lookup runs first.
    """

    def __init__(self, *, membership, organization):
        self._membership = membership
        self._organization = organization
        self.calls: list[tuple[str, object]] = []

    def scalar(self, statement):
        self.calls.append(("membership", None))
        return self._membership

    def get(self, model, ident):
        assert model is Organization
        self.calls.append(("organization", ident))
        return self._organization


_STUB_USER = SimpleNamespace(id="stub-user")


class TestLookupOrdering:
    """Membership (403) precedes organization existence (404). Stub-based by necessity."""

    def test_membership_without_its_organization_is_404(self):
        session = _OrderingStubSession(membership=SimpleNamespace(role="owner"), organization=None)
        with pytest.raises(NotFoundError) as exc:
            get_organization_context(MISSING_ORG, db=session, user=_STUB_USER)
        assert exc.value.message == ORG_NOT_FOUND_MESSAGE
        assert exc.value.status_code == 404
        assert exc.value.code == "not_found"
        assert session.calls == [("membership", None), ("organization", MISSING_ORG)]

    @pytest.mark.parametrize("organization", (object(), None), ids=("exists", "missing"))
    def test_no_membership_means_no_organization_lookup(self, organization):
        """No oracle: the outcome and the call log are identical whether or not it exists."""
        session = _OrderingStubSession(membership=None, organization=organization)
        with pytest.raises(PermissionDeniedError) as exc:
            get_organization_context(ORG_A, db=session, user=_STUB_USER)
        assert exc.value.message == NOT_A_MEMBER_MESSAGE
        assert session.calls == [("membership", None)]

    def test_membership_lookup_precedes_organization_lookup_on_success(self):
        organization = object()
        session = _OrderingStubSession(
            membership=SimpleNamespace(role="admin"), organization=organization
        )
        ctx = get_organization_context(ORG_A, db=session, user=_STUB_USER)
        assert session.calls == [("membership", None), ("organization", ORG_A)]
        assert ctx.organization is organization
        assert ctx.user is _STUB_USER
        assert ctx.role is Role.ADMIN


class TestNonContiguousExactnessProbe:
    """``(OWNER, COMPLIANCE_REVIEWER)``: a policy no rank-based checker can express."""

    @pytest.mark.parametrize("role", PROBE_POLICY)
    def test_named_role_allowed(self, db, role: Role):
        ctx = _resolve(db, ORG_A, _uid(role))
        assert require_exact_organization_roles(*PROBE_POLICY)(ctx) is ctx

    @pytest.mark.parametrize("role", PROBE_DENIED)
    def test_unnamed_role_denied(self, db, role: Role):
        ctx = _resolve(db, ORG_A, _uid(role))
        denial = _denial(lambda: require_exact_organization_roles(*PROBE_POLICY)(ctx))
        assert denial.message == _role_denial_message(role)

    def test_probe_admits_exactly_the_named_pair(self, db):
        admitted = _admitted(require_exact_organization_roles(*PROBE_POLICY), db)
        assert admitted == {Role.OWNER, Role.COMPLIANCE_REVIEWER}

    def test_same_rank_roles_are_distinguished(self, db):
        """REVIEWER and COMPLIANCE_REVIEWER share a rank; only one is named."""
        assert _ROLE_RANK[Role.REVIEWER] == _ROLE_RANK[Role.COMPLIANCE_REVIEWER]
        checker = require_exact_organization_roles(*PROBE_POLICY)
        compliance = _resolve(db, ORG_A, _uid(Role.COMPLIANCE_REVIEWER))
        reviewer = _resolve(db, ORG_A, _uid(Role.REVIEWER))
        assert checker(compliance) is compliance
        assert _denial(lambda: checker(reviewer)).message == _role_denial_message(Role.REVIEWER)

    def test_no_rank_threshold_yields_the_probe_set(self, db):
        """Every rank floor admits an upward-closed set; the probe's admitted set is not.

        ADMIN and MARKETER outrank COMPLIANCE_REVIEWER and are denied, so the result is
        unreachable by ``rank >= t`` for any ``t``.
        """
        admitted = _admitted(require_exact_organization_roles(*PROBE_POLICY), db)
        floors = {
            frozenset(r for r in ALL_ROLES if _ROLE_RANK[r] >= t) for t in set(_ROLE_RANK.values())
        }
        assert frozenset(admitted) not in floors
        assert _ROLE_RANK[Role.ADMIN] > _ROLE_RANK[Role.COMPLIANCE_REVIEWER]
        assert _ROLE_RANK[Role.MARKETER] > _ROLE_RANK[Role.COMPLIANCE_REVIEWER]
        assert Role.ADMIN not in admitted and Role.MARKETER not in admitted

    def test_production_policy_alone_cannot_tell_exact_from_rank(self, db):
        """Why the probe exists: (OWNER, ADMIN) coincides with ``rank >= ADMIN``."""
        rank_floor = {r for r in ALL_ROLES if _ROLE_RANK[r] >= _ROLE_RANK[Role.ADMIN]}
        exact = _admitted(require_exact_organization_roles(*ROUTE_POLICY), db)
        assert rank_floor == exact == {Role.OWNER, Role.ADMIN}


class TestRoutePolicyAtTheHelper:
    """``(OWNER, ADMIN)``, the policy the workspace-create route mounts."""

    def test_admits_exactly_owner_and_admin(self, db):
        assert _admitted(require_exact_organization_roles(*ROUTE_POLICY), db) == {
            Role.OWNER,
            Role.ADMIN,
        }

    @pytest.mark.parametrize("role", ROUTE_POLICY)
    def test_named_role_allowed(self, db, role: Role):
        ctx = _resolve(db, ORG_A, _uid(role))
        assert require_exact_organization_roles(*ROUTE_POLICY)(ctx) is ctx

    @pytest.mark.parametrize("role", ROUTE_DENIED)
    def test_denied_role_gets_the_role_message_naming_only_its_own_role(self, db, role: Role):
        ctx = _resolve(db, ORG_A, _uid(role))
        denial = _denial(lambda: require_exact_organization_roles(*ROUTE_POLICY)(ctx))
        assert denial.message == _role_denial_message(role)
        assert denial.message != NOT_A_MEMBER_MESSAGE
        assert denial.status_code == 403
        assert denial.code == "permission_denied"
        for other in ALL_ROLES:
            if other is not role:
                assert f"'{other.value}'" not in denial.message
        for leaked in ("owner", "admin", ORG_A):
            assert leaked not in denial.message

    def test_split_users_are_judged_by_the_requested_organization(self, db):
        """Admitted where the role is named, denied where it is not -- both directions."""
        checker = require_exact_organization_roles(*ROUTE_POLICY)

        home = _resolve(db, ORG_A, SPLIT_OWNER_VIEWER)
        assert checker(home) is home
        away = _resolve(db, ORG_B, SPLIT_OWNER_VIEWER)
        assert _denial(lambda: checker(away)).message == _role_denial_message(Role.VIEWER)

        mirror_a = _resolve(db, ORG_A, SPLIT_VIEWER_ADMIN)
        assert _denial(lambda: checker(mirror_a)).message == _role_denial_message(Role.VIEWER)
        mirror_b = _resolve(db, ORG_B, SPLIT_VIEWER_ADMIN)
        assert checker(mirror_b) is mirror_b

    def test_role_denial_and_membership_denial_are_distinct_sources(self, db):
        """Same caller: role denial where it is a member, membership denial elsewhere.

        Both are 403 / ``permission_denied``; only the message says which gate refused.
        The membership denial is raised while resolving the context, before the role
        checker could run.
        """
        checker = require_exact_organization_roles(*ROUTE_POLICY)
        uid = _uid(Role.MARKETER)

        home = _denial(lambda: checker(_resolve(db, ORG_A, uid)))
        away = _denial(lambda: checker(_resolve(db, ORG_B, uid)))
        assert (home.status_code, home.code) == (away.status_code, away.code)
        assert home.message == _role_denial_message(Role.MARKETER)
        assert away.message == NOT_A_MEMBER_MESSAGE


class TestSingleRolePolicies:
    """A single-role policy admits that role alone -- not the roles that outrank it."""

    def test_admin_only_admits_admin(self, db):
        ctx = _resolve(db, ORG_A, _uid(Role.ADMIN))
        assert require_exact_organization_roles(Role.ADMIN)(ctx) is ctx

    @pytest.mark.parametrize("role", ADMIN_ONLY_DENIED)
    def test_admin_only_denies_every_other_role(self, db, role: Role):
        ctx = _resolve(db, ORG_A, _uid(role))
        denial = _denial(lambda: require_exact_organization_roles(Role.ADMIN)(ctx))
        assert denial.message == _role_denial_message(role)
        assert denial.status_code == 403
        assert denial.code == "permission_denied"

    def test_admin_only_denies_owner_although_owner_outranks_admin(self, db):
        """The admission a rank floor over privileged-only policies would make."""
        assert _ROLE_RANK[Role.OWNER] > _ROLE_RANK[Role.ADMIN]
        owner = _resolve(db, ORG_A, _uid(Role.OWNER))
        denial = _denial(lambda: require_exact_organization_roles(Role.ADMIN)(owner))
        assert denial.message == _role_denial_message(Role.OWNER)
        assert f"'{Role.ADMIN.value}'" not in denial.message

    def test_admin_only_admits_exactly_admin(self, db):
        assert _admitted(require_exact_organization_roles(Role.ADMIN), db) == {Role.ADMIN}

    def test_admin_only_is_not_the_admin_rank_floor(self, db):
        """Unlike (OWNER, ADMIN), ``(ADMIN,)`` differs from ``rank >= ADMIN`` by OWNER."""
        rank_floor = {r for r in ALL_ROLES if _ROLE_RANK[r] >= _ROLE_RANK[Role.ADMIN]}
        exact = _admitted(require_exact_organization_roles(Role.ADMIN), db)
        assert rank_floor - exact == {Role.OWNER}
        assert exact - rank_floor == set()

    def test_owner_only_admits_owner(self, db):
        ctx = _resolve(db, ORG_A, _uid(Role.OWNER))
        assert require_exact_organization_roles(Role.OWNER)(ctx) is ctx

    @pytest.mark.parametrize("role", OWNER_ONLY_DENIED)
    def test_owner_only_denies_every_other_role(self, db, role: Role):
        ctx = _resolve(db, ORG_A, _uid(role))
        denial = _denial(lambda: require_exact_organization_roles(Role.OWNER)(ctx))
        assert denial.message == _role_denial_message(role)
        assert denial.status_code == 403
        assert denial.code == "permission_denied"

    def test_owner_only_admits_exactly_owner(self, db):
        assert _admitted(require_exact_organization_roles(Role.OWNER), db) == {Role.OWNER}

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_every_single_role_policy_admits_exactly_its_role(self, db, role: Role):
        assert _admitted(require_exact_organization_roles(role), db) == {role}


class TestConstruction:
    def test_empty_allowed_set_fails_at_construction(self):
        with pytest.raises(ValueError, match="at least one role"):
            require_exact_organization_roles()

    def test_duplicate_roles_do_not_broaden_access(self, db):
        once = require_exact_organization_roles(*ROUTE_POLICY)
        with_duplicate = require_exact_organization_roles(Role.OWNER, Role.OWNER, Role.ADMIN)
        assert _admitted(with_duplicate, db) == _admitted(once, db) == {Role.OWNER, Role.ADMIN}


class TestContextIdentity:
    """An admitted context is the resolver's own object, returned untouched."""

    def test_every_admitted_role_returns_the_resolved_object(self, db):
        checked = 0
        for policy in (ROUTE_POLICY, PROBE_POLICY):
            checker = require_exact_organization_roles(*policy)
            for role in policy:
                ctx = _resolve(db, ORG_A, _uid(role))
                assert checker(ctx) is ctx
                checked += 1
        assert checked == 4

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_single_role_policy_returns_the_resolved_object(self, db, role: Role):
        """Identity, not equality: a value-equal copy would pass ``==``."""
        ctx = _resolve(db, ORG_A, _uid(role))
        twin = dataclasses.replace(ctx)
        returned = require_exact_organization_roles(role)(ctx)
        assert returned is ctx
        assert returned is not twin

    def test_admitted_context_is_the_identical_object_not_a_copy(self, db):
        """Identity, because ``OrganizationContext`` is a dataclass and compares by value."""
        ctx = _resolve(db, ORG_A, _uid(Role.ADMIN))
        twin = dataclasses.replace(ctx)
        assert twin == ctx and twin is not ctx  # equality cannot see a substitution

        returned = require_exact_organization_roles(*ROUTE_POLICY)(ctx)
        assert returned is ctx
        assert returned is not twin

    def test_context_fields_are_passed_through_untouched(self):
        user, organization = object(), object()
        ctx = OrganizationContext(user=user, organization=organization, role=Role.OWNER)
        returned = require_exact_organization_roles(*ROUTE_POLICY)(ctx)
        assert returned is ctx
        assert returned.user is user
        assert returned.organization is organization
        assert returned.role is Role.OWNER

    def test_context_carries_exactly_user_organization_role(self):
        assert [f.name for f in dataclasses.fields(OrganizationContext)] == [
            "user",
            "organization",
            "role",
        ]


def _openapi_parameters(checker) -> list[tuple[str, str]]:
    """Mount ``checker`` on a throwaway app (never the production app) and read its spec."""
    probe = FastAPI()

    @probe.get("/organizations/{organization_id}/probe")
    def _probe(ctx=Depends(checker)):
        return None

    operation = probe.openapi()["paths"]["/organizations/{organization_id}/probe"]["get"]
    assert "requestBody" not in operation
    return [(p["name"], p["in"]) for p in operation.get("parameters", [])]


class TestSeamShape:
    """The checker is wired to the organization resolver, and nothing asks for a workspace."""

    def test_checker_depends_only_on_get_organization_context(self):
        checker = require_exact_organization_roles(*ROUTE_POLICY)
        params = inspect.signature(checker).parameters
        assert list(params) == ["ctx"]
        dependency = params["ctx"].default.dependency
        assert dependency is get_organization_context
        assert dependency is not get_tenant_context

    def test_resolver_signature_is_path_id_then_session_then_authenticated_user(self):
        """``user`` is a ``get_current_user`` dependency, so authentication runs first."""
        params = inspect.signature(get_organization_context).parameters
        assert list(params) == ["organization_id", "db", "user"]
        assert "workspace_id" not in params
        assert params["organization_id"].default is inspect.Parameter.empty
        assert typing.get_type_hints(get_organization_context)["organization_id"] is str
        assert params["db"].default.dependency is get_db
        assert params["user"].default.dependency is get_current_user

    def test_guarded_route_publishes_only_path_id_and_authorization_header(self):
        parameters = _openapi_parameters(require_exact_organization_roles(*ROUTE_POLICY))
        assert sorted(parameters) == [("authorization", "header"), ("organization_id", "path")]
        names = {name for name, _ in parameters}
        for forbidden in ("workspace_id", "role", "membership", "user_id"):
            assert forbidden not in names

    def test_the_workspace_scoped_checker_would_publish_workspace_id(self):
        """Contrast: the check above can fail. Routing through ``get_tenant_context``
        publishes a ``workspace_id`` query parameter the create route does not have.
        """
        parameters = _openapi_parameters(require_exact_roles(*ROUTE_POLICY))
        assert ("workspace_id", "query") in parameters
