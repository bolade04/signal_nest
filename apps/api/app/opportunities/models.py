"""Opportunity models: opportunities, score breakdowns, validation evidence."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.enums import OpportunityStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Opportunity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "opportunities"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    cluster_id: Mapped[str | None] = mapped_column(
        ForeignKey("signal_clusters.id", ondelete="SET NULL"), index=True
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)

    opportunity_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence_level: Mapped[str] = mapped_column(String(20), default="low")
    priority_score: Mapped[int] = mapped_column(Integer, default=0)
    relevance_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_level: Mapped[str] = mapped_column(String(20), default="low")

    resolved_market: Mapped[str | None] = mapped_column(String(200))
    inside_scout_area: Mapped[bool] = mapped_column(Boolean, default=True)

    # Explanation engine output (observed / inference / action are separated).
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    who_cares: Mapped[str | None] = mapped_column(Text)
    observed_evidence: Mapped[list] = mapped_column(JSON, default=list)
    ai_inference: Mapped[str | None] = mapped_column(Text)
    recommended_action: Mapped[str | None] = mapped_column(Text)
    suggested_angles: Mapped[list] = mapped_column(JSON, default=list)
    risk_note: Mapped[str | None] = mapped_column(Text)
    claims_warnings: Mapped[list] = mapped_column(JSON, default=list)
    audience_fit: Mapped[str | None] = mapped_column(String(300))
    urgency: Mapped[str | None] = mapped_column(String(40))
    commercial_value: Mapped[str | None] = mapped_column(String(40))
    source_summary: Mapped[list] = mapped_column(JSON, default=list)

    status: Mapped[str] = mapped_column(String(40), default=OpportunityStatus.NEW.value)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)


class OpportunityScore(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Explainable per-factor score breakdown (opportunity + confidence)."""

    __tablename__ = "opportunity_scores"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # opportunity | confidence
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)  # factor -> {weight, value, points}
    total: Mapped[int] = mapped_column(Integer, default=0)


class ValidationEvidence(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "validation_evidence"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    source_url: Mapped[str | None] = mapped_column(String(1000))
