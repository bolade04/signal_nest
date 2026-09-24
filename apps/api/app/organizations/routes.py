from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    OrganizationContext,
    get_current_user,
    get_organization_context,
    require_exact_organization_roles,
)
from app.core.enums import Role
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.session import get_db
from app.organizations import invitations as invitation_service
from app.organizations import members as member_service
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.organizations.schemas import (
    InvitationCreate,
    InvitationCreatedOut,
    InvitationOut,
    MemberRoleUpdate,
    OrganizationMemberOut,
    OrganizationOut,
    WorkspaceCreate,
    WorkspaceOut,
)

router = APIRouter(tags=["organizations"])

#: Organization administration: exactly OWNER and ADMIN, by name (no rank floor).
ORGANIZATION_ADMINS = require_exact_organization_roles(Role.OWNER, Role.ADMIN)


def _assert_member(db: Session, user_id: str, org_id: str) -> OrganizationMember:
    m = db.scalar(
        select(OrganizationMember).where(
            OrganizationMember.user_id == user_id,
            OrganizationMember.organization_id == org_id,
        )
    )
    if not m:
        raise PermissionDeniedError("Not a member of this organization.")
    return m


@router.get("/organizations", response_model=list[OrganizationOut])
def list_organizations(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[Organization]:
    rows = db.execute(
        select(Organization)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .where(OrganizationMember.user_id == user.id)
    ).scalars()
    return list(rows)


@router.get("/organizations/{organization_id}/workspaces", response_model=list[WorkspaceOut])
def list_workspaces(
    organization_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Workspace]:
    _assert_member(db, user.id, organization_id)
    rows = db.execute(
        select(Workspace).where(Workspace.organization_id == organization_id)
    ).scalars()
    return list(rows)


@router.post(
    "/organizations/{organization_id}/workspaces",
    response_model=WorkspaceOut,
    status_code=201,
)
def create_workspace(
    body: WorkspaceCreate,
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(require_exact_organization_roles(Role.OWNER, Role.ADMIN)),
) -> Workspace:
    # Membership, organization existence and the exact OWNER/ADMIN check are done by the
    # dependency; the organization written to is the one it authorized.
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or "workspace"
    if db.scalar(
        select(Workspace).where(
            Workspace.organization_id == ctx.organization.id, Workspace.slug == slug
        )
    ):
        slug = f"{slug}-{len(slug)}"
    ws = Workspace(organization_id=ctx.organization.id, name=body.name, slug=slug)
    db.add(ws)
    db.flush()
    return ws


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Workspace:
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise NotFoundError("Workspace not found.")
    _assert_member(db, user.id, ws.organization_id)
    return ws


# --- Invitations -------------------------------------------------------------------------
# Every mutating route commits before it returns: a success response is only sent for a
# committed change, never one that the post-response teardown could still lose.


@router.post(
    "/organizations/{organization_id}/invitations",
    response_model=InvitationCreatedOut,
    status_code=201,
)
def create_invitation(
    body: InvitationCreate,
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_ADMINS),
) -> InvitationCreatedOut:
    invitation, token = invitation_service.create_invitation(
        db, ctx=ctx, email=body.email, role=Role(body.role)
    )
    db.commit()
    # The raw token appears in this response and nowhere else.
    return InvitationCreatedOut(
        **InvitationOut.model_validate(invitation).model_dump(), token=token
    )


@router.get(
    "/organizations/{organization_id}/invitations",
    response_model=list[InvitationOut],
)
def list_invitations(
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_ADMINS),
) -> list[InvitationOut]:
    rows = invitation_service.list_pending_invitations(db, organization_id=ctx.organization.id)
    return [InvitationOut.model_validate(row) for row in rows]


@router.delete(
    "/organizations/{organization_id}/invitations/{invitation_id}",
    status_code=204,
)
def revoke_invitation(
    invitation_id: str,
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_ADMINS),
) -> Response:
    invitation_service.revoke_invitation(db, ctx=ctx, invitation_id=invitation_id)
    db.commit()
    return Response(status_code=204)


# --- Members -----------------------------------------------------------------------------


@router.get(
    "/organizations/{organization_id}/members",
    response_model=list[OrganizationMemberOut],
)
def list_members(
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(get_organization_context),
) -> list[OrganizationMemberOut]:
    # Any member may read the member list; a non-member is refused by the context (403).
    rows = member_service.list_members(db, organization_id=ctx.organization.id)
    return [OrganizationMemberOut.model_validate(row) for row in rows]


@router.put(
    "/organizations/{organization_id}/members/{user_id}/role",
    response_model=OrganizationMemberOut,
)
def change_member_role(
    user_id: str,
    body: MemberRoleUpdate,
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_ADMINS),
) -> OrganizationMemberOut:
    row = member_service.change_member_role(db, ctx=ctx, target_user_id=user_id, new_role=body.role)
    db.commit()
    return OrganizationMemberOut.model_validate(row)


@router.delete(
    "/organizations/{organization_id}/members/{user_id}",
    status_code=204,
)
def remove_member(
    user_id: str,
    db: Session = Depends(get_db),
    ctx: OrganizationContext = Depends(ORGANIZATION_ADMINS),
) -> Response:
    member_service.remove_member(db, ctx=ctx, target_user_id=user_id)
    db.commit()
    return Response(status_code=204)
