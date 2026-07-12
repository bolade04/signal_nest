"""Opportunity + confidence scoring and classification.

Opportunity score (weights): relevance 25, trend 15, discussion 15, commercial 15,
engagement 10, source credibility 10, recency 10.

Confidence score (weights): evidence quantity 20, evidence diversity 20,
source reliability 20, signal clarity 15, score consistency 15, recency reliability 10.
"""

from __future__ import annotations

from app.core.enums import ConfidenceLevel, OpportunityClassification
from app.scoring.types import ScoreBreakdown, SignalInput

# Source credibility priors (0..1).
_SOURCE_CREDIBILITY = {
    "rss_news": 0.85,
    "reviews": 0.8,
    "google_trends": 0.75,
    "meta_ad_library": 0.8,
    "tiktok_creative_center": 0.7,
    "competitor_scan": 0.7,
    "website_scan": 0.7,
    "reddit": 0.55,
    "manual": 0.6,
}

_OPPORTUNITY_WEIGHTS = {
    "relevance": 25,
    "trend": 15,
    "discussion": 15,
    "commercial": 15,
    "engagement": 10,
    "source_credibility": 10,
    "recency": 10,
}

_CONFIDENCE_WEIGHTS = {
    "evidence_quantity": 20,
    "evidence_diversity": 20,
    "source_reliability": 20,
    "signal_clarity": 15,
    "score_consistency": 15,
    "recency_reliability": 10,
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _weighted(values: dict[str, float], weights: dict[str, int]) -> ScoreBreakdown:
    factors: dict[str, dict[str, float]] = {}
    total = 0.0
    for name, weight in weights.items():
        value = _clamp01(values.get(name, 0.0))
        points = round(value * weight, 2)
        factors[name] = {"weight": weight, "value": round(value, 3), "points": points}
        total += points
    return ScoreBreakdown(total=int(round(total)), factors=factors)


def score_opportunity(
    signal: SignalInput, relevance_score: int, validation_score: int
) -> ScoreBreakdown:
    trend = 0.0
    if signal.search_trend_up:
        trend += 0.6
    if signal.signal_type in {"trend_discussion", "positive_trend", "seasonal_opportunity"}:
        trend += 0.4
    if signal.news_coverage:
        trend += 0.2

    discussion = _clamp01(
        0.5 * min(1.0, signal.duplicate_count / 6.0)
        + 0.5 * min(1.0, signal.distinct_source_types / 3.0)
    )
    commercial = validation_score / 100.0
    if signal.has_buying_intent:
        commercial = _clamp01(commercial + 0.15)
    engagement = _clamp01(signal.engagement / 100.0)
    source_credibility = _SOURCE_CREDIBILITY.get(signal.source_type, 0.5)
    recency = _clamp01(1.0 - signal.age_days / 90.0)

    values = {
        "relevance": relevance_score / 100.0,
        "trend": _clamp01(trend),
        "discussion": discussion,
        "commercial": commercial,
        "engagement": engagement,
        "source_credibility": source_credibility,
        "recency": recency,
    }
    return _weighted(values, _OPPORTUNITY_WEIGHTS)


def score_confidence(
    signal: SignalInput,
    evidence_count: int,
    opportunity_score: int,
    relevance_score: int,
    validation_score: int,
) -> ScoreBreakdown:
    evidence_quantity = min(1.0, evidence_count / 5.0)
    evidence_diversity = min(1.0, signal.distinct_source_types / 3.0)
    source_reliability = _SOURCE_CREDIBILITY.get(signal.source_type, 0.5)
    # Clarity: classified signal type + non-trivial content.
    signal_clarity = 0.4
    if signal.signal_type:
        signal_clarity += 0.35
    if len(signal.content) > 60:
        signal_clarity += 0.25
    # Consistency: do the three scores agree in direction?
    spread = max(opportunity_score, relevance_score, validation_score) - min(
        opportunity_score, relevance_score, validation_score
    )
    score_consistency = _clamp01(1.0 - spread / 100.0)
    recency_reliability = _clamp01(1.0 - signal.age_days / 120.0)

    values = {
        "evidence_quantity": evidence_quantity,
        "evidence_diversity": evidence_diversity,
        "source_reliability": source_reliability,
        "signal_clarity": _clamp01(signal_clarity),
        "score_consistency": score_consistency,
        "recency_reliability": recency_reliability,
    }
    return _weighted(values, _CONFIDENCE_WEIGHTS)


def classify_opportunity(opportunity_score: int) -> OpportunityClassification:
    if opportunity_score < 25:
        return OpportunityClassification.NOISE
    if opportunity_score < 40:
        return OpportunityClassification.DISCUSSION_ONLY
    if opportunity_score < 55:
        return OpportunityClassification.WEAK
    if opportunity_score < 70:
        return OpportunityClassification.EARLY
    if opportunity_score < 85:
        return OpportunityClassification.VALIDATED
    return OpportunityClassification.HIGH_PRIORITY


def confidence_level(confidence_score: int) -> ConfidenceLevel:
    if confidence_score < 40:
        return ConfidenceLevel.LOW
    if confidence_score < 70:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.HIGH
