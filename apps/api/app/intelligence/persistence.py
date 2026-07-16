"""Persistence repository for signal intelligence (Batch 4A).

Turns a deterministic Batch 3 :class:`~app.intelligence.models.OpportunityCandidate`
into a durable :class:`~app.intelligence.records.SignalIntelligenceRecord` and links
it to its opportunity once one exists. Two disciplines drive this module:

* **Concurrency-safe idempotency, not check-then-insert.** :func:`persist_intelligence`
  inserts inside a SAVEPOINT and lets the database's unique constraint be the final
  arbiter: on an ``IntegrityError`` it rolls back only the savepoint and returns the
  already-persisted row. Retries and concurrent workers converge on one row.
* **Bounded, sanitized payloads.** :func:`serialize_candidate` caps list lengths and
  string lengths before anything reaches a JSON column. The source excerpt was
  already sanitized by the Batch 3 extractor, so no raw untrusted text is stored.

The functions never open, commit or roll back the *outer* transaction — the caller
owns it — and never touch tenant scope beyond the ids it is handed.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.intelligence.clustering import content_fingerprint
from app.intelligence.models import OpportunityCandidate
from app.intelligence.records import SignalIntelligenceRecord

#: Version of the analysis (extraction/enrichment) pipeline that produced a record.
#: Distinct from the scoring version (``candidate.score.version``); bump when the
#: extraction/inference behavior changes so historical rows stay interpretable.
ANALYSIS_VERSION = "3b"

# Defensive payload bounds. The Batch 3 domain output is already bounded, but a
# persisted JSON column should never be able to grow without limit.
_MAX_STR = 2000
_MAX_QUOTE = 400
_MAX_EVIDENCE = 32
_MAX_HITS = 64


def _clip(value: object, limit: int) -> object:
    return value[:limit] if isinstance(value, str) else value


def _bounded_evidence(spans: list) -> list:
    out: list = []
    for span in spans[:_MAX_EVIDENCE]:
        if isinstance(span, dict):
            out.append({**span, "quote": _clip(span.get("quote", ""), _MAX_QUOTE)})
    return out


def _bounded_attr(attr: dict | None) -> dict | None:
    if not isinstance(attr, dict):
        return None
    return {**attr, "evidence": _bounded_evidence(list(attr.get("evidence", [])))}


def serialize_candidate(candidate: OpportunityCandidate) -> dict:
    """Bounded JSON payloads derived from a candidate (facts vs inference kept apart)."""
    data = candidate.as_dict()
    facts = dict(data["facts"])
    facts["excerpt"] = _clip(facts.get("excerpt", ""), _MAX_STR)

    inference = dict(data["intelligence"])
    inference["signal_type"] = _bounded_attr(inference.get("signal_type"))
    inference["pain_point_dna"] = _bounded_attr(inference.get("pain_point_dna"))
    inference["sentiment"] = _bounded_attr(inference.get("sentiment"))
    inference["intent_evidence"] = _bounded_evidence(list(inference.get("intent_evidence", [])))

    relevance = dict(data["relevance"])
    for key in ("keyword_hits", "pain_point_hits", "audience_hits", "competitor_hits",
                "exclusion_hits"):
        if isinstance(relevance.get(key), list):
            relevance[key] = relevance[key][:_MAX_HITS]

    return {
        "facts": facts,
        "inference": inference,
        "relevance": relevance,
        "score_components": data["score"],
    }


def persist_intelligence(
    db: Session,
    *,
    organization_id: str,
    workspace_id: str,
    scout_request_id: str,
    normalized_signal_id: str,
    candidate: OpportunityCandidate,
    location_id: str | None = None,
    analysis_version: str = ANALYSIS_VERSION,
    enricher_name: str = "deterministic",
) -> SignalIntelligenceRecord:
    """Idempotently persist one candidate as a scoped, immutable record.

    Inserts inside a SAVEPOINT so that a unique-constraint collision (a retry or a
    concurrent worker having written the same identity) rolls back only the
    savepoint — leaving the outer transaction usable — and the already-persisted
    row is returned instead. The caller owns the outer transaction/commit.
    """
    fingerprint = content_fingerprint(candidate.facts.excerpt)
    scoring_version = candidate.score.version
    payload = serialize_candidate(candidate)

    record = SignalIntelligenceRecord(
        organization_id=organization_id,
        workspace_id=workspace_id,
        scout_request_id=scout_request_id,
        normalized_signal_id=normalized_signal_id,
        location_id=location_id,
        analysis_version=analysis_version,
        scoring_version=scoring_version,
        fingerprint=fingerprint,
        enricher=enricher_name,
        accepted=candidate.accepted,
        classification=candidate.score.classification.value,
        decision=candidate.decision.value if candidate.decision else None,
        rejection_reason=candidate.rejection.value if candidate.rejection else None,
        cluster_key=candidate.cluster_key,
        score_total=candidate.score.total,
        evidence_count=candidate.evidence_count,
        rationale=_clip(candidate.rationale, _MAX_STR),
        is_simulated=candidate.is_simulated,
        provenance={
            "analysis_version": analysis_version,
            "scoring_version": scoring_version,
            "enricher": enricher_name,
            "fingerprint": fingerprint,
        },
        **payload,
    )

    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
        return record
    except IntegrityError:
        existing = db.scalar(
            select(SignalIntelligenceRecord).where(
                SignalIntelligenceRecord.workspace_id == workspace_id,
                SignalIntelligenceRecord.normalized_signal_id == normalized_signal_id,
                SignalIntelligenceRecord.analysis_version == analysis_version,
                SignalIntelligenceRecord.scoring_version == scoring_version,
                SignalIntelligenceRecord.fingerprint == fingerprint,
            )
        )
        if existing is None:  # pragma: no cover - constraint fired but row not found
            raise
        return existing


def get_latest_for_opportunity(
    db: Session,
    *,
    workspace_id: str,
    opportunity_id: str,
) -> SignalIntelligenceRecord | None:
    """Return the single deterministic *latest eligible* record for one opportunity.

    Read-only (Batch 4B). Both scope args are **mandatory** and applied together so a
    record is never reachable by ``id`` or ``normalized_signal_id`` alone: the query
    is scoped by ``workspace_id`` **and** ``opportunity_id`` and only accepted,
    opportunity-linked rows are eligible (rejected/suppressed rows carry
    ``accepted == False`` and are excluded). A single indexed query orders by
    ``score_total DESC, created_at DESC, id ASC`` and takes the first row; the final
    ``id ASC`` is a total tiebreak so the result is byte-stable even for
    duplicate-equivalent rows. Returns ``None`` when no eligible record exists
    (a valid absence). Performs no mutation, flush or commit.
    """
    return db.scalar(
        select(SignalIntelligenceRecord)
        .where(
            SignalIntelligenceRecord.workspace_id == workspace_id,
            SignalIntelligenceRecord.opportunity_id == opportunity_id,
            SignalIntelligenceRecord.accepted.is_(True),
        )
        .order_by(
            SignalIntelligenceRecord.score_total.desc(),
            SignalIntelligenceRecord.created_at.desc(),
            SignalIntelligenceRecord.id.asc(),
        )
        .limit(1)
    )


def attach_opportunity(
    db: Session,
    *,
    workspace_id: str,
    opportunity_id: str,
    normalized_signal_ids: list[str],
) -> int:
    """Link intelligence rows to their opportunity, scoped to one workspace.

    Only unlinked rows whose ``normalized_signal_id`` is in the given set *and*
    whose ``workspace_id`` matches are updated, so an intelligence row can never be
    associated with an opportunity from another workspace. Returns the row count.
    """
    if not normalized_signal_ids:
        return 0
    result = db.execute(
        update(SignalIntelligenceRecord)
        .where(
            SignalIntelligenceRecord.workspace_id == workspace_id,
            SignalIntelligenceRecord.normalized_signal_id.in_(normalized_signal_ids),
            SignalIntelligenceRecord.opportunity_id.is_(None),
        )
        .values(opportunity_id=opportunity_id)
    )
    return result.rowcount or 0
