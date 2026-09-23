"""6B-1: HTTP-boundary authorization proof for the audit-log route (P6-AUTH-3).

    GET /api/v1/workspaces/{workspace_id}/audit-logs

Before this tranche the route used ``require_role(OWNER, ADMIN, COMPLIANCE_REVIEWER)``.
That primitive is a rank floor, so ``min_rank = min(4, 3, 1) = 1`` and MARKETER (2) and
REVIEWER (1) were both admitted to the organization-wide audit trail without being
named. Only VIEWER (0) was denied. The route now uses ``require_exact_roles``.

This is the primary P6-AUTH-3 regression barrier, and the route had none before: no
test anywhere under ``apps/api`` previously exercised it. It runs the real endpoint
through real dependency resolution -- only ``get_db`` is overridden, exactly as the
rest of the HTTP suite does, so bearer parsing, token decode, user lookup, workspace
resolution, membership lookup and the role guard all execute. Overriding the auth
dependencies would make the matrix vacuous, which is why nothing here touches them.

``OrganizationMember`` is unique on (organization_id, user_id), so the six-role matrix
uses six distinct users in one organization rather than re-roling one user.

Three fixture properties are load-bearing and easy to lose, so each is stated here and
guarded by its own test:

* **Reciprocal NULL-workspace rows.** ``workspace_id IS NULL`` (organization-wide) rows
  are the *only* rows the route's workspace predicate cannot exclude on its own, so they
  are the only rows whose visibility is decided by the organization predicate. Both
  organizations therefore carry one, and isolation is proved in both directions. A
  foreign-organization row that also sits in a foreign workspace proves nothing about the
  organization filter -- the workspace predicate already excluded it.
* **Ids that fit their columns.** Every id column here is ``String(32)``. SQLite stores an
  over-long value silently; PostgreSQL rejects it. Ids are therefore length-checked at
  construction (``_db_id``) and re-checked against the persisted rows, so this module
  stays portable without needing a live PostgreSQL service.
* **A denial's *source*.** Membership failure and role failure both surface as
  ``403 / permission_denied``. Status alone cannot tell them apart, so MARKETER and
  REVIEWER are proved to clear membership and fail at the role guard, using the two
  distinct production-owned messages.
* **The limiter that is reset is the one that serves requests.** ``RateLimitMiddleware``
  keeps its budget on the *instance*, and ``build_middleware_stack()`` returns a fresh,
  detached chain whose limiter is a real object of the right class with the right
  configuration -- and which no request will ever touch. "Found a limiter and cleared
  it" is therefore not the property that matters; "cleared the instance reachable from
  ``app.middleware_stack``" is. The helper returns the instance it cleared and the test
  compares it by identity against a reference located by its own independent walk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.core.config import get_settings
from app.core.enums import Role
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, OrganizationMember, User, Workspace

API = get_settings().api_prefix

ORG_A, WS_A, WS_A2 = "org-audit-a", "ws-audit-a", "ws-audit-a2"
ORG_B, WS_B = "org-audit-b", "ws-audit-b"

# The exact policy the route must enforce.
ALLOWED = (Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER)
DENIED = (Role.MARKETER, Role.REVIEWER, Role.VIEWER)
ALL_ROLES = ALLOWED + DENIED

OUTSIDER = "audit-outsider"

# Audit rows, by id. The two NULL-workspace rows are the discriminating pair: every
# other row is excluded from the opposite organization's view by the workspace
# predicate alone, and so says nothing about the organization predicate.
A_WS, A_NULL, A_SIBLING = "aud-a-ws", "aud-a-null", "aud-a-other-ws"
B_WS, B_NULL = "aud-b-ws", "aud-b-null"

VISIBLE_FROM_WS_A = {A_WS, A_NULL}
VISIBLE_FROM_WS_B = {B_WS, B_NULL}

# Width of every id column in play (``UUIDPrimaryKeyMixin`` and ``AuditLog.workspace_id``
# are both ``String(32)``). Read off the mapping rather than hard-coded, so a model change
# surfaces here instead of silently loosening the check.
_ID_MAX = User.__table__.c.id.type.length

# Every String-bounded id column this module writes, as (model, column names).
_ID_COLUMNS = (
    (Organization, ("id",)),
    (Workspace, ("id", "organization_id")),
    (User, ("id",)),
    (OrganizationMember, ("id", "organization_id", "user_id")),
    (AuditLog, ("id", "workspace_id")),
)


def _db_id(value: str) -> str:
    """Return ``value`` unless it cannot fit a ``String(32)`` id column.

    SQLite accepts an over-long value in a ``VARCHAR(n)`` column without complaint;
    PostgreSQL raises ``value too long for type character varying(32)``. A fixture id
    that overflows would therefore pass this module on SQLite and fail only against a
    deployment-shaped database, which is precisely the false confidence this guard
    removes. Failing at construction keeps the check engine-independent and needs no
    live PostgreSQL service.
    """
    if len(value) > _ID_MAX:
        raise AssertionError(
            f"fixture id {value!r} is {len(value)} characters; "
            f"the id columns are String({_ID_MAX})"
        )
    return value


# One user per role in ORG_A: "owner" -> user id "org-audit-a-owner".
def _uid(org: str, role: Role) -> str:
    return _db_id(f"{org}-{role.value}")


def _mid(org: str, role: Role) -> str:
    """Membership id, keyed on a three-letter role code rather than the role name.

    ``f"m-{_uid(ORG_A, Role.COMPLIANCE_REVIEWER)}"`` is 33 characters and the column is
    ``String(32)``. The six role values have distinct three-letter prefixes (owner,
    admin, marketer, reviewer, viewer, compliance_reviewer -> own/adm/mar/rev/vie/com),
    so truncating loses no distinction; ``test_membership_ids_are_distinct_per_role``
    holds that to be true rather than assuming it.
    """
    return _db_id(f"m-{org}-{role.value[:3]}")


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


# The two production-owned denial messages. Both arrive as 403 / permission_denied, so
# the message is the only thing that says *which* gate refused the request. Quoted from
# ``app.auth.dependencies``: ``_membership`` raises the first, the ``require_exact_roles``
# checker the second.
NOT_A_MEMBER_MESSAGE = "You are not a member of this organization."


def _role_denial_message(role: Role) -> str:
    return f"Role '{role.value}' is not permitted for this action."


#: Sentinel client key seeded into the *active* limiter's ledger by
#: ``test_reset_clears_the_instance_requests_actually_dispatch_through``. A helper that
#: clears some other ``RateLimitMiddleware`` leaves this key behind, which is what makes
#: that test's failure independent of anything the helper reports about itself.
_ACTIVE_INSTANCE_PROBE_KEY = "phase6b1-active-instance-proof"


def _materialize_middleware_stack() -> None:
    """Ensure the app has ASSIGNED its own active middleware stack.

    Starlette builds the stack lazily and caches it on first request
    (``Starlette.__call__``: ``if self.middleware_stack is None: self.middleware_stack =
    self.build_middleware_stack()``). This does the same thing and, crucially, the same
    way: the built chain is *assigned*, so it becomes the chain requests dispatch
    through. Building one without assigning it would produce a detached chain that is
    authoritative for nothing.
    """
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()


def _active_rate_limiter():
    """Return the ``RateLimitMiddleware`` reachable from the app's OWN active stack.

    The search root is ``app.middleware_stack`` and nothing else. It is deliberately
    never ``build_middleware_stack()``: that call returns a fresh chain containing a
    genuine ``RateLimitMiddleware`` of the right class with the right configuration,
    which is nonetheless not the object any request will consult. Its ledger is
    write-only. Reading the assigned attribute is what makes the result the *active*
    instance rather than merely a correct-looking one.

    Returns ``None`` when no limiter is mounted; callers decide how loudly to fail.
    """
    from app.core.middleware import RateLimitMiddleware

    node = app.middleware_stack
    while node is not None:
        if isinstance(node, RateLimitMiddleware):
            return node
        node = getattr(node, "app", None)
    return None


def _reset_rate_limiter():
    """Clear the ACTIVE fixed-window limiter, and return the exact instance cleared.

    Every TestClient request shares one client host and one in-process bucket; this
    module issues a request per role per test, so the bucket is wiped around each test
    to leave no residue that would 429 a later suite.

    Two failure modes are closed here, and they are different:

    * **Nothing found.** If no limiter is mounted in the active stack the helper raises
      rather than returning quietly, because a silent no-op leaves the budget unreset
      while every caller believes otherwise.
    * **The wrong thing found.** "A ``RateLimitMiddleware`` was located and cleared" is
      satisfiable by a detached instance and says nothing about the budget that actually
      governs requests. The instance is therefore *returned*, so a caller can hold it to
      the only standard that matters -- object identity with the limiter reachable from
      ``app.middleware_stack``.

    Returning the instance is safe: ``_reset_rate_limiter`` is test-only and no
    production API changes.
    """
    _materialize_middleware_stack()
    limiter = _active_rate_limiter()

    assert limiter is not None, (
        "RateLimitMiddleware was not found in app.middleware_stack, so the shared "
        "fixed-window budget was NOT reset. Silently continuing would let this "
        "module's requests accumulate and 429 an unrelated later suite."
    )

    limiter._hits.clear()
    return limiter


def _seed_org(s, org: str, ws_ids: tuple[str, ...]) -> None:
    s.add(Organization(id=_db_id(org), name=f"Org {org}", slug=org))
    s.flush()
    for ws in ws_ids:
        s.add(Workspace(id=_db_id(ws), organization_id=org, name=f"WS {ws}", slug=ws))
    s.flush()
    for role in ALL_ROLES:
        uid = _uid(org, role)
        s.add(
            User(
                id=uid,
                email=f"{uid}@example.com",
                full_name=uid,
                hashed_password="x",
                is_active=True,
            )
        )
        s.flush()
        s.add(
            OrganizationMember(
                id=_mid(org, role),
                organization_id=org,
                user_id=uid,
                role=role.value,
            )
        )
    s.flush()


@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("audit_logs_role_matrix")
    engine = create_engine(
        f"sqlite:///{tmp / 'audit_role_matrix.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with make() as s:
        _seed_org(s, ORG_A, (WS_A, WS_A2))
        _seed_org(s, ORG_B, (WS_B,))
        # Active user belonging to no organization at all.
        s.add(
            User(
                id=_db_id(OUTSIDER),
                email=f"{OUTSIDER}@example.com",
                full_name="Outsider",
                hashed_password="x",
                is_active=True,
            )
        )
        s.flush()

        # Five audit rows spanning every combination the route's filter discriminates,
        # including a NULL-workspace row in BOTH organizations. The B-side NULL row is
        # the one that makes the organization predicate load-bearing: it satisfies
        # ``workspace_id IS NULL`` and so survives the workspace predicate from any
        # workspace, in any organization. Only ``organization_id == ctx.organization.id``
        # keeps it out of org A's view, and vice versa for the A-side NULL row.
        #
        # created_at is set explicitly and distinctly: the query orders by created_at
        # DESC with no secondary key, so equal timestamps would make order engine-
        # dependent and these assertions flaky.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        rows = (
            (A_WS, ORG_A, WS_A, "workspace.scoped", 0),
            (A_NULL, ORG_A, None, "org.wide.a", 1),
            (A_SIBLING, ORG_A, WS_A2, "workspace.sibling", 2),
            (B_WS, ORG_B, WS_B, "other.org", 3),
            (B_NULL, ORG_B, None, "org.wide.b", 4),
        )
        for row_id, org, ws, action, offset in rows:
            s.add(
                AuditLog(
                    id=_db_id(row_id),
                    organization_id=org,
                    workspace_id=None if ws is None else _db_id(ws),
                    action=action,
                    context={},
                    created_at=base + timedelta(minutes=offset),
                )
            )
        s.commit()
    try:
        yield make
    finally:
        engine.dispose()


@pytest.fixture
def client(factory):
    def _override_get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    c = TestClient(app)
    _reset_rate_limiter()
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


def _url(ws: str) -> str:
    return f"{API}/workspaces/{ws}/audit-logs"


def _ids(response) -> set[str]:
    return {row["id"] for row in response.json()}


class TestFixtureIntegrity:
    """The fixture's own preconditions. A defect here silently weakens every test below."""

    def test_id_columns_are_the_width_this_module_enforces(self):
        """``_ID_MAX`` is read off the mapping; this pins what it is read from."""
        assert _ID_MAX == 32
        for model, names in _ID_COLUMNS:
            for name in names:
                column = model.__table__.c[name]
                assert column.type.length == _ID_MAX, (model.__name__, name, column.type)

    def test_id_guard_rejects_an_overlong_id(self):
        """The guard is live, not decorative -- a 33-character id must not pass it."""
        assert _db_id("x" * _ID_MAX) == "x" * _ID_MAX
        with pytest.raises(AssertionError):
            _db_id("x" * (_ID_MAX + 1))

    def test_every_generated_id_fits_before_it_reaches_the_database(self):
        """The generators, checked directly: this is what regressed to 33 characters."""
        generated = (
            [ORG_A, ORG_B, WS_A, WS_A2, WS_B, OUTSIDER, A_WS, A_NULL, A_SIBLING, B_WS, B_NULL]
            + [_uid(org, role) for org in (ORG_A, ORG_B) for role in ALL_ROLES]
            + [_mid(org, role) for org in (ORG_A, ORG_B) for role in ALL_ROLES]
        )
        assert generated, "no ids collected -- the assertion below would be vacuous"
        oversized = {v: len(v) for v in generated if len(v) > _ID_MAX}
        assert oversized == {}

    def test_every_persisted_id_fits_its_column(self, factory):
        """Re-checked against the rows that actually landed, not the generators.

        Catches any id that reaches the database without passing ``_db_id`` -- including
        foreign-key columns this module never writes by hand.
        """
        checked = 0
        with factory() as s:
            for model, names in _ID_COLUMNS:
                for row in s.scalars(select(model)):
                    for name in names:
                        value = getattr(row, name)
                        if value is None:
                            continue
                        limit = model.__table__.c[name].type.length
                        assert len(value) <= limit, (model.__name__, name, value, len(value))
                        checked += 1
        assert checked > 0, "no persisted ids inspected -- the loop above was vacuous"

    def test_membership_ids_are_distinct_per_role(self):
        """Shortening the membership id must not collapse two roles onto one row."""
        for org in (ORG_A, ORG_B):
            assert len({_mid(org, role) for role in ALL_ROLES}) == len(ALL_ROLES)
        assert len({_mid(ORG_A, r) for r in ALL_ROLES} | {_mid(ORG_B, r) for r in ALL_ROLES}) == (
            2 * len(ALL_ROLES)
        )

    def test_reset_clears_the_instance_requests_actually_dispatch_through(self, client):
        """The active-instance proof: identity, not class, not configuration.

        The witness is collected by this test's **own** inline walk of
        ``app.middleware_stack`` -- deliberately not via ``_active_rate_limiter``, since
        a helper and a check that share a lookup would go wrong together and agree.
        Recognisable state is then planted in that exact object, so the assertions below
        cannot be satisfied by anything the helper merely reports about itself:

        * a helper clearing a detached instance returns an object that fails
          ``is active_before``, and
        * leaves ``_ACTIVE_INSTANCE_PROBE_KEY`` sitting in the active ledger.

        Either failure alone is sufficient; both fire together for the throwaway-stack
        defect this test exists to prevent.
        """
        from app.core.middleware import RateLimitMiddleware

        # The ``client`` fixture has already driven the helper, so the app must by now
        # hold its own assigned stack. Asserted explicitly: the witness is only
        # meaningful if it is read from a materialised active stack, and the helper must
        # not be the thing that quietly creates one.
        assert app.middleware_stack is not None

        node = app.middleware_stack
        while node is not None and not isinstance(node, RateLimitMiddleware):
            node = getattr(node, "app", None)
        active_before = node
        assert isinstance(active_before, RateLimitMiddleware)

        active_before._hits[_ACTIVE_INSTANCE_PROBE_KEY] = [123.0]
        assert _ACTIVE_INSTANCE_PROBE_KEY in active_before._hits  # the probe is real

        reset_instance = _reset_rate_limiter()

        node = app.middleware_stack
        while node is not None and not isinstance(node, RateLimitMiddleware):
            node = getattr(node, "app", None)
        active_after = node

        assert active_after is active_before, (
            "the app's active limiter instance changed during the reset"
        )
        assert reset_instance is active_before, (
            "_reset_rate_limiter cleared a RateLimitMiddleware that is NOT the instance "
            "reachable from app.middleware_stack -- the reset is a silent no-op"
        )
        assert _ACTIVE_INSTANCE_PROBE_KEY not in active_before._hits, (
            "seeded state survived in the ACTIVE limiter's ledger, so the instance the "
            "helper cleared was not the one requests dispatch through"
        )

    def test_reset_returns_the_limiter_and_leaves_the_ledger_empty(self, client):
        """The returned instance is live and genuinely emptied, not a detached copy."""
        limiter = _reset_rate_limiter()
        assert limiter is _active_rate_limiter()
        assert dict(limiter._hits) == {}

        limiter._hits["some-client"] = [1.0, 2.0]
        again = _reset_rate_limiter()
        assert again is limiter
        assert dict(limiter._hits) == {}

    def test_reset_fails_loudly_when_the_active_stack_has_no_limiter(self, client):
        """The fail-loud path, exercised directly rather than inferred from a mutant.

        Substitutes a chain carrying no ``RateLimitMiddleware`` for the app's active
        stack and requires the helper to refuse. This gives ``assert limiter is not
        None`` a real in-suite failure mode: delete it and this test fails, with no
        mutation needed. The real stack is restored in ``finally``, so the identity the
        rest of the module depends on is untouched.
        """
        real_stack = app.middleware_stack
        assert real_stack is not None

        class _StackWithoutLimiter:
            app = None

        app.middleware_stack = _StackWithoutLimiter()
        try:
            assert _active_rate_limiter() is None
            with pytest.raises(AssertionError, match="RateLimitMiddleware was not found"):
                _reset_rate_limiter()
        finally:
            app.middleware_stack = real_stack

        assert app.middleware_stack is real_stack
        assert _active_rate_limiter() is not None

    def test_the_lookup_never_uses_a_detached_stack(self, client):
        """A freshly built chain is a different object graph -- the contrast, stated.

        ``build_middleware_stack()`` yields a limiter that passes every class and
        configuration check and is still the wrong object. Asserting the distinction
        here documents why ``_active_rate_limiter`` reads the assigned attribute.
        """
        from app.core.middleware import RateLimitMiddleware

        detached = app.build_middleware_stack()
        node = detached
        while node is not None and not isinstance(node, RateLimitMiddleware):
            node = getattr(node, "app", None)

        assert isinstance(node, RateLimitMiddleware)  # real class, real config
        assert node is not _active_rate_limiter()  # ... and still not the live one
        assert detached is not app.middleware_stack


