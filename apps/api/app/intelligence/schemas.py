"""Public, typed response schemas for read-only opportunity intelligence (Batch 4B).

These Pydantic models are the *only* customer-facing shape of a persisted
:class:`~app.intelligence.records.SignalIntelligenceRecord`. Two disciplines carry
over from the persistence layer and are re-asserted here at the API boundary:

* **Facts stay separate from inference.** :class:`IntelligenceFacts` carries only
  observed fields; every interpreted value lives under :class:`IntelligenceInference`
  as an evidence-backed :class:`InferredAttribute`. The two are never collapsed into
  one untyped blob.
* **Nothing internal leaks.** The record ``id``, ``fingerprint``, ``cluster_key``,
  ``normalized_signal_id``, ``organization_id``, ``updated_at``, ``author``,
  ``exclusion_hits`` and ``rejection_reason`` have **no field** here, so they can
  never be serialized. Raw DB JSON is never passed through — the read-service maps
  column-by-column into these typed, bounded models.

All bounds (string/list lengths, score/confidence ranges) mirror the Batch 4A
persistence bounds and are enforced again by the mapper (``read_service``) before a
model is constructed, so an oversized or malformed persisted row fails safe rather
than producing an unbounded response.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class InferredAttribute(BaseModel):
    """A single inferred value with the deterministic method behind it.

    Evidence spans are surfaced once, at the payload level (:class:`IntelligencePayload.evidence`),
    not repeated inside every attribute.
    """

    value: str
    confidence: float  # 0..1, rounded to 3 dp by the mapper
    method: str  # deterministic matcher label (explainability)


class IntelligenceEvidenceItem(BaseModel):
    """A sanitized excerpt slice supporting an inference (for later highlighting)."""

    quote: str  # ≤ 400 chars (bounded by the mapper)
    method: str
    start: int
    end: int


class IntelligenceFacts(BaseModel):
    """Observed-only fields. Nothing here is inferred. ``author`` is excluded (PII)."""

    source_type: str
    market: str | None = None
    language: str
    published_days_ago: float
    char_count: int
    word_count: int
    excerpt: str  # ≤ 2000 chars (sanitized in 4A, re-bounded by the mapper)
    distinct_source_types: int
    duplicate_count: int
    engagement: int


class IntelligenceInference(BaseModel):
    """Interpretation only. Each attribute is evidence-backed, never treated as fact."""

    signal_type: InferredAttribute | None = None
    pain_point_dna: InferredAttribute | None = None
    sentiment: InferredAttribute | None = None
    has_buying_intent: bool = False
    has_competitor_dissatisfaction: bool = False


class IntelligenceRelevance(BaseModel):
    """Business-relevance breakdown. ``exclusion_hits`` is excluded (operator-only)."""

    score: int  # 0..100
    below_action_floor: bool
    keyword_hits: list[str] = []  # each list ≤ 64 items
    pain_point_hits: list[str] = []
    audience_hits: list[str] = []
    competitor_hits: list[str] = []


class ScoreFactor(BaseModel):
    weight: float
    value: float
    points: float


class IntelligenceScoreBreakdown(BaseModel):
    total: int  # 0..100 (mirrors ``score_total``)
    classification: str
    version: str  # scoring version
    factors: dict[str, ScoreFactor] = {}


class IntelligenceProvenance(BaseModel):
    """Public-safe provenance. The raw ``fingerprint`` is intentionally dropped."""

    enricher: str
    analysis_version: str
    scoring_version: str


class IntelligenceVersionInfo(BaseModel):
    analysis_version: str
    scoring_version: str


class IntelligencePayload(BaseModel):
    """The typed, bounded public view of one persisted intelligence record."""

    # Customer-safe opaque identifier of the exact persisted record this payload was
    # mapped from (3C-C.1). It is the stable handle a client passes to the feedback
    # POST so a judgement is bound to this precise immutable record. It is *only* the
    # record's primary key — never the fingerprint, tenant scope columns or any other
    # internal metadata.
    intelligence_record_id: str
    classification: str
    decision: str | None = None
    is_simulated: bool
    rationale: str | None = None
    created_at: datetime  # ISO-8601 UTC
    facts: IntelligenceFacts
    inference: IntelligenceInference
    relevance: IntelligenceRelevance
    score: IntelligenceScoreBreakdown
    evidence: list[IntelligenceEvidenceItem] = []
    provenance: IntelligenceProvenance
    version: IntelligenceVersionInfo


class OpportunityIntelligenceResponse(BaseModel):
    """Top-level response. ``intelligence`` is ``null`` when no eligible record exists."""

    opportunity_id: str
    intelligence: IntelligencePayload | None = None
