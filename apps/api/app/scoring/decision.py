"""Decision engine.

Core rule: if SignalNest cannot explain why the user should care, it should not alert.
Also enforces: relevance < 40 => never recommend action (monitor/silent at most).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import DecisionAction, OpportunityClassification, RiskLevel
from app.scoring.relevance import RELEVANCE_ACTION_FLOOR


@dataclass
class DecisionInput:
    relevance_score: int
    opportunity_score: int
    confidence_score: int
    classification: OpportunityClassification
    risk_level: RiskLevel
    inside_scout_area: bool
    is_noise: bool
    audience_fit: bool


def decide(d: DecisionInput) -> tuple[DecisionAction, str]:
    """Return (action, human-readable rationale)."""
    if d.risk_level == RiskLevel.BLOCKED:
        return DecisionAction.BLOCK, "Blocked by claim/compliance safety controls."
    if d.is_noise or d.classification == OpportunityClassification.NOISE:
        return DecisionAction.STAY_SILENT, "Filtered as noise; nothing actionable."
    if not d.inside_scout_area:
        return DecisionAction.MONITOR, "Outside the configured scout reach; monitoring only."

    # Hard relevance floor: never recommend action below the floor.
    if d.relevance_score < RELEVANCE_ACTION_FLOOR:
        return DecisionAction.STAY_SILENT, (
            f"Relevance {d.relevance_score} is below the action floor "
            f"({RELEVANCE_ACTION_FLOOR}); no action recommended."
        )
    if not d.audience_fit:
        return DecisionAction.MONITOR, "Audience fit is unclear; monitoring until clearer."

    high_conf = d.confidence_score >= 70
    if d.classification == OpportunityClassification.HIGH_PRIORITY and high_conf:
        return DecisionAction.ACT_NOW, "High-priority, well-validated, high confidence."
    if d.classification in (
        OpportunityClassification.VALIDATED,
        OpportunityClassification.HIGH_PRIORITY,
    ):
        return DecisionAction.ACT_SOON, "Validated opportunity; act soon."
    if d.classification in (
        OpportunityClassification.EARLY,
        OpportunityClassification.EMERGING,
        OpportunityClassification.WEAK,
    ):
        return DecisionAction.MONITOR, "Relevant but not yet strongly validated; monitor."
    if d.classification == OpportunityClassification.DISCUSSION_ONLY:
        return DecisionAction.MONITOR, "Discussion only; watchlist."
    return DecisionAction.ARCHIVE, "Dead or spent opportunity; archive."


ACTIONABLE = {DecisionAction.ACT_NOW, DecisionAction.ACT_SOON}