class TestSixRoleMatrix:
    """The load-bearing P6-AUTH-3 matrix, over the real endpoint."""

    @pytest.mark.parametrize("role", ALLOWED)
    def test_allowed_role_gets_200(self, client, role: Role):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, role)))
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    @pytest.mark.parametrize("role", DENIED)
    def test_denied_role_gets_403(self, client, role: Role):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, role)))
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "permission_denied"

    def test_full_matrix_in_one_assertion(self, client):
        """Guards against a partial fix that repairs one role and not the other."""
        observed = {
            role: client.get(_url(WS_A), headers=_auth(_uid(ORG_A, role))).status_code
            for role in ALL_ROLES
        }
        assert observed == {
            Role.OWNER: 200,
            Role.ADMIN: 200,
            Role.COMPLIANCE_REVIEWER: 200,
            Role.MARKETER: 403,
            Role.REVIEWER: 403,
            Role.VIEWER: 403,
        }

    def test_marketer_denied_is_the_pre_fix_over_permission(self, client):
        """MARKETER (rank 2) cleared the old floor of 1. It must not clear membership."""
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.MARKETER)))
        assert r.status_code == 403

    def test_reviewer_denied_despite_sharing_rank_with_compliance_reviewer(self, client):
        """REVIEWER and COMPLIANCE_REVIEWER are both rank 1 -- only one is named."""
        allowed = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.COMPLIANCE_REVIEWER)))
        denied = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.REVIEWER)))
        assert allowed.status_code == 200
        assert denied.status_code == 403

    def test_denial_message_does_not_disclose_the_policy(self, client):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.VIEWER)))
        message = r.json()["error"]["message"]
        assert message == _role_denial_message(Role.VIEWER)
        for leaked in ("owner", "admin", "compliance_reviewer", ORG_A, WS_A):
            assert leaked not in message


