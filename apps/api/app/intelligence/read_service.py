"""Read-only mapping service for opportunity intelligence (Batch 4B).

Turns a persisted, workspace-scoped :class:`~app.intelligence.records.SignalIntelligenceRecord`
into the typed, bounded public :class:`~app.intelligence.schemas.IntelligencePayload`.
The route has already authorized the opportunity (``_get_scoped``); this service only
*reads*, maps and bounds — it never mutates, never calls a model, connector or the
enrichment pipeline, and never fabricates a payload from legacy
``ingest_metadata["intelligence"]``.

Three safety disciplines:

* **Column-by-column typed mapping** — every public field is read from a named column
  and re-bounded here; raw DB JSON is never returned verbatim, so unknown/extra keys
  are dropped.
* **Fail-safe on malformed rows** — if a persisted payload cannot be mapped, the
  service logs ``intelligence_read_malformed`` (versions + outcome only, never the
  payload) and returns ``None`` (→ ``intelligence: null``) rather than a ``500`` or a
  partial, again-untrusted object. One bad row never hard-fails the request.
* **Safe observability** — structured events carry only ``workspace_id``,
  ``opportunity_id``, ``outcome``, ``duration_ms`` and record versions; never evidence
  text, quotes, raw source, secrets, PII or the fingerprint.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.logging import get_logger, log_event
from app.intelligence.persistence import get_latest_for_opportunity
from app.intelligence.records import SignalIntelligenceRecord
from app.intelligence.schemas import (
    InferredAttribute,
    IntelligenceEvidenceItem,
    IntelligenceFacts,
    IntelligenceInference,
    IntelligencePayload,
    IntelligenceProvenance,
    IntelligenceRelevance,
    IntelligenceScoreBreakdown,
    IntelligenceVersionInfo,
    ScoreFactor,
)

logger = get_logger("signalnest.intelligence.read")

# Public serialization bounds (mirror the Batch 4A persistence bounds and re-assert
# them at the API boundary; a persisted row should never yield an unbounded response).
_MAX_STR = 2000
_MAX_QUOTE = 400
_MAX_EVIDENCE = 32
_MAX_HITS = 64
_MAX_FACTORS = 32
_HIT_KEYS = ("keyword_hits", "pain_point_hits", "audience_hits", "competitor_hits")


def _clip_str(value: object, limit: int) -> str:
    return str(value)[:limit]


def _clamp_int(value: object, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _clamp_confidence(value: object) -> float:
    return round(max(0.0, min(1.0, float(value))), 3)


def _bounded_hits(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_clip_str(v, _MAX_QUOTE) for v in values[:_MAX_HITS]]


def _map_attribute(attr: object) -> InferredAttribute | None:
    if not isinstance(attr, dict):
        return None
    return InferredAttribute(
        value=_clip_str(attr["value"], _MAX_STR),
        confidence=_clamp_confidence(attr.get("confidence", 0.0)),
        method=_clip_str(attr.get("method", ""), _MAX_STR),
    )


def _collect_evidence(inference: dict) -> list[IntelligenceEvidenceItem]:
    """Gather bounded evidence spans across all inferred attributes + intent evidence."""
    spans: list[dict] = []
    for key in ("signal_type", "pain_point_dna", "sentiment"):
        attr = inference.get(key)
        if isinstance(attr, dict):
            spans.extend(s for s in attr.get("evidence", []) if isinstance(s, dict))
    spans.extend(s for s in inference.get("intent_evidence", []) if isinstance(s, dict))

    items: list[IntelligenceEvidenceItem] = []
    for span in spans[:_MAX_EVIDENCE]:
        items.append(
            IntelligenceEvidenceItem(
                quote=_clip_str(span.get("quote", ""), _MAX_QUOTE),
                method=_clip_str(span.get("method", ""), _MAX_STR),
                start=int(span.get("start", 0)),
                end=int(span.get("end", 0)),
            )
        )
    return items


def _map_facts(facts: dict) -> IntelligenceFacts:
    # ``author`` is intentionally never read here (PII caution — excluded in 4B).
    return IntelligenceFacts(
        source_type=_clip_str(facts["source_type"], _MAX_STR),
        market=(_clip_str(facts["market"], _MAX_STR) if facts.get("market") is not None else None),
        language=_clip_str(facts.get("language", ""), _MAX_STR),
        published_days_ago=float(facts.get("published_days_ago", 0.0)),
        char_count=int(facts.get("char_count", 0)),
        word_count=int(facts.get("word_count", 0)),
        excerpt=_clip_str(facts.get("excerpt", ""), _MAX_STR),
        distinct_source_types=int(facts.get("distinct_source_types", 1)),
        duplicate_count=int(facts.get("duplicate_count", 1)),
        engagement=int(facts.get("engagement", 0)),
    )


def _map_inference(inference: dict) -> IntelligenceInference:
    return IntelligenceInference(
        signal_type=_map_attribute(inference.get("signal_type")),
        pain_point_dna=_map_attribute(inference.get("pain_point_dna")),
        sentiment=_map_attribute(inference.get("sentiment")),
        has_buying_intent=bool(inference.get("has_buying_intent", False)),
        has_competitor_dissatisfaction=bool(
            inference.get("has_competitor_dissatisfaction", False)
        ),
    )


def _map_relevance(relevance: dict) -> IntelligenceRelevance:
    # ``exclusion_hits`` is intentionally never read (operator-only tuning detail).
    return IntelligenceRelevance(
        score=_clamp_int(relevance.get("score", 0), 0, 100),
        below_action_floor=bool(relevance.get("below_action_floor", False)),
        **{key: _bounded_hits(relevance.get(key, [])) for key in _HIT_KEYS},
    )


def _map_score(score_components: dict, score_total: int) -> IntelligenceScoreBreakdown:
    raw_factors = score_components.get("factors", {})
    factors: dict[str, ScoreFactor] = {}
    if isinstance(raw_factors, dict):
        for name in sorted(raw_factors)[:_MAX_FACTORS]:
            f = raw_factors[name]
            if isinstance(f, dict):
                factors[_clip_str(name, _MAX_STR)] = ScoreFactor(
                    weight=float(f.get("weight", 0.0)),
                    value=float(f.get("value", 0.0)),
                    points=float(f.get("points", 0.0)),
                )
    return IntelligenceScoreBreakdown(
        total=_clamp_int(score_components.get("total", score_total), 0, 100),
        classification=_clip_str(score_components.get("classification", ""), _MAX_STR),
        version=_clip_str(score_components.get("version", ""), _MAX_STR),
        factors=factors,
    )


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _map_record(record: SignalIntelligenceRecord) -> IntelligencePayload:
    """Map an ORM record into the bounded public payload. Raises on a malformed row."""
    facts = record.facts if isinstance(record.facts, dict) else {}
    inference = record.inference if isinstance(record.inference, dict) else {}
    relevance = record.relevance if isinstance(record.relevance, dict) else {}
    score_components = record.score_components if isinstance(record.score_components, dict) else {}

    return IntelligencePayload(
        # The identifier is the exact record already selected by the scoped read
        # service — never a second lookup, never inferred from the opportunity id.
        intelligence_record_id=record.id,
        classification=_clip_str(record.classification, _MAX_STR),
        decision=(_clip_str(record.decision, _MAX_STR) if record.decision is not None else None),
        is_simulated=bool(record.is_simulated),
        rationale=(_clip_str(record.rationale, _MAX_STR) if record.rationale is not None else None),
        created_at=_as_utc(record.created_at),
        facts=_map_facts(facts),
        inference=_map_inference(inference),
        relevance=_map_relevance(relevance),
        score=_map_score(score_components, record.score_total),
        evidence=_collect_evidence(inference),
        provenance=IntelligenceProvenance(
            enricher=_clip_str(record.enricher, _MAX_STR),
            analysis_version=_clip_str(record.analysis_version, _MAX_STR),
            scoring_version=_clip_str(record.scoring_version, _MAX_STR),
        ),
        version=IntelligenceVersionInfo(
            analysis_version=_clip_str(record.analysis_version, _MAX_STR),
            scoring_version=_clip_str(record.scoring_version, _MAX_STR),
        ),
    )


def get_opportunity_intelligence(
    db: Session,
    *,
    workspace_id: str,
    opportunity_id: str,
) -> IntelligencePayload | None:
    """Load, map and bound the latest eligible intelligence for one opportunity.

    The caller (route) must already have authorized the opportunity within the
    workspace. Returns the typed payload, or ``None`` for a valid absence (no eligible
    record) **or** a malformed row that failed safe. Never raises for a malformed row.
    """
    started = time.perf_counter()
    record = get_latest_for_opportunity(
        db, workspace_id=workspace_id, opportunity_id=opportunity_id
    )
    if record is None:
        log_event(
            logger,
            "intelligence_read_absent",
            outcome="absent",
            duration_ms=(time.perf_counter() - started) * 1000,
            workspace_id=workspace_id,
            opportunity_id=opportunity_id,
        )
        return None

    try:
        payload = _map_record(record)
    except Exception:
        # Fail safe: never surface a partial/again-untrusted object or a 500, and never
        # log the payload — only coarse versions + outcome.
        log_event(
            logger,
            "intelligence_read_malformed",
            outcome="malformed",
            duration_ms=(time.perf_counter() - started) * 1000,
            workspace_id=workspace_id,
            opportunity_id=opportunity_id,
            analysis_version=record.analysis_version,
            scoring_version=record.scoring_version,
        )
        return None

    log_event(
        logger,
        "intelligence_read_success",
        outcome="success",
        duration_ms=(time.perf_counter() - started) * 1000,
        workspace_id=workspace_id,
        opportunity_id=opportunity_id,
        analysis_version=record.analysis_version,
        scoring_version=record.scoring_version,
    )
    return payload
