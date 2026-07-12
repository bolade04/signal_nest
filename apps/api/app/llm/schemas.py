"""Per-task output schemas. The service validates every provider response against these,
so mock/openai/anthropic are guaranteed to return the same normalized shape."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClassifySignalOut(BaseModel):
    signal_type: str
    pain_point_dna: str | None = None
    sentiment: str = "neutral"
    audience_fit_label: str | None = None
    is_actionable: bool = True
    rationale: str = ""


class ExplainOpportunityOut(BaseModel):
    why_it_matters: str
    who_cares: str
    ai_inference: str
    recommended_action: str
    risk_note: str = ""
    suggested_angles: list[str] = Field(default_factory=list)


class ClaimRiskReviewOut(BaseModel):
    risk_level: str
    warnings: list[str] = Field(default_factory=list)
    safe_alternative: str = ""


class SummarizeOut(BaseModel):
    summary: str


class GeoReasoningOut(BaseModel):
    resolved_market: str | None = None
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


TASK_SCHEMAS: dict[str, type[BaseModel]] = {
    "classify_signal": ClassifySignalOut,
    "explain_opportunity": ExplainOpportunityOut,
    "check_claim_safety": ClaimRiskReviewOut,
    "summarize_website": SummarizeOut,
    "geo_reasoning": GeoReasoningOut,
}
