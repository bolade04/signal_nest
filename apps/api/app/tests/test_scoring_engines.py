"""Unit tests for the pure scoring engines (no FastAPI, no DB).

These lock in the correctness rules the product's explainability claims depend on:
the relevance action floor, opportunity/confidence weighting + banding, the noise
gate, and cross-source validation.
"""

from __future__ import annotations

from app.core.enums import ConfidenceLevel, OpportunityClassification
from app.scoring.noise import PRE_ANALYSIS_FULL_THRESHOLD, evaluate_noise
from app.scoring.opportunity import (
    classify_opportunity,
    confidence_level,
    score_confidence,
    score_opportunity,
)
from app.scoring.relevance import RELEVANCE_ACTION_FLOOR, score_relevance
from app.scoring.types import BusinessContext, SignalInput
from app.scoring.validation import score_validation


def _ctx(**kw) -> BusinessContext:
    base = dict(
        keywords=["coffee", "delivery"],
        pain_points=["slow delivery"],
        audiences=["urban households"],
        competitors=["rival brew"],
    )
    base.update(kw)
    return BusinessContext(**base)


class TestRelevance:
    def test_strong_topical_match_scores_above_floor(self):
        sig = SignalInput(
            content="Coffee delivery here is so slow, urban households are frustrated",
            source_type="rss_news",
            signal_type="complaint",
        )
        score, expl = score_relevance(sig, _ctx())
        assert score >= RELEVANCE_ACTION_FLOOR
        assert expl["below_action_floor"] is False
        assert "coffee" in expl["keyword_hits"] or "delivery" in expl["keyword_hits"]

    def test_offtopic_signal_falls_below_action_floor(self):
        sig = SignalInput(content="The weather is nice today", source_type="manual")
        score, expl = score_relevance(sig, _ctx())
        assert score < RELEVANCE_ACTION_FLOOR
        assert expl["below_action_floor"] is True

    def test_exclusion_term_hard_kills_relevance(self):
        sig = SignalInput(
            content="Great coffee delivery jobs hiring now",
            source_type="manual",
        )
        score, expl = score_relevance(sig, _ctx(exclusion_terms=["jobs"]))
        assert score == 0
        assert expl["reason"] == "excluded_terms"


class TestOpportunityScoring:
    def test_weighted_total_never_exceeds_100(self):
        sig = SignalInput(
            content="x" * 80,
            source_type="rss_news",
            signal_type="buying_intent",
            engagement=100,
            duplicate_count=10,
            distinct_source_types=3,
            has_buying_intent=True,
            search_trend_up=True,
            news_coverage=True,
            age_days=0,
        )
        breakdown = score_opportunity(sig, relevance_score=100, validation_score=100)
        assert 0 <= breakdown.total <= 100
        assert breakdown.total >= 80  # strong signal should score high

    def test_weak_signal_scores_low(self):
        sig = SignalInput(content="meh", source_type="manual", age_days=80)
        breakdown = score_opportunity(sig, relevance_score=10, validation_score=0)
        assert breakdown.total < 40

    def test_classification_bands(self):
        assert classify_opportunity(10) == OpportunityClassification.NOISE
        assert classify_opportunity(30) == OpportunityClassification.DISCUSSION_ONLY
        assert classify_opportunity(50) == OpportunityClassification.WEAK
        assert classify_opportunity(60) == OpportunityClassification.EARLY
        assert classify_opportunity(80) == OpportunityClassification.VALIDATED
        assert classify_opportunity(90) == OpportunityClassification.HIGH_PRIORITY

    def test_confidence_level_bands(self):
        assert confidence_level(20) == ConfidenceLevel.LOW
        assert confidence_level(55) == ConfidenceLevel.MEDIUM
        assert confidence_level(85) == ConfidenceLevel.HIGH

    def test_confidence_rewards_evidence_and_consistency(self):
        sig = SignalInput(
            content="x" * 80,
            source_type="rss_news",
            signal_type="complaint",
            distinct_source_types=3,
            age_days=0,
        )
        high = score_confidence(
            sig, evidence_count=5, opportunity_score=80, relevance_score=78, validation_score=82
        )
        thin = SignalInput(
            content="short", source_type="manual", distinct_source_types=1, age_days=100
        )
        low = score_confidence(
            thin, evidence_count=1, opportunity_score=80, relevance_score=10, validation_score=90
        )
        assert high.total > low.total


class TestNoiseGate:
    def test_spam_is_hard_noise(self):
        sig = SignalInput(content="click here for free money now!!!", source_type="manual")
        score, is_noise, reasons = evaluate_noise(sig)
        assert is_noise is True
        assert "spam" in reasons

    def test_bot_author_is_noise(self):
        sig = SignalInput(
            content="This is a perfectly normal length complaint about delivery times.",
            source_type="manual",
            author="bot_marketer",
        )
        _, is_noise, reasons = evaluate_noise(sig)
        assert is_noise is True
        assert "bot_like" in reasons

    def test_quality_signal_passes_full_analysis_gate(self):
        sig = SignalInput(
            content="Customers keep complaining that delivery is far too slow in the city center.",
            source_type="rss_news",
            signal_type="complaint",
            engagement=60,
            distinct_source_types=2,
            has_buying_intent=True,
            age_days=1,
        )
        score, is_noise, reasons = evaluate_noise(sig)
        assert is_noise is False
        assert score >= PRE_ANALYSIS_FULL_THRESHOLD
        assert reasons == []

    def test_low_context_penalized(self):
        sig = SignalInput(content="slow", source_type="manual")
        _, _, reasons = evaluate_noise(sig)
        assert "low_context" in reasons


class TestValidation:
    def test_cross_source_agreement_dominates(self):
        multi = SignalInput(content="demand", source_type="rss_news", distinct_source_types=3)
        single = SignalInput(content="demand", source_type="rss_news", distinct_source_types=1)
        multi_score, evidence = score_validation(multi)
        single_score, _ = score_validation(single)
        assert multi_score > single_score
        assert any(e["source_type"] == "cross_source" for e in evidence)

    def test_validation_capped_at_100(self):
        sig = SignalInput(
            content="demand",
            source_type="rss_news",
            distinct_source_types=3,
            duplicate_count=9,
            engagement=90,
            has_active_ads=True,
            news_coverage=True,
            search_trend_up=True,
            has_buying_intent=True,
        )
        score, _ = score_validation(sig)
        assert score == 100