class TestDenialSource:
    """*Which* gate refused the request -- 403 alone cannot say.

    ``_membership`` and the ``require_exact_roles`` checker both raise
    ``PermissionDeniedError``, so both denials leave the API as 403 with code
    ``permission_denied``. A MARKETER who was accidentally excluded from the
    organization would therefore produce exactly the same status as one correctly
    stopped by the role guard, and the matrix above would pass while proving nothing
    about P6-AUTH-3. The two production messages are the discriminator (mutant M9).
    """

    def test_the_two_denials_are_indistinguishable_by_status_and_code(self, client):
        """Establishes the problem: status and code cannot tell the gates apart."""
        role_denied = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.MARKETER)))
        membership_denied = client.get(_url(WS_A), headers=_auth(OUTSIDER))
        assert role_denied.status_code == membership_denied.status_code == 403
        assert (
            role_denied.json()["error"]["code"]
            == membership_denied.json()["error"]["code"]
            == "permission_denied"
        )
        # ... and establishes the discriminator: the messages differ.
        assert role_denied.json()["error"]["message"] != (
            membership_denied.json()["error"]["message"]
        )

    def test_membership_failure_carries_the_membership_message(self, client):
        r = client.get(_url(WS_A), headers=_auth(OUTSIDER))
        assert r.status_code == 403
        assert r.json()["error"]["message"] == NOT_A_MEMBER_MESSAGE

    @pytest.mark.parametrize("role", (Role.MARKETER, Role.REVIEWER))
    def test_over_permissioned_roles_are_stopped_by_the_role_guard(self, client, role: Role):
        """MARKETER and REVIEWER clear membership and fail at the role guard.

        ``get_tenant_context`` resolves membership *before* the checker runs, so the
        role-guard message can only be produced by a request whose membership lookup
        succeeded. This is the P6-AUTH-3 claim stated precisely: these roles are
        organization members who are nonetheless not permitted this action.
        """
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, role)))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "permission_denied"
        assert r.json()["error"]["message"] == _role_denial_message(role)
        assert r.json()["error"]["message"] != NOT_A_MEMBER_MESSAGE

    @pytest.mark.parametrize("role", (Role.MARKETER, Role.REVIEWER))
    def test_one_user_two_organizations_two_denial_sources(self, client, role: Role):
        """The same caller, the same route: role denial at home, membership denial away.

        Direct evidence that the org-A denial is not a membership denial in disguise --
        the very same token produces the membership message the moment membership
        genuinely fails.
        """
        home = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, role)))
        away = client.get(_url(WS_B), headers=_auth(_uid(ORG_A, role)))
        assert home.status_code == away.status_code == 403
        assert home.json()["error"]["message"] == _role_denial_message(role)
        assert away.json()["error"]["message"] == NOT_A_MEMBER_MESSAGE

    def test_viewer_is_also_stopped_at_the_role_guard(self, client):
        """VIEWER was already denied pre-fix; the source of that denial is still the guard."""
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.VIEWER)))
        assert r.json()["error"]["message"] == _role_denial_message(Role.VIEWER)


