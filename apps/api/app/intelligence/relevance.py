"""Business-relevance adapter.

Thin, deterministic bridge from the intelligence layer onto the existing
:func:`app.scoring.relevance.score_relevance` engine, so the product keeps a
*single* relevance definition (and its action-floor of 40) rather than growing a
second, drifting one. This module only reshapes inputs/outputs into the
intelligence domain types; it does not re-implement scoring.
"""

from __future__ import annotations

from app.intelligence.models import BusinessRelevance, ExtractedIntelligence, SignalFacts
from app.scoring.relevance import RELEVANCE_ACTION_FLOOR, score_relevance
from app.scoring.types import BusinessContext, SignalInput


def assess_relevance(
    facts: SignalFacts, intelligence: ExtractedIntelligence, ctx: BusinessContext
) -> BusinessRelevance:
    """Score business relevance for a signal using the shared relevance engine."""
    signal_type = intelligence.signal_type.value if intelligence.signal_type else None
    signal_input = SignalInput(
        content=facts.excerpt,
        source_type=facts.source_type,
        signal_type=signal_type,
        engagement=facts.engagement,
        age_days=facts.published_days_ago,
        author=facts.author,
        language=facts.language,
        duplicate_count=facts.duplicate_count,
        distinct_source_types=facts.distinct_source_types,
        has_buying_intent=intelligence.has_buying_intent,
    )
    score, expl = score_relevance(signal_input, ctx)

    if expl.get("reason") == "excluded_terms":
        return BusinessRelevance(
            score=0,
            below_action_floor=True,
            exclusion_hits=tuple(expl.get("excluded", ())),
        )

    return BusinessRelevance(
        score=score,
        below_action_floor=score < RELEVANCE_ACTION_FLOOR,
        keyword_hits=tuple(expl.get("keyword_hits", ())),
        pain_point_hits=tuple(expl.get("pain_point_hits", ())),
        audience_hits=tuple(expl.get("audience_hits", ())),
        competitor_hits=tuple(expl.get("competitor_hits", ())),
    )
