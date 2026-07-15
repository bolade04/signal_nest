"""Signal intelligence core (Phase 3B Batch 3).

A fully **deterministic, offline** layer that turns a normalized, market-scoped
signal into an explainable, evidence-backed opportunity candidate. It is strictly
additive to the Phase 2 pipeline: it reuses the existing relevance/validation/
decision engines rather than replacing them, performs **no** network egress, makes
**no** model call in normal operation, and never trusts or executes untrusted
source text.

The chain is: ``enrich → extract facts + intelligence → business relevance →
versioned scoring → structured accept/reject → deterministic clustering``. The
same input always produces the same output (see ``evaluation`` for the labeled
dataset that pins this down).

Nothing here changes the API contract or the database schema. See
``docs/phase-3b/signal-intelligence-design.md`` for the full design and
``docs/security/signal-intelligence-threat-model.md`` for the safety analysis.
"""

from __future__ import annotations

from app.intelligence.analyze import analyze_signal
from app.intelligence.clustering import cluster_candidates, cluster_key
from app.intelligence.enrichment import (
    DeterministicEnricher,
    Enricher,
    ModelEnricher,
    get_enricher,
)
from app.intelligence.extraction import extract_facts, extract_intelligence, sanitize_text
from app.intelligence.models import (
    BusinessRelevance,
    EvidenceSpan,
    ExtractedIntelligence,
    IntelligenceScore,
    OpportunityCandidate,
    SignalFacts,
)
from app.intelligence.rejection import evaluate_rejection
from app.intelligence.scoring import SCORING_VERSION, score_candidate

__all__ = [
    "analyze_signal",
    "cluster_key",
    "cluster_candidates",
    "DeterministicEnricher",
    "Enricher",
    "ModelEnricher",
    "get_enricher",
    "extract_facts",
    "extract_intelligence",
    "sanitize_text",
    "BusinessRelevance",
    "EvidenceSpan",
    "ExtractedIntelligence",
    "IntelligenceScore",
    "OpportunityCandidate",
    "SignalFacts",
    "evaluate_rejection",
    "SCORING_VERSION",
    "score_candidate",
]
