"""Brand + business intelligence profile models."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Brand(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "brands"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120))
    business_type: Mapped[str | None] = mapped_column(String(120))


class BusinessProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Structured business-intelligence context (parent-brand level)."""

    __tablename__ = "business_profiles"

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), index=True, nullable=False
    )

    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120))
    business_type: Mapped[str | None] = mapped_column(String(120))

    # Presence — none are required (supports offline / no-presence businesses).
    website: Mapped[str | None] = mapped_column(String(500))
    alternative_presence: Mapped[str | None] = mapped_column(Text)
    social_links: Mapped[dict] = mapped_column(JSON, default=dict)
    google_business_profile: Mapped[str | None] = mapped_column(String(500))
    marketplace_links: Mapped[list] = mapped_column(JSON, default=list)

    description: Mapped[str | None] = mapped_column(Text)
    core_problem_solved: Mapped[str | None] = mapped_column(Text)
    unique_value_proposition: Mapped[str | None] = mapped_column(Text)
    target_audience: Mapped[str | None] = mapped_column(Text)
    ideal_customer_profile: Mapped[str | None] = mapped_column(Text)
    markets_served: Mapped[list] = mapped_column(JSON, default=list)

    customer_pain_points: Mapped[list] = mapped_column(JSON, default=list)
    common_objections: Mapped[list] = mapped_column(JSON, default=list)
    pricing_model: Mapped[str | None] = mapped_column(String(200))
    buying_process: Mapped[str | None] = mapped_column(Text)

    compliance_notes: Mapped[str | None] = mapped_column(Text)
    sensitive_topics: Mapped[list] = mapped_column(JSON, default=list)
    weekly_ad_volume: Mapped[int | None] = mapped_column()
    advertising_budget_preference: Mapped[str | None] = mapped_column(String(120))
    campaign_goals: Mapped[list] = mapped_column(JSON, default=list)
    preferred_platforms: Mapped[list] = mapped_column(JSON, default=list)

    # Onboarding path selected (website_only, social_only, offline_only, none, ...).
    onboarding_path: Mapped[str | None] = mapped_column(String(60))
