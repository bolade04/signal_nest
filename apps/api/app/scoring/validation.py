"""Validation engine.

Answers: is this isolated, or is there real market evidence behind it?
Strongest validation comes from cross-source agreement.
"""

from __future__ import annotations

from app.scoring.types import SignalInput


def score_validation(signal: SignalInput) -> tuple[int, list[dict]]:
    """Return (0..100 validation strength, evidence list)."""
    evidence: list[dict] = []
    score = 0

    if signal.distinct_source_types >= 3:
        score += 35
        evidence.append(
            {"source_type": "cross_source", "detail": "3+ source types agree", "weight": 35}
        )
    elif signal.distinct_source_types == 2:
        score += 22
        evidence.append(
            {"source_type": "cross_source", "detail": "2 source types agree", "weight": 22}
        )

    if signal.duplicate_count >= 5:
        score += 18
        evidence.append(
            {"source_type": "volume", "detail": f"{signal.duplicate_count} mentions", "weight": 18}
        )
    elif signal.duplicate_count >= 2:
        score += 10
        evidence.append(
            {"source_type": "volume", "detail": f"{signal.duplicate_count} mentions", "weight": 10}
        )

    if signal.engagement >= 50:
        score += 12
        evidence.append({"source_type": "engagement", "detail": "high engagement", "weight": 12})

    if signal.has_active_ads:
        score += 12
        evidence.append(
            {"source_type": "active_advertising", "detail": "competitors advertising", "weight": 12}
        )
    if signal.news_coverage:
        score += 10
        evidence.append({"source_type": "news", "detail": "news coverage present", "weight": 10})
    if signal.search_trend_up:
        score += 8
        evidence.append(
            {"source_type": "search_trends", "detail": "rising search demand", "weight": 8}
        )
    if signal.has_buying_intent:
        score += 10
        evidence.append(
            {"source_type": "buying_intent", "detail": "explicit buying intent", "weight": 10}
        )

    return min(100, score), evidence
