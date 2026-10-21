"""Operator takedown of a live source (P6-PRIV-4).

A takedown answers a publisher's (or counsel's) request to stop using a feed. In one
transaction it:

1. records a :class:`~app.connectors.models.SourceTakedown` for the source key — from
   then on :mod:`app.connectors.live` never fetches that key, whatever its registry
   record says;
2. purges every signal the source produced, in **every** tenant: raw and normalized
   signals, their location evidence and intelligence records;
3. deletes each opportunity built only from that source's signals (with its scores,
   validation evidence and feedback) and, for an opportunity that also cites other
   sources, removes the source's entries from its observed evidence;
4. drops the source's feed bytes from this process's cache.

The result is **verifiable**: the remaining counts are recounted from the database
after the purge, not derived from what was deleted, and :func:`source_signal_counts`
answers the same question at any later time. A takedown is permanent for its key and
idempotent — repeating it re-runs the purge and reports the same zero remainder.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Text, cast, delete, func, select
from sqlalchemy.orm import Session

from app.connectors.feedcache import feed_cache
from app.connectors.models import SourceTakedown
from app.connectors.sources import APPROVED_SOURCES, SOURCE_KEY_PATTERN
from app.core.errors import NotFoundError, ValidationDomainError
from app.core.logging import get_logger
from app.feedback.models import OpportunityFeedback
from app.intelligence.records import SignalIntelligenceRecord
from app.opportunities.models import Opportunity
from app.signals.models import (
    NormalizedSignal,
    RawSignal,
    SignalCluster,
    SignalLocationEvidence,
)

logger = get_logger("signalnest.connectors.takedown")

_CHUNK = 500


def _chunks(ids: list[str]):
    for i in range(0, len(ids), _CHUNK):
        yield ids[i : i + _CHUNK]


def active_takedowns(db: Session) -> frozenset[str]:
    return frozenset(db.scalars(select(SourceTakedown.source_key)))


def _evidence_cites(evidence: object, source_key: str) -> bool:
    return isinstance(evidence, list) and any(
        isinstance(e, dict) and e.get("source_key") == source_key for e in evidence
    )


def _opportunities_citing(db: Session, source_key: str) -> list[Opportunity]:
    # A textual prefilter keeps this one indexed-ish scan portable across SQLite and
    # PostgreSQL; the exact test is done on the decoded JSON.
    rows = db.scalars(
        select(Opportunity).where(
            cast(Opportunity.observed_evidence, Text).like(f"%{source_key}%")
        )
    )
    return [o for o in rows if _evidence_cites(o.observed_evidence, source_key)]


def source_signal_counts(db: Session, source_key: str) -> dict[str, int]:
    """What the database still holds for ``source_key``, counted now."""
    return {
        "raw_signals": db.scalar(
            select(func.count()).select_from(RawSignal).where(RawSignal.source_key == source_key)
        )
        or 0,
        "normalized_signals": db.scalar(
            select(func.count())
            .select_from(NormalizedSignal)
            .where(NormalizedSignal.source_key == source_key)
        )
        or 0,
        "opportunities_citing": len(_opportunities_citing(db, source_key)),
    }


@dataclass(frozen=True)
class TakedownResult:
    takedown: SourceTakedown
    purged: dict[str, int]
    remaining: dict[str, int]
    already_taken_down: bool

    @property
    def verified(self) -> bool:
        return all(v == 0 for v in self.remaining.values())


def _purge(db: Session, source_key: str) -> dict[str, int]:
    raw_ids = list(db.scalars(select(RawSignal.id).where(RawSignal.source_key == source_key)))
    norm_cols = select(NormalizedSignal.id, NormalizedSignal.cluster_id)
    norm_rows = list(db.execute(norm_cols.where(NormalizedSignal.source_key == source_key)))
    for chunk in _chunks(raw_ids):
        norm_rows.extend(db.execute(norm_cols.where(NormalizedSignal.raw_signal_id.in_(chunk))))
    norm_ids = sorted({n for n, _ in norm_rows})
    purged_norm = set(norm_ids)
    cluster_ids = sorted({c for _, c in norm_rows if c})

    counts = {
        "raw_signals": len(raw_ids),
        "normalized_signals": len(norm_ids),
        "opportunities_deleted": 0,
        "opportunities_redacted": 0,
        "feedback_removed": 0,
        "clusters_deleted": 0,
    }

    # Opportunities: built only from this source -> delete; mixed -> redact evidence.
    doomed: list[str] = []
    redacted: set[str] = set()
    for chunk in _chunks(cluster_ids):
        for opp in db.scalars(select(Opportunity).where(Opportunity.cluster_id.in_(chunk))):
            members = set(
                db.scalars(
                    select(NormalizedSignal.id).where(NormalizedSignal.cluster_id == opp.cluster_id)
                )
            )
            if members <= purged_norm:
                doomed.append(opp.id)
    for opp in _opportunities_citing(db, source_key):
        if opp.id in doomed:
            continue
        opp.observed_evidence = [
            e
            for e in opp.observed_evidence
            if not (isinstance(e, dict) and e.get("source_key") == source_key)
        ]
        redacted.add(opp.id)
    for chunk in _chunks(doomed):
        counts["feedback_removed"] += (
            db.scalar(
                select(func.count())
                .select_from(OpportunityFeedback)
                .where(OpportunityFeedback.opportunity_id.in_(chunk))
            )
            or 0
        )
        db.execute(
            delete(OpportunityFeedback).where(OpportunityFeedback.opportunity_id.in_(chunk))
        )
        db.execute(delete(Opportunity).where(Opportunity.id.in_(chunk)))
    counts["opportunities_deleted"] = len(doomed)
    counts["opportunities_redacted"] = len(redacted)

    for chunk in _chunks(norm_ids):
        db.execute(
            delete(SignalIntelligenceRecord).where(
                SignalIntelligenceRecord.normalized_signal_id.in_(chunk)
            )
        )
        db.execute(
            delete(SignalLocationEvidence).where(
                SignalLocationEvidence.normalized_signal_id.in_(chunk)
            )
        )
        db.execute(delete(NormalizedSignal).where(NormalizedSignal.id.in_(chunk)))
    for chunk in _chunks(raw_ids):
        db.execute(delete(RawSignal).where(RawSignal.id.in_(chunk)))

    # Clusters left without a member are derived only from the purged text.
    for chunk in _chunks(cluster_ids):
        for cluster_id in chunk:
            alive = db.scalar(
                select(func.count())
                .select_from(NormalizedSignal)
                .where(NormalizedSignal.cluster_id == cluster_id)
            )
            if not alive:
                db.execute(
                    delete(SignalCluster).where(SignalCluster.id == cluster_id)
                )
                counts["clusters_deleted"] += 1
    db.flush()
    return counts


def takedown_source(
    db: Session,
    *,
    source_key: str,
    reason: str,
    reference: str | None,
    actor_user_id: str | None,
) -> TakedownResult:
    if not SOURCE_KEY_PATTERN.match(source_key):
        raise ValidationDomainError("source_key must be a lowercase slug.")
    reason = reason.strip()
    if not reason:
        raise ValidationDomainError("A takedown reason is required.")
    existing = db.scalar(select(SourceTakedown).where(SourceTakedown.source_key == source_key))
    known = existing is not None or any(s.source_key == source_key for s in known_sources())
    if not known and not db.scalar(
        select(func.count()).select_from(RawSignal).where(RawSignal.source_key == source_key)
    ):
        raise NotFoundError("No source with this key is registered or holds signals.")

    takedown = existing
    if takedown is None:
        takedown = SourceTakedown(
            source_key=source_key,
            reason=reason,
            reference=(reference or "").strip() or None,
            actor_user_id=actor_user_id,
        )
        db.add(takedown)
        db.flush()
    purged = _purge(db, source_key)
    takedown.purge_summary = purged
    db.flush()
    feed_cache.forget(source_key)
    remaining = source_signal_counts(db, source_key)
    logger.warning(
        "connector.source_taken_down",
        extra={
            "extra_fields": {
                "source_key": source_key,
                "repeat": existing is not None,
                **{f"purged_{k}": v for k, v in purged.items()},
                **{f"remaining_{k}": v for k, v in remaining.items()},
            }
        },
    )
    return TakedownResult(
        takedown=takedown,
        purged=purged,
        remaining=remaining,
        already_taken_down=existing is not None,
    )


def known_sources():
    """Registry records visible to operators: the approved list, or the replay corpus."""
    from app.core.config import get_settings

    settings = get_settings()
    if settings.connector_rss_replay_origin:
        from app.connectors.replay import replay_targets

        return [t.source for t in replay_targets(settings.connector_rss_replay_origin)]
    return list(APPROVED_SOURCES)
