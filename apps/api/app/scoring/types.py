"""Value objects shared by the scoring engines. Pure data, no framework deps."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BusinessContext:
    """Flattened business/product/audience context used for relevance scoring."""

    keywords: list[str] = field(default_factory=list)
    pain_points: list[str] = field(default_factory=list)
    audiences: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    exclusion_terms: list[str] = field(default_factory=list)
    campaign_goal: str | None = None
    industry: str | None = None


@dataclass
class SignalInput:
    """Normalized signal fields consumed by the scoring engines."""

    content: str
    source_type: str
    signal_type: str | None = None
    pain_point_dna: str | None = None
    engagement: int = 0
    age_days: float = 0.0
    author: str | None = None
    language: str = "en"
    duplicate_count: int = 1  # how many sources agree (cross-source)
    distinct_source_types: int = 1
    has_buying_intent: bool = False
    has_active_ads: bool = False
    news_coverage: bool = False
    search_trend_up: bool = False


@dataclass
class ScoreBreakdown:
    total: int
    factors: dict[str, dict[str, float]]  # factor -> {weight, value, points}

    def as_dict(self) -> dict:
        return {"total": self.total, "factors": self.factors}
