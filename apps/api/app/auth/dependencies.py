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