class TestAuthenticationAndTenancy:
    """Authorization is layered on top of identity and membership, not instead of it."""

    def test_anonymous_gets_401(self, client):
        r = client.get(_url(WS_A))
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"

    def test_malformed_bearer_gets_401(self, client):
        r = client.get(_url(WS_A), headers={"Authorization": "Bearer not-a-token"})
        assert r.status_code == 401

    def test_non_member_gets_403(self, client):
        """Active user, valid token, no membership anywhere."""
        r = client.get(_url(WS_A), headers=_auth(OUTSIDER))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "permission_denied"

    def test_missing_workspace_keeps_existing_404_oracle(self, client):
        """Recorded as EXISTING behaviour, unchanged by this tranche.

        ``get_tenant_context`` resolves the workspace before membership, so an unknown
        workspace id is a 404 for any authenticated caller. 6B-1 does not alter this.
        """
        r = client.get(_url("ws-does-not-exist"), headers=_auth(_uid(ORG_A, Role.OWNER)))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_anonymous_on_missing_workspace_is_still_401(self, client):
        """Identity is resolved before the workspace: 401 outranks 404."""
        r = client.get(_url("ws-does-not-exist"))
        assert r.status_code == 401


class TestCrossTenantIsolation:
    """An allowed role in org A authorizes nothing in org B."""

    @pytest.mark.parametrize("role", ALLOWED)
    def test_allowed_role_cannot_reach_another_orgs_workspace(self, client, role: Role):
        r = client.get(_url(WS_B), headers=_auth(_uid(ORG_A, role)))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "permission_denied"
        assert r.json()["error"]["message"] == NOT_A_MEMBER_MESSAGE

    def test_org_b_owner_can_reach_only_its_own_workspace(self, client):
        own = client.get(_url(WS_B), headers=_auth(_uid(ORG_B, Role.OWNER)))
        foreign = client.get(_url(WS_A), headers=_auth(_uid(ORG_B, Role.OWNER)))
        assert own.status_code == 200
        assert foreign.status_code == 403

    def test_denied_role_is_denied_cross_tenant_too(self, client):
        r = client.get(_url(WS_B), headers=_auth(_uid(ORG_A, Role.MARKETER)))
        assert r.status_code == 403


