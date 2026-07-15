"""Intelligence orchestrator.

Runs the full deterministic chain for a single signal:

``enrich → facts + inference → business relevance → noise gate → versioned score →
structured accept/reject → decision → clustered candidate``

It reuses the existing noise, relevance, validation and decision engines so there
is one definition of each rule. No network, no model call, no randomness — the
same ``AnalysisInput`` + ``BusinessContext`` always yields the same
``OpportunityCandidate``.
"""

from __future__ import annotations

from app.core.enums import OpportunityClassification, RiskLevel
from app.intelligence.clustering import cluster_key, content_fingerprint
from app.intelligence.enrichment import Enricher, get_enricher
from app.intelligence.models import AnalysisInput, OpportunityCandidate
from app.intelligence.rejection import evaluate_rejection
from app.intelligence.relevance import assess_relevance
from app.intelligence.scoring import score_candidate
from app.scoring.decision import DecisionInput, decide
from app.scoring.noise import evaluate_noise
from app.scoring.types import BusinessContext, SignalInput


def analyze_signal(
    signal: AnalysisInput,
    ctx: BusinessContext,
    *,
    enricher: Enricher | None = None,
    seen_fingerprints: set[str] | None = None,
) -> OpportunityCandidate:
    """Analyze one signal into an explainable, evidence-backed candidate.

    ``seen_fingerprints`` (optional, mutated) enables deterministic in-run
    duplicate detection: the first occurrence is kept, later identical content is
    rejected as ``DUPLICATE``.
    """
    enricher = enricher or get_enricher()
    facts, intelligence = enricher.enrich(signal)
    relevance = assess_relevance(facts, intelligence, ctx)

    # Reuse the shared noise gate on the sanitized excerpt.
    noise_input = SignalInput(
        content=facts.excerpt,
        source_type=facts.source_type,
        signal_type=intelligence.signal_type.value if intelligence.signal_type else None,
        engagement=facts.engagement,
        age_days=facts.published_days_ago,
        author=facts.author,
        duplicate_count=facts.duplicate_count,
        distinct_source_types=facts.distinct_source_types,
        has_buying_intent=intelligence.has_buying_intent,
    )
    _, is_noise, _ = evaluate_noise(noise_input)

    # Deterministic in-run duplicate detection.
    is_duplicate = False
    fingerprint = content_fingerprint(facts.excerpt)
    if seen_fingerprints is not None:
        is_duplicate = fingerprint in seen_fingerprints

    score = score_candidate(
        facts, intelligence, relevance, inside_scout_area=signal.inside_scout_area
    )
    key = cluster_key(intelligence)
    evidence_count = len(intelligence.all_evidence)

    rejection = evaluate_rejection(
        facts,
        intelligence,
        relevance,
        score,
        is_noise=is_noise,
        inside_scout_area=signal.inside_scout_area,
        claim_risk_blocked=signal.claim_risk_blocked,
        is_duplicate=is_duplicate,
    )

    if rejection is not None:
        reason, rationale = rejection
        return OpportunityCandidate(
            facts=facts,
            intelligence=intelligence,
            relevance=relevance,
            score=score,
            accepted=False,
            rationale=rationale,
            rejection=reason,
            cluster_key=key,
            is_simulated=signal.is_simulated,
            evidence_count=evidence_count,
        )

    # Accepted: record the fingerprint so later duplicates are caught, then decide.
    if seen_fingerprints is not None:
        seen_fingerprints.add(fingerprint)

    decision, rationale = decide(
        DecisionInput(
            relevance_score=relevance.score,
            opportunity_score=score.total,
            confidence_score=score.total,
            classification=score.classification,
            risk_level=RiskLevel.BLOCKED if signal.claim_risk_blocked else RiskLevel.LOW,
            inside_scout_area=signal.inside_scout_area,
            is_noise=is_noise,
            audience_fit=bool(relevance.audience_hits) or bool(ctx.audiences),
        )
    )

    return OpportunityCandidate(
        facts=facts,
        intelligence=intelligence,
        relevance=relevance,
        score=score,
        accepted=score.classification != OpportunityClassification.NOISE,
        rationale=rationale,
        decision=decision,
        cluster_key=key,
        is_simulated=signal.is_simulated,
        evidence_count=evidence_count,
    )
