"""6B-1: dependency-level proof for the exact-set role primitive (P6-AUTH-6).

Two primitives now live side by side in ``app.auth.dependencies`` and they answer
different questions:

    require_role(*allowed)         -- rank floor: admits every role whose rank is at
                                      or above the LOWEST rank among ``allowed``.
    require_exact_roles(*allowed)  -- exact membership: admits a role only if it is
                                      named in ``allowed``.

This module proves both, at the dependency layer, over the full six-role vocabulary.

The rank-floor half is regression protection for a *semantic* boundary, not a claim
about blast radius. The audit-log route was the only current call site where rank-floor
evaluation admitted roles outside its explicitly named set (``min_rank`` collapsed to
COMPLIANCE_REVIEWER's rank 1, dragging in MARKETER and REVIEWER). The other 21 current
call sites use OWNER / ADMIN / MARKETER, whose named set is already identical to the
current rank-floor result -- converting them would, as things stand, change nothing.

``require_role`` therefore remains the rank-floor primitive and ``require_exact_roles``
is a separate primitive for policies that require literal set membership: the two answer
different authorization questions, and migrating only the audit route kept the blast
radius at zero. ``TestRankFloorPreserved`` pins the rank-floor semantics so a silent
conversion is caught (mutant M6), independently of how many callers it would move today.

The HTTP-boundary proof for the audit route lives in ``test_audit_logs_role_matrix``;
this suite deliberately calls the checkers directly so a failure localises to the
primitive rather than to routing, tenancy, or the test harness.
"""

from __future__ import annotations

import pytest

from app.auth.dependencies import (
    _ROLE_RANK,
    TenantContext,
    require_exact_roles,
    require_role,
)
from app.core.enums import Role
from app.core.errors import PermissionDeniedError

ALL_ROLES = (
    Role.OWNER,
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
)

# The policy the audit-log route enforces (P6-AUTH-3).
AUDIT_ALLOWED = (Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER)
AUDIT_DENIED = (Role.MARKETER, Role.REVIEWER, Role.VIEWER)


def _ctx(role: Role) -> TenantContext:
    """A TenantContext carrying only what a role checker reads.

    ``TenantContext`` is a plain dataclass with no validation, and neither checker
    touches ``user``/``organization``/``workspace`` -- they are resolved upstream by
    ``get_tenant_context`` and merely passed through. Leaving them ``None`` keeps this
    suite a test of the authorization decision alone.
    """
    return TenantContext(user=None, organization=None, workspace=None, role=role)


def _admits(checker, role: Role) -> bool:
    """Decide admission, asserting the pass-through contract on the way through.

    The load-bearing property on the admitting path is *identity*: a checker that
    admits must hand back the very object ``get_tenant_context`` resolved, never a
    reconstructed or substituted one. Downstream handlers read ``ctx.user``,
    ``ctx.organization`` and ``ctx.workspace`` off that object; a checker that built
    its own replacement would silently discard anything the resolver attached.

    ``assert returned.role is role`` cannot carry that proof on its own -- a
    value-equivalent substitute satisfies it -- so identity is asserted first and the
    role check is kept only as a redundant readability aid (mutant M11).
    """
    ctx = _ctx(role)
    try:
        returned = checker(ctx)
    except PermissionDeniedError:
        return False
    assert returned is ctx
    assert returned.role is role
    return True


