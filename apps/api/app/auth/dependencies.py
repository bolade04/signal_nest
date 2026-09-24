"""Auth + tenancy dependencies.

Server-side enforcement of identity, organization membership, workspace ownership and
role. Client-supplied tenant IDs are never trusted: every workspace resolves to its
organization and the user's membership/role is verified here.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import Role
from app.core.errors import AuthError, NotFoundError, PermissionDeniedError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.organizations.models import Organization, OrganizationMember, User, Workspace

# Role hierarchy for permission checks (higher = more privilege).
_ROLE_RANK = {
    Role.VIEWER: 0,
    Role.REVIEWER: 1,
    Role.COMPLIANCE_REVIEWER: 1,
    Role.MARKETER: 2,
    Role.ADMIN: 3,
    Role.OWNER: 4,
}


@dataclass
class TenantContext:
    user: User
    organization: Organization
    workspace: Workspace
    role: Role


@dataclass
class OrganizationContext:
    """Resolved context for an organization-scoped request that has no workspace."""

    user: User
    organization: Organization
    role: Role


def get_current_user(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("Missing bearer token.")
    token = authorization.split(" ", 1)[1]
    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise AuthError("Invalid or expired token.")
    user = db.get(User, payload["sub"])
    if not user or not user.is_active:
        raise AuthError("User not found or inactive.")
    return user


def require_operator(user: User = Depends(get_current_user)) -> User:
    """Authorize a platform-operator request.

    Operator status is a server-controlled attribute on the user; it is never
    derived from client input, email domain, or organization role. Anonymous
    callers are rejected by ``get_current_user`` (401); an authenticated
    non-operator is rejected here (403). This gates detailed infrastructure
    introspection so runtime topology is not exposed to ordinary customers.
    """
    if not user.is_operator:
        raise PermissionDeniedError("Operator privileges are required for this resource.")
    return user


def _membership(db: Session, user_id: str, org_id: str) -> OrganizationMember:
    m = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == org_id,
        )
    )
    if not m:
        raise PermissionDeniedError("You are not a member of this organization.")
    return m


def get_tenant_context(
    workspace_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TenantContext:
    """Resolve and authorize a workspace-scoped request."""
    workspace = db.get(Workspace, workspace_id)
    if not workspace:
        raise NotFoundError("Workspace not found.")
    membership = _membership(db, user.id, workspace.organization_id)
    organization = db.get(Organization, workspace.organization_id)
    return TenantContext(
        user=user,
        organization=organization,
        workspace=workspace,
        role=Role(membership.role),
    )


def get_organization_context(
    organization_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OrganizationContext:
    """Resolve and authorize an organization-scoped request.

    Organization-level counterpart of :func:`get_tenant_context`, for
    organization-administration routes that have no workspace. ``organization_id`` is
    the route's path parameter; it only selects which membership to look up.

    The order of checks is fixed: authentication (``get_current_user``, 401), then
    membership via the shared ``_membership`` (403, "You are not a member of this
    organization."), then organization existence (404). Membership is checked before
    existence, so a caller with no membership gets the same 403 for an organization
    that exists and one that does not. The role is read from the persisted membership
    row, never from client input.
    """
    membership = _membership(db, user.id, organization_id)
    organization = db.get(Organization, organization_id)
    if not organization:
        raise NotFoundError("Organization not found.")
    return OrganizationContext(
        user=user,
        organization=organization,
        role=Role(membership.role),
    )


def require_exact_roles(*allowed: Role):
    """Dependency factory admitting only the roles explicitly named.

    Separation-of-duties counterpart to :func:`require_role`. ``require_role`` is a
    *rank floor*: it admits every role at or above the lowest rank among ``allowed``,
    so naming a low-ranked role silently admits every higher one. That is correct for
    a privilege hierarchy and wrong for a duty boundary, where a role must be granted
    access by name rather than inherit it by seniority.

    This factory performs exact membership only: ``ctx.role in allowed``. Rank plays
    no part -- no inheritance, no widening, no floor. ``require_role`` is deliberately
    left unchanged; the two answer different authorization questions and both are in
    use.

    Raises:
        ValueError: at construction time if no role is supplied. An empty allowed set
            is a programming error, and failing when the dependency is built keeps a
            route that would admit nobody from ever being mounted. This mirrors
            ``require_role``, whose ``min()`` over an empty iterable raises the same
            error at construction.
    """
    if not allowed:
        raise ValueError("require_exact_roles() requires at least one role.")
    # frozenset normalises duplicates: repeating a role is a no-op, never a widening.
    permitted = frozenset(allowed)

    def _checker(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if ctx.role not in permitted:
            raise PermissionDeniedError(
                f"Role '{ctx.role.value}' is not permitted for this action."
            )
        return ctx

    return _checker


def require_exact_organization_roles(*allowed: Role):
    """Dependency factory admitting only the roles explicitly named, per organization.

    Organization-scoped counterpart of :func:`require_exact_roles`, for
    organization-administration routes that have no workspace. The context comes from
    :func:`get_organization_context` rather than ``get_tenant_context``, so the route
    takes no ``workspace_id``.

    The decision is the same exact membership: ``ctx.role in allowed``. Rank plays no
    part -- no inheritance, no widening, no floor. An admitted context is returned
    unchanged.

    Raises:
        ValueError: at construction time if no role is supplied, for the same reason
            as ``require_exact_roles``: a route that would admit nobody is never
            mounted.
    """
    if not allowed:
        raise ValueError("require_exact_organization_roles() requires at least one role.")
    # frozenset normalises duplicates: repeating a role is a no-op, never a widening.
    permitted = frozenset(allowed)

    def _checker(
        ctx: OrganizationContext = Depends(get_organization_context),
    ) -> OrganizationContext:
        if ctx.role not in permitted:
            raise PermissionDeniedError(
                f"Role '{ctx.role.value}' is not permitted for this action."
            )
        return ctx

    return _checker


def require_role(*allowed: Role):
    """Dependency factory enforcing a minimum set of roles for a workspace request."""
    min_rank = min(_ROLE_RANK[r] for r in allowed)

    def _checker(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if _ROLE_RANK[ctx.role] < min_rank and ctx.role not in allowed:
            raise PermissionDeniedError(
                f"Role '{ctx.role.value}' is not permitted for this action."
            )
        return ctx

    return _checker
