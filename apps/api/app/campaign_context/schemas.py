from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProductIn(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    audience: str | None = None
    pain_points: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclusion_rules: list[str] = Field(default_factory=list)
    relevance_weight: float = 1.0
    claims_rules: dict = Field(default_factory=dict)


class AudienceIn(BaseModel):
    label: str = Field(min_length=1)
    description: str | None = None
    demographics: dict = Field(default_factory=dict)
    motivations: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class CompetitorIn(BaseModel):
    name: str = Field(min_length=1)
    website: str | None = None
    known_weaknesses: list[str] = Field(default_factory=list)
    notes: str | None = None


class BrandVoiceIn(BaseModel):
    tone: list[str] = Field(default_factory=list)
    personality: list[str] = Field(default_factory=list)
    do_use: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    example_copy: str | None = None


class OfferIn(BaseModel):
    name: str = Field(min_length=1)
    product_service: str | None = None
    discount_amount: float | None = None
    percentage_discount: float | None = None
    sale_price: float | None = None
    regular_price: float | None = None
    promo_code: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    eligible_location_ids: list[str] = Field(default_factory=list)
    terms: str | None = None
    required_disclaimer: str | None = None
    cta: str | None = None
    landing_url: str | None = None
    is_active: bool = True


class ClaimIn(BaseModel):
    text: str = Field(min_length=1)
    kind: str = "approved"  # approved | restricted | blocked | required_disclaimer
    category: str | None = None
    risk_level: str = "low"
    notes: str | None = None


class SourcePrefIn(BaseModel):
    source_type: str
    enabled: bool = True
    config: dict = Field(default_factory=dict)


class ChannelPrefIn(BaseModel):
    channel: str
    enabled: bool = True
    weekly_volume: int | None = None


class CampaignIn(BaseModel):
    name: str = Field(min_length=1)
    goal: str | None = None
    mode: str = "per_location"
    location_ids: list[str] = Field(default_factory=list)
    status: str = "active"