class TestExactRolePrimitive:
    """require_exact_roles: membership, and nothing but membership."""

    def test_audit_policy_admits_exactly_the_named_three(self):
        checker = require_exact_roles(*AUDIT_ALLOWED)
        admitted = {r for r in ALL_ROLES if _admits(checker, r)}
        assert admitted == set(AUDIT_ALLOWED)

    @pytest.mark.parametrize("role", AUDIT_ALLOWED)
    def test_named_role_allowed(self, role: Role):
        assert _admits(require_exact_roles(*AUDIT_ALLOWED), role) is True

    @pytest.mark.parametrize("role", AUDIT_DENIED)
    def test_unnamed_role_denied(self, role: Role):
        assert _admits(require_exact_roles(*AUDIT_ALLOWED), role) is False

    def test_no_rank_widening_from_below_or_above(self):
        """Naming a single mid-rank role admits neither its juniors nor its seniors.

        Under the rank floor, ``MARKETER`` (rank 2) would drag in ADMIN(3) and
        OWNER(4). Exact membership must not.
        """
        checker = require_exact_roles(Role.MARKETER)
        assert _admits(checker, Role.MARKETER) is True
        for role in (Role.OWNER, Role.ADMIN, Role.REVIEWER, Role.COMPLIANCE_REVIEWER,
                     Role.VIEWER):
            assert _admits(checker, role) is False

    def test_same_rank_roles_are_distinguished(self):
        """REVIEWER and COMPLIANCE_REVIEWER share rank 1; exact semantics separate them.

        This is the precise defect P6-AUTH-3 turned on -- no rank-based primitive can
        tell these two apart.
        """
        assert _ROLE_RANK[Role.REVIEWER] == _ROLE_RANK[Role.COMPLIANCE_REVIEWER]
        checker = require_exact_roles(Role.COMPLIANCE_REVIEWER)
        assert _admits(checker, Role.COMPLIANCE_REVIEWER) is True
        assert _admits(checker, Role.REVIEWER) is False

    def test_owner_is_not_a_superuser(self):
        """The highest rank inherits nothing it was not granted by name."""
        checker = require_exact_roles(Role.COMPLIANCE_REVIEWER)
        assert _admits(checker, Role.OWNER) is False

    def test_admitted_context_is_the_identical_object_not_a_copy(self):
        """``require_exact_roles`` returns its input context, it does not rebuild one.

        Asserted by identity rather than equality because ``TenantContext`` is a plain
        ``@dataclass`` and therefore compares by value: a checker that constructed a
        fresh, field-identical context would satisfy ``==`` while having thrown away
        the resolver's object. The twin below makes that gap explicit.
        """
        ctx = _ctx(Role.ADMIN)
        twin = _ctx(Role.ADMIN)
        assert twin == ctx and twin is not ctx  # equality cannot see a substitution

        returned = require_exact_roles(Role.ADMIN)(ctx)
        assert returned is ctx

    def test_identity_is_preserved_for_every_admitted_role(self):
        checker = require_exact_roles(*AUDIT_ALLOWED)
        for role in AUDIT_ALLOWED:
            ctx = _ctx(role)
            assert checker(ctx) is ctx

    def test_context_fields_are_passed_through_untouched(self):
        """Nothing the resolver attached is dropped, replaced or rewritten.

        Sentinels stand in for the resolved ORM objects; the checker reads only
        ``ctx.role``, so every other attribute must come back identical by identity.
        """
        user, org, workspace = object(), object(), object()
        ctx = TenantContext(
            user=user, organization=org, workspace=workspace, role=Role.OWNER
        )
        returned = require_exact_roles(Role.OWNER)(ctx)
        assert returned is ctx
        assert returned.user is user
        assert returned.organization is org
        assert returned.workspace is workspace
        assert returned.role is Role.OWNER

    def test_empty_allowed_set_fails_at_construction(self):
        """An empty policy is a programming error, caught before the route mounts.

        Mirrors ``require_role()``, whose ``min()`` over an empty iterable also raises
        ValueError at construction rather than admitting or denying at runtime.
        """
        with pytest.raises(ValueError):
            require_exact_roles()
        with pytest.raises(ValueError):
            require_role()

    def test_duplicate_roles_do_not_broaden_access(self):
        """Repeating a role is a no-op, never a widening."""
        once = require_exact_roles(Role.ADMIN)
        twice = require_exact_roles(Role.ADMIN, Role.ADMIN)
        for role in ALL_ROLES:
            assert _admits(twice, role) == _admits(once, role)
        assert _admits(twice, Role.ADMIN) is True
        assert _admits(twice, Role.OWNER) is False

    def test_denial_message_matches_require_role_and_leaks_nothing(self):
        """Denials are indistinguishable between the two primitives.

        The message reaches the client verbatim, so it must name only the requester's
        own role -- never the required set, which would disclose the policy.
        """
        with pytest.raises(PermissionDeniedError) as exact:
            require_exact_roles(Role.OWNER)(_ctx(Role.VIEWER))
        with pytest.raises(PermissionDeniedError) as floor:
            require_role(Role.OWNER)(_ctx(Role.VIEWER))

        assert exact.value.message == floor.value.message
        assert exact.value.message == "Role 'viewer' is not permitted for this action."
        assert exact.value.status_code == 403
        assert exact.value.code == "permission_denied"
        for leaked in ("owner", "admin", "compliance_reviewer", "allowed", "required"):
            assert leaked not in exact.value.message


class TestRankFloorPreserved:
    """require_role remains the established rank-floor primitive.

    The audit-log route is the current exact-set exception; every other call site
    stays on this primitive. These tests pin its semantics, not its caller count.
    """

    def test_marketer_floor_admits_seniors_denies_juniors(self):
        checker = require_role(Role.MARKETER)
        for role in (Role.MARKETER, Role.ADMIN, Role.OWNER):
            assert _admits(checker, role) is True
        for role in (Role.REVIEWER, Role.COMPLIANCE_REVIEWER, Role.VIEWER):
            assert _admits(checker, role) is False

    def test_rank_floor_also_returns_the_identical_context(self):
        """The pass-through contract is shared by both primitives, not new in one."""
        ctx = _ctx(Role.OWNER)
        assert require_role(Role.MARKETER)(ctx) is ctx

    def test_editors_call_shape_is_unchanged(self):
        """The (OWNER, ADMIN, MARKETER) shape used by all 21 other call sites."""
        checker = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)
        admitted = {r for r in ALL_ROLES if _admits(checker, r)}
        assert admitted == {Role.OWNER, Role.ADMIN, Role.MARKETER}

    def test_rank_floor_still_widens_and_that_is_why_audit_moved_off_it(self):
        """Documents the retained widening behaviour of the general primitive.

        ``require_role(OWNER, ADMIN, COMPLIANCE_REVIEWER)`` names three roles and
        admits five, because ``min_rank`` collapses to COMPLIANCE_REVIEWER's rank 1.
        That behaviour is intentional for a hierarchy and is deliberately NOT changed
        here -- it is exactly why the audit route no longer uses this primitive.
        """
        checker = require_role(*AUDIT_ALLOWED)
        admitted = {r for r in ALL_ROLES if _admits(checker, r)}
        assert admitted == {
            Role.OWNER,
            Role.ADMIN,
            Role.COMPLIANCE_REVIEWER,
            Role.MARKETER,
            Role.REVIEWER,
        }
        assert Role.VIEWER not in admitted

    def test_the_two_primitives_disagree_exactly_on_the_widened_pair(self):
        """The delta between the primitives is {MARKETER, REVIEWER} -- the defect."""
        floor = require_role(*AUDIT_ALLOWED)
        exact = require_exact_roles(*AUDIT_ALLOWED)
        delta = {r for r in ALL_ROLES if _admits(floor, r) != _admits(exact, r)}
        assert delta == {Role.MARKETER, Role.REVIEWER}
