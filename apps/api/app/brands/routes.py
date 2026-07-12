from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands import service
from app.brands.models import Brand, BusinessProfile
from app.brands.schemas import (
    BrandOut,
    BusinessProfileBase,
    BusinessProfileOut,
    OnboardingRequest,
    OnboardingResult,
)
from app.core.enums import Role
from app.core.errors import NotFoundError
from app.db.session import get_db

router = APIRouter(tags=["brands"])


@router.post(
    "/workspaces/{workspace_id}/onboarding",
    response_model=OnboardingResult,
    status_code=201,
)
def onboard(
    workspace_id: str,
    body: OnboardingRequest,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)),
) -> OnboardingResult:
    brand, profile = service.run_onboarding(db, ctx, body)
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="onboarding.completed",
        entity_type="brand",
        entity_id=brand.id,
    )
    return OnboardingResult(
        brand=BrandOut.model_validate(brand),
        business_profile=BusinessProfileOut.model_validate(profile),
        workspace_id=workspace_id,
        onboarding_completed=True,
    )


@router.get("/workspaces/{workspace_id}/brands", response_model=list[BrandOut])
def list_brands(
    workspace_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[Brand]:
    rows = db.execute(select(Brand).where(Brand.workspace_id == workspace_id)).scalars()
    return list(rows)


@router.get("/workspaces/{workspace_id}/business-profile", response_model=BusinessProfileOut)
def get_business_profile(
    workspace_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> BusinessProfile:
    profile = db.scalar(
        select(BusinessProfile).where(BusinessProfile.workspace_id == workspace_id)
    )
    if not profile:
        raise NotFoundError("No business profile for this workspace yet.")
    return profile


@router.put("/workspaces/{workspace_id}/business-profile", response_model=BusinessProfileOut)
def update_business_profile(
    workspace_id: str,
    body: BusinessProfileBase,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)),
) -> BusinessProfile:
    profile = db.scalar(
        select(BusinessProfile).where(BusinessProfile.workspace_id == workspace_id)
    )
    if not profile:
        raise NotFoundError("No business profile for this workspace yet.")
    for key, value in body.model_dump().items():
        setattr(profile, key, value)
    db.add(profile)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="business_profile.updated",
        entity_type="business_profile",
        entity_id=profile.id,
    )
    return profile
