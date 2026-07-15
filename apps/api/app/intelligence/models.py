"""Typed domain models for the signal-intelligence core.

The central discipline here is a hard separation between **facts** and
**inference**:

* :class:`SignalFacts` carries only what is *literally* present in the signal
  (source type, market, author, counts, the sanitized excerpt). Nothing here is
  guessed.
* :class:`ExtractedIntelligence` carries *inference* (signal type, pain-point DNA,
  sentiment, commercial flags). Every inferred attribute must point back at one or
  more :class:`EvidenceSpan` in the sanitized excerpt, name the deterministic
  ``method`` that produced it, and carry a calibrated ``confidence``.

Because inference can never be silently promoted to fact, downstream consumers
(scoring, explanations, the UI) can always show *why* a claim was made and *what
text* supports it. All models are plain dataclasses — no framework dependencies,
no I/O — so they are cheap to construct in tests and safe to serialize.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import DecisionAction, OpportunityClassification, RejectionReason


@dataclass(frozen=True)
class EvidenceSpan:
    """A substring of the *sanitized* excerpt that supports an inference.

    ``start``/``end`` are indices into the sanitized excerpt (never the raw
    untrusted text). ``quote`` is the exact matched slice, kept for display and
    audit. ``method`` names the deterministic matcher (e.g. ``"lexicon:complaint"``)
    so an inference is reproducible and reviewable.
    """

    start: int
    end: int
    quote: str
    method: str

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "quote": self.quote, "method": self.method}


@dataclass(frozen=True)
class SignalFacts:
    """Only what is literally observable in the signal. No inference."""

    source_type: str
    market: str | None
    author: str | None
    language: str
    published_days_ago: float
    char_count: int
    word_count: int
    excerpt: str  # sanitized, safe-to-display
    distinct_source_types: int = 1
    duplicate_count: int = 1
    engagement: int = 0

    def as_dict(self) -> dict:
        return {
            "source_type": self.source_type,
            "market": self.market,
            "author": self.author,
            "language": self.language,
            "published_days_ago": self.published_days_ago,
            "char_count": self.char_count,
            "word_count": self.word_count,
            "excerpt": self.excerpt,
            "distinct_source_types": self.distinct_source_types,
            "duplicate_count": self.duplicate_count,
            "engagement": self.engagement,
        }


@dataclass(frozen=True)
class InferredAttribute:
    """A single inferred value with the evidence and method behind it."""

    value: str
    confidence: float  # 0..1
    method: str
    evidence: tuple[EvidenceSpan, ...] = ()

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 3),
            "method": self.method,
            "evidence": [e.as_dict() for e in self.evidence],
        }


@dataclass(frozen=True)
class ExtractedIntelligence:
    """Inference layer. Each field is evidence-backed and never treated as fact."""

    signal_type: InferredAttribute | None = None
    pain_point_dna: InferredAttribute | None = None
    sentiment: InferredAttribute | None = None
    has_buying_intent: bool = False
    has_competitor_dissatisfaction: bool = False
    intent_evidence: tuple[EvidenceSpan, ...] = ()

    @property
    def all_evidence(self) -> tuple[EvidenceSpan, ...]:
        spans: list[EvidenceSpan] = []
        for attr in (self.signal_type, self.pain_point_dna, self.sentiment):
            if attr is not None:
                spans.extend(attr.evidence)
        spans.extend(self.intent_evidence)
        return tuple(spans)

    def as_dict(self) -> dict:
        return {
            "signal_type": self.signal_type.as_dict() if self.signal_type else None,
            "pain_point_dna": self.pain_point_dna.as_dict() if self.pain_point_dna else None,
            "sentiment": self.sentiment.as_dict() if self.sentiment else None,
            "has_buying_intent": self.has_buying_intent,
            "has_competitor_dissatisfaction": self.has_competitor_dissatisfaction,
            "intent_evidence": [e.as_dict() for e in self.intent_evidence],
        }


@dataclass(frozen=True)
class BusinessRelevance:
    """Does this signal matter to *this* business? Reuses the relevance engine."""

    score: int  # 0..100
    below_action_floor: bool
    keyword_hits: tuple[str, ...] = ()
    pain_point_hits: tuple[str, ...] = ()
    audience_hits: tuple[str, ...] = ()
    competitor_hits: tuple[str, ...] = ()
    exclusion_hits: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "below_action_floor": self.below_action_floor,
            "keyword_hits": list(self.keyword_hits),
            "pain_point_hits": list(self.pain_point_hits),
            "audience_hits": list(self.audience_hits),
            "competitor_hits": list(self.competitor_hits),
            "exclusion_hits": list(self.exclusion_hits),
        }


@dataclass(frozen=True)
class IntelligenceScore:
    """Versioned, explainable composite score (0..100)."""

    version: str
    total: int
    classification: OpportunityClassification
    factors: dict[str, dict[str, float]]  # factor -> {weight, value, points}

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "total": self.total,
            "classification": self.classification.value,
            "factors": self.factors,
        }


@dataclass
class OpportunityCandidate:
    """The batch's output: facts + inference + relevance + score + decision.

    Exactly one of (``decision``, ``rejection``) is set. ``rationale`` explains the
    outcome in one human-readable line either way. ``cluster_key`` groups related
    candidates deterministically.
    """

    facts: SignalFacts
    intelligence: ExtractedIntelligence
    relevance: BusinessRelevance
    score: IntelligenceScore
    accepted: bool
    rationale: str
    decision: DecisionAction | None = None
    rejection: RejectionReason | None = None
    cluster_key: str = "general"
    is_simulated: bool = True
    evidence_count: int = 0

    def as_dict(self) -> dict:
        return {
            "facts": self.facts.as_dict(),
            "intelligence": self.intelligence.as_dict(),
            "relevance": self.relevance.as_dict(),
            "score": self.score.as_dict(),
            "accepted": self.accepted,
            "rationale": self.rationale,
            "decision": self.decision.value if self.decision else None,
            "rejection": self.rejection.value if self.rejection else None,
            "cluster_key": self.cluster_key,
            "is_simulated": self.is_simulated,
            "evidence_count": self.evidence_count,
        }


@dataclass(frozen=True)
class AnalysisInput:
    """Normalized, market-scoped signal fed to the intelligence core.

    Mirrors the pipeline's read surface (``ConnectorSignal`` / ``NormalizedSignal``)
    without importing SQLAlchemy, so the core stays pure and testable.
    """

    content: str
    source_type: str
    market: str | None = None
    author: str | None = None
    language: str = "en"
    published_days_ago: float = 0.0
    engagement: int = 0
    distinct_source_types: int = 1
    duplicate_count: int = 1
    has_active_ads: bool = False
    news_coverage: bool = False
    search_trend_up: bool = False
    inside_scout_area: bool = True
    claim_risk_blocked: bool = False
    is_simulated: bool = True
    extra_keywords: tuple[str, ...] = field(default_factory=tuple)
