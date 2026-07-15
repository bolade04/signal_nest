"""Versioned, explainable opportunity scoring.

The composite 0..100 score is a weighted blend of eight clamped factors. Every
breakdown carries the :data:`SCORING_VERSION` that produced it, so a persisted or
displayed score is always interpretable against its exact formula even after the
weights evolve. The factors deliberately reuse the existing engines' priors
(``_SOURCE_CREDIBILITY``, cross-source ``score_validation``, the relevance
action-floor) rather than inventing parallel definitions.

The function is pure and deterministic: identical inputs always yield an identical
breakdown and total.
"""

from __future__ import annotations

from app.intelligence.models import (
    BusinessRelevance,
    ExtractedIntelligence,
    IntelligenceScore,
    SignalFacts,
)
from app.scoring.opportunity import _SOURCE_CREDIBILITY, classify_opportunity
from app.scoring.types import SignalInput
from app.scoring.validation import score_validation

#: Bump when factor weights or definitions change. Persisted with every breakdown.
SCORING_VERSION = "3b.1"

_WEIGHTS: dict[str, int] = {
    "source_quality": 15,
    "recency": 10,
    "evidence_strength": 20,
    "urgency": 10,
    "business_fit": 20,
    "market_fit": 10,
    "commercial_usefulness": 10,
    "confidence": 5,
}
assert sum(_WEIGHTS.values()) == 100


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _confidence_factor(
    facts: SignalFacts, intelligence: ExtractedIntelligence, evidence_count: int
) -> float:
    conf = 0.3
    if intelligence.signal_type is not None:
        conf += 0.25 * intelligence.signal_type.confidence
    if facts.word_count >= 12:
        conf += 0.2
    conf += min(0.25, 0.05 * evidence_count)
    return _clamp01(conf)


def score_candidate(
    facts: SignalFacts,
    intelligence: ExtractedIntelligence,
    relevance: BusinessRelevance,
    *,
    inside_scout_area: bool,
) -> IntelligenceScore:
    """Compute the versioned composite score with a per-factor breakdown."""
    evidence_count = len(intelligence.all_evidence)

    validation_input = SignalInput(
        content=facts.excerpt,
        source_type=facts.source_type,
        signal_type=intelligence.signal_type.value if intelligence.signal_type else None,
        engagement=facts.engagement,
        age_days=facts.published_days_ago,
        duplicate_count=facts.duplicate_count,
        distinct_source_types=facts.distinct_source_types,
        has_buying_intent=intelligence.has_buying_intent,
    )
    validation_score, _ = score_validation(validation_input)

    source_quality = _SOURCE_CREDIBILITY.get(facts.source_type, 0.5)
    recency = _clamp01(1.0 - facts.published_days_ago / 90.0)
    evidence_strength = _clamp01(
        0.6 * min(1.0, facts.distinct_source_types / 3.0)
        + 0.4 * min(1.0, facts.duplicate_count / 6.0)
    )
    urgency = 0.0
    if intelligence.has_buying_intent:
        urgency += 0.6
    if intelligence.signal_type and intelligence.signal_type.value in {
        "complaint",
        "competitor_dissatisfaction",
    }:
        urgency += 0.3
    urgency = _clamp01(urgency)
    business_fit = _clamp01(relevance.score / 100.0)
    market_fit = 1.0 if inside_scout_area else 0.0
    commercial_usefulness = _clamp01(validation_score / 100.0)
    if intelligence.has_buying_intent:
        commercial_usefulness = _clamp01(commercial_usefulness + 0.15)
    confidence = _confidence_factor(facts, intelligence, evidence_count)

    values = {
        "source_quality": source_quality,
        "recency": recency,
        "evidence_strength": evidence_strength,
        "urgency": urgency,
        "business_fit": business_fit,
        "market_fit": market_fit,
        "commercial_usefulness": commercial_usefulness,
        "confidence": confidence,
    }

    factors: dict[str, dict[str, float]] = {}
    total = 0.0
    for name, weight in _WEIGHTS.items():
        value = _clamp01(values[name])
        points = round(value * weight, 2)
        factors[name] = {"weight": weight, "value": round(value, 3), "points": points}
        total += points

    total_int = int(round(total))
    return IntelligenceScore(
        version=SCORING_VERSION,
        total=total_int,
        classification=classify_opportunity(total_int),
        factors=factors,
    )
