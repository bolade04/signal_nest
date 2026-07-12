"""Onboarding + brand/business-profile services."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext
from app.brands.models import Brand, BusinessProfile
from app.brands.schemas import BusinessProfileBase, OnboardingRequest
from app.campaign_context.models import (
    AudienceProfile,
    ClaimsLibraryEntry,
    CompetitorProfile,
    ProductProfile,
    SourcePreference,
)


def _profile_columns(profile: BusinessProfileBase) -> dict:
    return profile.model_dump()


def run_onboarding(
    db: Session, ctx: TenantContext, body: OnboardingRequest
) -> tuple[Brand, BusinessProfile]:
    org_id = ctx.organization.id
    ws_id = ctx.workspace.id

    brand = Brand(
        organization_id=org_id,
        workspace_id=ws_id,
        name=body.brand_name,
        industry=body.profile.industry,
        business_type=body.profile.business_type,
    )
    db.add(brand)
    db.flush()

    profile = BusinessProfile(
        organization_id=org_id,
        workspace_id=ws_id,
        brand_id=brand.id,
        **_profile_columns(body.profile),
    )
    db.add(profile)
    db.flush()

    for p in body.products:
        db.add(
            ProductProfile(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                name=p.get("name", "Product"),
                description=p.get("description"),
                audience=p.get("audience"),
                pain_points=p.get("pain_points", []),
                use_cases=p.get("use_cases", []),
                competitors=p.get("competitors", []),
                keywords=p.get("keywords", []),
                exclusion_rules=p.get("exclusion_rules", []),
                claims_rules=p.get("claims_rules", {}),
            )
        )
    for c in body.competitors:
        db.add(
            CompetitorProfile(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                name=c.get("name", "Competitor"),
                website=c.get("website"),
                known_weaknesses=c.get("known_weaknesses", []),
                notes=c.get("notes"),
            )
        )
    for a in body.audiences:
        db.add(
            AudienceProfile(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                label=a.get("label", "Target audience"),
                description=a.get("description"),
                keywords=a.get("keywords", []),
                objections=a.get("objections", []),
            )
        )
    for cl in body.claims:
        db.add(
            ClaimsLibraryEntry(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                text=cl.get("text", ""),
                kind=cl.get("kind", "approved"),
                category=cl.get("category"),
                risk_level=cl.get("risk_level", "low"),
            )
        )
    for st in body.preferred_source_types:
        db.add(
            SourcePreference(
                organization_id=org_id, workspace_id=ws_id, brand_id=brand.id,
                source_type=st, enabled=True,
            )
        )

    ctx.workspace.onboarding_completed = True
    db.add(ctx.workspace)
    db.flush()
    return brand, profile


def get_primary_brand(db: Session, workspace_id: str) -> Brand | None:
    return db.scalar(
        select(Brand).where(Brand.workspace_id == workspace_id).order_by(Brand.created_at)
    )
