from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BusinessProfileBase(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    industry: str | None = None
    business_type: str | None = None
    website: str | None = None
    alternative_presence: str | None = None
    social_links: dict[str, str] = Field(default_factory=dict)
    google_business_profile: str | None = None
    marketplace_links: list[str] = Field(default_factory=list)
    description: str | None = None
    core_problem_solved: str | None = None
    unique_value_proposition: str | None = None
    target_audience: str | None = None
    ideal_customer_profile: str | None = None
    markets_served: list[str] = Field(default_factory=list)
    customer_pain_points: list[str] = Field(default_factory=list)
    common_objections: list[str] = Field(default_factory=list)
    pricing_model: str | None = None
    buying_process: str | None = None
    compliance_notes: str | None = None
    sensitive_topics: list[str] = Field(default_factory=list)
    weekly_ad_volume: int | None = None
    advertising_budget_preference: str | None = None
    campaign_goals: list[str] = Field(default_factory=list)
    preferred_platforms: list[str] = Field(default_factory=list)
    onboarding_path: str | None = None


class BusinessProfileOut(BusinessProfileBase):
    id: str
    brand_id: str
    workspace_id: str

    class Config:
        from_attributes = True


class BrandOut(BaseModel):
    id: str
    name: str
    industry: str | None
    business_type: str | None

    class Config:
        from_attributes = True


class OnboardingRequest(BaseModel):
    """Full guided-onboarding payload. No presence field is required."""

    brand_name: str = Field(min_length=1, max_length=200)
    profile: BusinessProfileBase
    # Optional inline sub-context created during onboarding.
    products: list[dict[str, Any]] = Field(default_factory=list)
    competitors: list[dict[str, Any]] = Field(default_factory=list)
    audiences: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    preferred_source_types: list[str] = Field(default_factory=list)


class OnboardingResult(BaseModel):
    brand: BrandOut
    business_profile: BusinessProfileOut
    workspace_id: str
    onboarding_completed: bool
