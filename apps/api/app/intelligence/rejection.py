"""Structured rejection / suppression rules.

A signal that is not surfaced must be *as explainable* as one that is. These rules
map a candidate's facts/relevance/score onto exactly one :class:`RejectionReason`
plus a human rationale. Rules are ordered from most-decisive/cheapest to weakest
and short-circuit, so the reported reason is the first, strongest cause of
suppression rather than an arbitrary one.

Returning ``None`` means "no suppression rule fired" — the candidate is accepted
and handed to the decision engine.
"""

from __future__ import annotations

from app.core.enums import RejectionReason
from app.intelligence.models import (
    BusinessRelevance,
    ExtractedIntelligence,
    IntelligenceScore,
    SignalFacts,
)

#: Composite scores below this are surfaced as weak rather than actionable.
WEAK_SIGNAL_FLOOR = 25
#: Minimum evidence spans required to treat inference as usable.
MIN_EVIDENCE_SPANS = 1


def evaluate_rejection(
    facts: SignalFacts,
    intelligence: ExtractedIntelligence,
    relevance: BusinessRelevance,
    score: IntelligenceScore,
    *,
    is_noise: bool,
    inside_scout_area: bool,
    claim_risk_blocked: bool,
    is_duplicate: bool,
) -> tuple[RejectionReason, str] | None:
    """Return ``(reason, rationale)`` if the signal should be suppressed, else ``None``."""
    if claim_risk_blocked:
        return (
            RejectionReason.POLICY_BLOCKED,
            "Suppressed by claim/compliance safety controls.",
        )
    if is_noise:
        return (RejectionReason.NOISE, "Filtered as noise; nothing actionable.")
    if relevance.exclusion_hits:
        return (
            RejectionReason.OUT_OF_CONTEXT,
            f"Matched business exclusion rules: {', '.join(relevance.exclusion_hits)}.",
        )
    if relevance.below_action_floor:
        return (
            RejectionReason.OUT_OF_CONTEXT,
            f"Relevance {relevance.score} is below the action floor; not this business's topic.",
        )
    if not inside_scout_area:
        return (
            RejectionReason.OUT_OF_MARKET,
            "Outside the configured scout reach; not surfaced for this market.",
        )
    if is_duplicate:
        return (RejectionReason.DUPLICATE, "Duplicate of an already-seen signal in this run.")
    if len(intelligence.all_evidence) < MIN_EVIDENCE_SPANS:
        return (
            RejectionReason.INSUFFICIENT_EVIDENCE,
            "No supporting evidence spans extracted; cannot justify an opportunity.",
        )
    if score.total < WEAK_SIGNAL_FLOOR:
        return (
            RejectionReason.WEAK_SIGNAL,
            f"Composite score {score.total} is below the weak-signal floor "
            f"({WEAK_SIGNAL_FLOOR}).",
        )
    return None
