from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.core.errors import NotFoundError, PermissionDeniedError
from app.db.session import get_db
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.organizations.schemas import OrganizationOut, WorkspaceCreate, WorkspaceOut

router = APIRouter(tags=["organizations"])


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
    organization_id: str,
    body: WorkspaceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Workspace:
    _assert_member(db, user.id, organization_id)
    if not db.get(Organization, organization_id):
        raise NotFoundError("Organization not found.")
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") or "workspace"
    if db.scalar(
        select(Workspace).where(
            Workspace.organization_id == organization_id, Workspace.slug == slug
        )
    ):
        slug = f"{slug}-{len(slug)}"
    ws = Workspace(organization_id=organization_id, name=body.name, slug=slug)
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
