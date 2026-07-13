from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth import service
from app.auth.dependencies import get_current_user
from app.auth.schemas import (
    LoginRequest,
    MembershipOut,
    RegisterRequest,
    SessionOut,
    UserOut,
)
from app.db.session import get_db
from app.organizations.models import Organization, OrganizationMember, User

router = APIRouter(prefix="/auth", tags=["auth"])


def _session(db: Session, user: User) -> SessionOut:
    rows = db.execute(
        select(OrganizationMember, Organization)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(OrganizationMember.user_id == user.id)
    ).all()
    memberships = [
        MembershipOut(organization_id=o.id, organization_name=o.name, role=m.role)
        for m, o in rows
    ]
    return SessionOut(
        access_token=service.issue_token(user),
        user=UserOut(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_operator=user.is_operator,
        ),
        memberships=memberships,
    )


@router.post("/register", response_model=SessionOut, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> SessionOut:
    user = service.register(
        db,
        email=body.email,
        full_name=body.full_name,
        password=body.password,
        org_name=body.organization_name,
    )
    membership = db.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == user.id)
    )
    record_audit(
        db,
        organization_id=membership.organization_id,
        actor_user_id=user.id,
        action="auth.register",
        entity_type="user",
        entity_id=user.id,
    )
    return _session(db, user)


@router.post("/login", response_model=SessionOut)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> SessionOut:
    user = service.authenticate(db, email=body.email, password=body.password)
    return _session(db, user)


@router.get("/me", response_model=SessionOut)
def me(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> SessionOut:
    return _session(db, user)
