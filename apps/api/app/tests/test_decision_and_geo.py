"""Unit tests for the decision engine, geography engine, and claim-safety engine."""

from __future__ import annotations

from app.claims.engine import check_claim_safety, translate_competitor_weakness
from app.core.enums import ClaimRisk, DecisionAction, OpportunityClassification, RiskLevel
from app.geography.engine import (
    CoverageRule,
    haversine_miles,
    is_within_radius,
    market_in_coverage,
    resolve_geo,
)
from app.scoring.decision import DecisionInput, decide


def _decision(**kw) -> DecisionInput:
    base = dict(
        relevance_score=80,
        opportunity_score=80,
        confidence_score=80,
        classification=OpportunityClassification.VALIDATED,
        risk_level=RiskLevel.LOW,
        inside_scout_area=True,
        is_noise=False,
        audience_fit=True,
    )
    base.update(kw)
    return DecisionInput(**base)


class TestDecisionEngine:
    def test_blocked_risk_blocks(self):
        action, _ = decide(_decision(risk_level=RiskLevel.BLOCKED))
        assert action == DecisionAction.BLOCK

    def test_noise_stays_silent(self):
        action, _ = decide(_decision(is_noise=True))
        assert action == DecisionAction.STAY_SILENT

    def test_outside_scout_area_only_monitors(self):
        action, _ = decide(_decision(inside_scout_area=False))
        assert action == DecisionAction.MONITOR

    def test_below_relevance_floor_never_recommends_action(self):
        # This is the product's hard rule: relevance < 40 => no action.
        action, rationale = decide(_decision(relevance_score=39))
        assert action == DecisionAction.STAY_SILENT
        assert "floor" in rationale.lower()

    def test_unclear_audience_fit_monitors(self):
        action, _ = decide(_decision(audience_fit=False))
        assert action == DecisionAction.MONITOR

    def test_high_priority_high_confidence_acts_now(self):
        action, _ = decide(
            _decision(
                classification=OpportunityClassification.HIGH_PRIORITY,
                confidence_score=85,
            )
        )
        assert action == DecisionAction.ACT_NOW

    def test_validated_acts_soon(self):
        action, _ = decide(_decision(classification=OpportunityClassification.VALIDATED))
        assert action == DecisionAction.ACT_SOON


class TestGeography:
    def test_haversine_known_distance(self):
        # Dallas -> London is roughly 4,900 miles.
        miles = haversine_miles(32.7767, -96.7970, 51.5074, -0.1278)
        assert 4700 < miles < 5100

    def test_radius_match_inside_and_outside(self):
        rule = CoverageRule(
            coverage_type="radius",
            center_latitude=32.7767,
            center_longitude=-96.7970,
            radius_miles=25,
        )
        # ~10 miles away -> inside.
        assert is_within_radius(rule, 32.90, -96.90) is True
        # London -> far outside.
        assert is_within_radius(rule, 51.5074, -0.1278) is False

    def test_radius_uncheckable_returns_none(self):
        rule = CoverageRule(coverage_type="radius", radius_miles=25)
        assert is_within_radius(rule, 32.9, -96.9) is None

    def test_excluded_market_wins(self):
        rule = CoverageRule(
            coverage_type="city",
            included_markets=("dallas",),
            excluded_markets=("plano",),
        )
        assert market_in_coverage(rule, "Plano, TX") is False
        assert market_in_coverage(rule, "Dallas, TX") is True

    def test_online_global_covers_everything(self):
        rule = CoverageRule(coverage_type="online", online_global=True)
        assert market_in_coverage(rule, "anywhere") is True

    def test_resolve_geo_picks_strongest_evidence(self):
        rule = CoverageRule(coverage_type="city", included_markets=("dallas",))
        res = resolve_geo(
            rule,
            signals=[
                ("platform_geotag", "Dallas, TX"),
                ("hashtag", "London"),
            ],
        )
        assert res.resolved_market == "Dallas, TX"
        assert res.confidence > 0.5
        assert res.inside_scout_area is True

    def test_resolve_geo_no_evidence(self):
        rule = CoverageRule()
        res = resolve_geo(rule, signals=[])
        assert res.resolved_market is None
        assert res.confidence == 0.0


class TestClaimSafety:
    def test_health_claim_flagged_high(self):
        result = check_claim_safety("This product cures anxiety instantly")
        assert result.risk_level == ClaimRisk.HIGH
        assert any(f.category == "health_claim" for f in result.findings)

    def test_financial_claim_blocked(self):
        result = check_claim_safety("Guaranteed returns — double your money!")
        assert result.risk_level == ClaimRisk.BLOCKED
        assert result.is_blocked is True

    def test_explicit_blocked_claim_list(self):
        result = check_claim_safety(
            "Our brew is organic certified", blocked_claims=["organic certified"]
        )
        assert result.is_blocked is True
        assert "organic certified" in result.blocked_terms

    def test_strict_industry_escalates_medium_to_high(self):
        # "best" is normally MEDIUM; in a strict industry it escalates.
        result = check_claim_safety("The best supplement around", industry="supplements")
        assert result.risk_level == ClaimRisk.HIGH

    def test_clean_text_is_low_risk(self):
        result = check_claim_safety("Locally roasted coffee, delivered to your door.")
        assert result.risk_level == ClaimRisk.LOW
        assert result.findings == []

    def test_competitor_weakness_translation_is_safe(self):
        msg = translate_competitor_weakness("speed_complaint")
        assert "faster" in msg.lower()
        # Never an attack or unsupported superiority claim.
        assert "better than" not in msg.lower()
