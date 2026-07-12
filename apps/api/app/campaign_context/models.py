"""Campaign Context Center models: products, audiences, competitors, brand voice,
offers, claims library, source/channel preferences, campaigns."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class _WorkspaceScoped(UUIDPrimaryKeyMixin, TimestampMixin):
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), index=True, nullable=False
    )


class ProductProfile(Base, _WorkspaceScoped):
    __tablename__ = "product_profiles"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    pain_points: Mapped[list] = mapped_column(JSON, default=list)
    use_cases: Mapped[list] = mapped_column(JSON, default=list)
    competitors: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    exclusion_rules: Mapped[list] = mapped_column(JSON, default=list)
    relevance_weight: Mapped[float] = mapped_column(Float, default=1.0)
    claims_rules: Mapped[dict] = mapped_column(JSON, default=dict)


class AudienceProfile(Base, _WorkspaceScoped):
    __tablename__ = "audience_profiles"

    label: Mapped[str] = mapped_column(String(200), nullable=False)  # specific, not "consumers"
    description: Mapped[str | None] = mapped_column(Text)
    demographics: Mapped[dict] = mapped_column(JSON, default=dict)
    motivations: Mapped[list] = mapped_column(JSON, default=list)
    objections: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)


class CompetitorProfile(Base, _WorkspaceScoped):
    __tablename__ = "competitor_profiles"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    website: Mapped[str | None] = mapped_column(String(500))
    known_weaknesses: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)


class BrandVoiceProfile(Base, _WorkspaceScoped):
    __tablename__ = "brand_voice_profiles"

    tone: Mapped[list] = mapped_column(JSON, default=list)
    personality: Mapped[list] = mapped_column(JSON, default=list)
    do_use: Mapped[list] = mapped_column(JSON, default=list)
    avoid: Mapped[list] = mapped_column(JSON, default=list)
    example_copy: Mapped[str | None] = mapped_column(Text)


class OfferCalendarEntry(Base, _WorkspaceScoped):
    __tablename__ = "offer_calendar"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    product_service: Mapped[str | None] = mapped_column(String(200))
    discount_amount: Mapped[float | None] = mapped_column(Float)
    percentage_discount: Mapped[float | None] = mapped_column(Float)
    sale_price: Mapped[float | None] = mapped_column(Float)
    regular_price: Mapped[float | None] = mapped_column(Float)
    promo_code: Mapped[str | None] = mapped_column(String(80))
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eligible_location_ids: Mapped[list] = mapped_column(JSON, default=list)
    terms: Mapped[str | None] = mapped_column(Text)
    required_disclaimer: Mapped[str | None] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(String(200))
    landing_url: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ClaimsLibraryEntry(Base, _WorkspaceScoped):
    __tablename__ = "claims_library"

    text: Mapped[str] = mapped_column(Text, nullable=False)
    # kind: approved | restricted | blocked | required_disclaimer
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))  # health, financial, comparative...
    risk_level: Mapped[str] = mapped_column(String(20), default="low")
    notes: Mapped[str | None] = mapped_column(Text)


class SourcePreference(Base, _WorkspaceScoped):
    __tablename__ = "source_preferences"

    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class ChannelPreference(Base, _WorkspaceScoped):
    __tablename__ = "channel_preferences"

    channel: Mapped[str] = mapped_column(String(60), nullable=False)  # facebook, instagram...
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weekly_volume: Mapped[int | None] = mapped_column(Integer)


class Campaign(Base, _WorkspaceScoped):
    __tablename__ = "campaigns"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(40), default="per_location")
    location_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default="active")
