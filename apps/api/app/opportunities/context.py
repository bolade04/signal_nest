"""Build the flattened BusinessContext used by the scoring engines from stored profiles."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brands.models import BusinessProfile
from app.campaign_context.models import (
    AudienceProfile,
    CompetitorProfile,
    ProductProfile,
)
from app.scoring.types import BusinessContext


def build_business_context(db: Session, workspace_id: str) -> BusinessContext:
    profile = db.scalar(
        select(BusinessProfile).where(BusinessProfile.workspace_id == workspace_id)
    )
    products = list(
        db.execute(
            select(ProductProfile).where(ProductProfile.workspace_id == workspace_id)
        ).scalars()
    )
    audiences = list(
        db.execute(
            select(AudienceProfile).where(AudienceProfile.workspace_id == workspace_id)
        ).scalars()
    )
    competitors = list(
        db.execute(
            select(CompetitorProfile).where(CompetitorProfile.workspace_id == workspace_id)
        ).scalars()
    )

    keywords: list[str] = []
    pain_points: list[str] = []
    exclusion: list[str] = []
    for p in products:
        keywords += p.keywords or []
        pain_points += p.pain_points or []
        exclusion += p.exclusion_rules or []
    if profile:
        pain_points += profile.customer_pain_points or []
        for market in profile.markets_served or []:
            keywords.append(market)

    audience_labels = [a.label for a in audiences] or (
        [profile.target_audience] if profile and profile.target_audience else []
    )
    competitor_names = [c.name for c in competitors]

    return BusinessContext(
        keywords=[k for k in keywords if k],
        pain_points=[p for p in pain_points if p],
        audiences=[a for a in audience_labels if a],
        competitors=[c for c in competitor_names if c],
        exclusion_terms=[e for e in exclusion if e],
        campaign_goal=(profile.campaign_goals[0] if profile and profile.campaign_goals else None),
        industry=(profile.industry if profile else None),
    )
