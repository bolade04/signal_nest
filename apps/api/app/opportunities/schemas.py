from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OpportunityCard(BaseModel):
    """Compact representation for the opportunity feed."""

    id: str
    title: str
    classification: str
    decision: str
    opportunity_score: int
    confidence_score: int
    confidence_level: str
    priority_score: int
    relevance_score: int
    risk_level: str
    resolved_market: str | None
    inside_scout_area: bool
    why_it_matters: str | None
    recommended_action: str | None
    audience_fit: str | None
    urgency: str | None
    commercial_value: str | None
    source_summary: list[str]
    status: str
    location_id: str | None
    campaign_id: str | None
    scout_request_id: str
    is_simulated: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ScoreBreakdown(BaseModel):
    kind: str
    total: int
    breakdown: dict


class ValidationEvidenceOut(BaseModel):
    source_type: str
    detail: str
    weight: float
    source_url: str | None = None

    model_config = {"from_attributes": True}


class OpportunityDetail(OpportunityCard):
    who_cares: str | None
    observed_evidence: list[dict]
    ai_inference: str | None
    suggested_angles: list[str]
    risk_note: str | None
    claims_warnings: list[str]
    brand_id: str
    scores: list[ScoreBreakdown] = Field(default_factory=list)
    validation_evidence: list[ValidationEvidenceOut] = Field(default_factory=list)


class OpportunityStatusUpdate(BaseModel):
    status: str  # saved | monitoring | ignored | actioned | new