class TestAuditDataIsolation:
    """Regression barrier around the unchanged query. 6B-1 alters no filter.

    The route's WHERE clause is a conjunction of two independent predicates::

        AuditLog.organization_id == ctx.organization.id          # organization
        workspace_id == workspace_id OR workspace_id IS NULL     # workspace

    A test can only be said to prove the organization predicate if it fails when that
    predicate is deleted. For every row whose ``workspace_id`` is a *foreign* workspace,
    the workspace predicate excludes it unaided -- asserting its absence is therefore
    satisfied by a query with no organization filter at all. Only the NULL-workspace
    rows discriminate, so those carry the proof, in both directions.
    """

    def test_ws_a_returns_own_workspace_and_own_org_wide_rows_only(self, client):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)))
        assert r.status_code == 200
        assert _ids(r) == VISIBLE_FROM_WS_A

    def test_ws_b_returns_own_workspace_and_own_org_wide_rows_only(self, client):
        r = client.get(_url(WS_B), headers=_auth(_uid(ORG_B, Role.OWNER)))
        assert r.status_code == 200
        assert _ids(r) == VISIBLE_FROM_WS_B

    def test_foreign_org_wide_row_is_excluded_from_org_a(self, client):
        """The reciprocal leak proof, direction A (mutant M8).

        ``aud-b-null`` has ``workspace_id IS NULL``, so it passes org A's workspace
        predicate. Its absence is attributable to the organization predicate and to
        nothing else: delete that predicate and this row appears here.
        """
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)))
        assert B_NULL not in _ids(r)

    def test_foreign_org_wide_row_is_excluded_from_org_b(self, client):
        """The reciprocal leak proof, direction B.

        Symmetrical and not redundant: a filter keyed to a constant, or to the wrong
        side of the comparison, can be right in one direction and wrong in the other.
        """
        r = client.get(_url(WS_B), headers=_auth(_uid(ORG_B, Role.OWNER)))
        assert A_NULL not in _ids(r)

    def test_both_organizations_own_org_wide_row_is_visible_to_them(self, client):
        """The other half of the discrimination: exclusion must not be blanket.

        Without this, a query that dropped every NULL-workspace row would pass the two
        leak proofs above while being badly wrong.
        """
        a = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)))
        b = client.get(_url(WS_B), headers=_auth(_uid(ORG_B, Role.OWNER)))
        assert A_NULL in _ids(a)
        assert B_NULL in _ids(b)

    def test_org_wide_visibility_is_exactly_reciprocal(self, client):
        """Both directions in one assertion, over the full five-row corpus."""
        a = _ids(client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER))))
        b = _ids(client.get(_url(WS_B), headers=_auth(_uid(ORG_B, Role.OWNER))))
        assert (a, b) == (VISIBLE_FROM_WS_A, VISIBLE_FROM_WS_B)
        assert a & b == set()

    def test_sibling_workspace_rows_excluded_by_the_workspace_predicate(self, client):
        """Same organization, different workspace.

        Attributed to the workspace predicate, not to tenant isolation: ``aud-a-other-ws``
        shares org A's ``organization_id``, so the organization filter cannot exclude it.
        """
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)))
        assert A_SIBLING not in _ids(r)

    def test_every_allowed_role_sees_the_identical_row_set(self, client):
        """Authorization decides access, not visibility -- the query is role-blind.

        Compared against the expected set rather than only against itself: three empty
        responses are also "identical", and would satisfy a self-comparison.
        """
        seen = {
            role: _ids(client.get(_url(WS_A), headers=_auth(_uid(ORG_A, role))))
            for role in ALLOWED
        }
        assert set(map(frozenset, seen.values())) == {frozenset(VISIBLE_FROM_WS_A)}

    def test_ordering_is_created_at_desc(self, client):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)))
        assert [row["id"] for row in r.json()] == [A_NULL, A_WS]

    def test_limit_is_honoured(self, client):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)), params={"limit": 1})
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_limit_above_bound_is_rejected(self, client):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)), params={"limit": 501})
        assert r.status_code == 422

    def test_response_fields_are_the_documented_eight(self, client):
        r = client.get(_url(WS_A), headers=_auth(_uid(ORG_A, Role.OWNER)))
        assert set(r.json()[0]) == {
            "id",
            "action",
            "actor_user_id",
            "entity_type",
            "entity_id",
            "reason",
            "created_at",
            "context",
        }
