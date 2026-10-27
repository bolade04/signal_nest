"""Operator takedown of a live source (P6-PRIV-4).

A takedown answers a publisher's (or counsel's) request to stop using a feed. In one
transaction it:

1. records a :class:`~app.connectors.models.SourceTakedown` for the source key — from
   then on :mod:`app.connectors.live` never fetches that key, whatever its registry
   record says. The record is the suppression itself and nothing more (D-9d): the key,
   the time and aggregate counts. It never holds a user id, free text or a reference,
   so nothing that is later anonymised or expired can re-enable the source;
2. purges every signal the source produced, in **every** tenant: raw and normalized
   signals, their location evidence and intelligence records;
3. deletes each opportunity built only from that source's signals (with its scores,
   validation evidence and the customers' feedback on it) and, for an opportunity that
   also cites other sources, removes the source's entries from its observed evidence;
4. writes one audit entry in every affected workspace, naming what was removed there,
   who did it (``actor_user_id``), a fixed generic explanation and the opaque
   ``case_id`` of the request. That audit row — under the audit log's own retention —
   is where attribution lives. The operator's free-text *reason* and ticket id go only
   into the restricted operator case record
   (:class:`~app.connectors.models.SourceTakedownCase`, D-9e): never into an audit
   entry, an export, a log line or a metric;
5. drops the source's feed bytes from this process's cache (a worker process drops
   them at its next scout run).

The result is **verifiable**: the remaining counts are recounted from the database
after the purge, not derived from what was deleted, and :func:`source_signal_counts`
answers the same question at any later time. A takedown is permanent for its key and
idempotent — repeating it re-runs the purge and reports the same zero remainder; the
first purge's counts are kept and each repeat appends ``{at, purged}`` (counts only)
to the record's history.

**Runs in flight.** A scout run reads takedowns when it starts, then fetches. Before it
writes any live signal it takes each source's lock and re-reads the takedowns
(:func:`without_taken_down`); a takedown takes the same lock exclusively. On
PostgreSQL this is a per-key transaction-scoped advisory lock, so a takedown either
commits before the run's re-check (the run drops that source's signals) or waits until
the run has committed (the purge then sees and removes them). SQLite serialises all
writers, and a run holds the write lock from its start, which gives the same order.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import BigInteger, Text, cast, delete, func, literal, select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.connectors.feedcache import feed_cache
from app.connectors.models import SourceTakedown, SourceTakedownCase
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
#: Audit action written in each workspace a takedown removed something from.
AUDIT_ACTION = "data_source.taken_down"
_AUDIT_REASON = (
    "The platform operator took down a data source; content it supplied was removed "
    "from this workspace."
)
#: Most opportunity ids one audit entry lists per kind (the counts are always exact).
_AUDIT_MAX_IDS = 500
#: Longest accepted operator reason (free text, private: the case record only).
REASON_MAX_LENGTH = 2000
#: Longest accepted ``external_reference`` (the operator's own ticket id, private).
EXTERNAL_REFERENCE_MAX_LENGTH = 200
#: Unicode categories an external reference may never contain: C0/C1 controls (Cc),
#: format characters such as zero-width and bidi overrides (Cf), private use (Co),
#: surrogates (Cs), line and paragraph separators (Zl, Zp). Operators read the value in
#: the case listing; a bidi override could make a "ticket id" read as something else.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Zl", "Zp"})


def validate_external_reference(value: str | None) -> str | None:
    """The operator's own ticket / request id, or ``None``; :class:`ValidationDomainError`.

    Optional, at most 200 characters on one line, with no control, format, private-use,
    surrogate or line/paragraph-separator character. It is **private** (D-9e): kept in
    the operator case record only. An ``@`` is not refused any more because nothing
    here is shown to a customer; a string without ``@`` was never evidence that a
    reference is free of sensitive content, which is why it no longer leaves the case
    record at all.
    """
    # Only ordinary spaces are trimmed: a line separator or control at either end is
    # refused below rather than silently dropped.
    reference = (value or "").strip(" ")
    if not reference:
        return None
    if len(reference) > EXTERNAL_REFERENCE_MAX_LENGTH:
        raise ValidationDomainError(
            f"external_reference must be at most {EXTERNAL_REFERENCE_MAX_LENGTH} characters."
        )
    if any(ch.isspace() and ch != " " for ch in reference) or any(
        ord(ch) < 32 or ord(ch) == 127 or unicodedata.category(ch) in _INVISIBLE_CATEGORIES
        for ch in reference
    ):
        raise ValidationDomainError("external_reference must be a single line of printable text.")
    return reference


def validate_reason(value: str) -> str:
    """The operator's free-text reason, or :class:`ValidationDomainError`.

    1–2000 characters; line breaks and tabs are allowed, other control characters,
    private-use characters and surrogates are not. Private: kept in the operator case
    record only, never copied anywhere a customer, an export or a log can read it.
    """
    reason = (value or "").strip()
    if not reason:
        raise ValidationDomainError("A takedown reason is required.")
    if len(reason) > REASON_MAX_LENGTH:
        raise ValidationDomainError(f"reason must be at most {REASON_MAX_LENGTH} characters.")
    if any(
        (unicodedata.category(ch) in {"Cc", "Cs", "Co"} and ch not in "\n\r\t") for ch in reason
    ):
        raise ValidationDomainError("reason must not contain control or private-use characters.")
    return reason


def _chunks(ids: list[str]):
    for i in range(0, len(ids), _CHUNK):
        yield ids[i : i + _CHUNK]


def active_takedowns(db: Session) -> frozenset[str]:
    return frozenset(db.scalars(select(SourceTakedown.source_key)))


def _lock_id(source_key: str) -> int:
    digest = hashlib.sha256(f"signalnest.source-takedown:{source_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_sources(db: Session, keys: Iterable[str], *, exclusive: bool) -> None:
    """Hold each source's takedown lock until this transaction ends (PostgreSQL).

    Shared for a scout run writing a source's signals, exclusive for a takedown. Keys
    are locked in sorted order, so two runs never deadlock. SQLite needs nothing here:
    it admits one writer at a time, and a run is a writer from its first statement.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    lock = func.pg_advisory_xact_lock if exclusive else func.pg_advisory_xact_lock_shared
    for key in sorted(set(keys)):
        db.execute(select(lock(cast(literal(_lock_id(key)), BigInteger))))


def _signal_source_key(signal: object) -> str | None:
    return (getattr(signal, "attribution", None) or {}).get("source_key")


def without_taken_down(db: Session, signals: list) -> list:
    """``signals`` minus those of any source taken down since the run read takedowns.

    Takes each live source's shared lock first and keeps it until the run's transaction
    ends, so a takedown cannot slip between this re-check and the run's commit.
    """
    keys = {k for k in map(_signal_source_key, signals) if k}
    if not keys:
        return list(signals)
    lock_sources(db, keys, exclusive=False)
    gone = set(
        db.scalars(select(SourceTakedown.source_key).where(SourceTakedown.source_key.in_(keys)))
    )
    if not gone:
        return list(signals)
    for key in sorted(gone):
        logger.warning(
            "connector.source_dropped_after_takedown", extra={"extra_fields": {"source_key": key}}
        )
    return [s for s in signals if _signal_source_key(s) not in gone]


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
    case_id: str

    @property
    def verified(self) -> bool:
        return all(v == 0 for v in self.remaining.values())


_TenantKey = tuple[str, str]  # (organization_id, workspace_id)


def _tenant_counts() -> dict:
    return {
        "raw_signals": 0,
        "normalized_signals": 0,
        "opportunities_deleted": 0,
        "opportunities_redacted": 0,
        "feedback_removed": 0,
    }


def _purge(db: Session, source_key: str) -> tuple[dict[str, int], dict[_TenantKey, dict]]:
    """Remove the source's data everywhere; return the totals and a per-tenant split."""
    tenants: dict[_TenantKey, dict] = defaultdict(
        lambda: {**_tenant_counts(), "deleted_ids": [], "redacted_ids": []}
    )
    raw_rows = list(
        db.execute(
            select(RawSignal.id, RawSignal.organization_id, RawSignal.workspace_id).where(
                RawSignal.source_key == source_key
            )
        )
    )
    raw_ids = [r for r, _, _ in raw_rows]
    for _, org, ws in raw_rows:
        tenants[(org, ws)]["raw_signals"] += 1
    norm_cols = select(
        NormalizedSignal.id,
        NormalizedSignal.cluster_id,
        NormalizedSignal.organization_id,
        NormalizedSignal.workspace_id,
    )
    norm_rows = list(db.execute(norm_cols.where(NormalizedSignal.source_key == source_key)))
    for chunk in _chunks(raw_ids):
        norm_rows.extend(db.execute(norm_cols.where(NormalizedSignal.raw_signal_id.in_(chunk))))
    norm_by_id = {n: (org, ws) for n, _, org, ws in norm_rows}
    norm_ids = sorted(norm_by_id)
    for org, ws in norm_by_id.values():
        tenants[(org, ws)]["normalized_signals"] += 1
    purged_norm = set(norm_ids)
    cluster_ids = sorted({c for _, c, _, _ in norm_rows if c})

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
                tenant = tenants[(opp.organization_id, opp.workspace_id)]
                tenant["opportunities_deleted"] += 1
                tenant["deleted_ids"].append(opp.id)
    for opp in _opportunities_citing(db, source_key):
        if opp.id in doomed:
            continue
        opp.observed_evidence = [
            e
            for e in opp.observed_evidence
            if not (isinstance(e, dict) and e.get("source_key") == source_key)
        ]
        redacted.add(opp.id)
        tenant = tenants[(opp.organization_id, opp.workspace_id)]
        tenant["opportunities_redacted"] += 1
        tenant["redacted_ids"].append(opp.id)
    for chunk in _chunks(doomed):
        feedback = list(
            db.execute(
                select(OpportunityFeedback.organization_id, OpportunityFeedback.workspace_id).where(
                    OpportunityFeedback.opportunity_id.in_(chunk)
                )
            )
        )
        counts["feedback_removed"] += len(feedback)
        for org, ws in feedback:
            tenants[(org, ws)]["feedback_removed"] += 1
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
    return counts, dict(tenants)


def _audit_tenants(
    db: Session,
    tenants: dict[_TenantKey, dict],
    *,
    takedown: SourceTakedown,
    case_id: str,
    actor_user_id: str | None,
    repeat: bool,
) -> int:
    """One audit entry per workspace the purge removed something from."""
    written = 0
    for (org, ws), tenant in sorted(tenants.items()):
        removed = {k: tenant[k] for k in _tenant_counts()}
        if not any(removed.values()):
            continue
        record_audit(
            db,
            organization_id=org,
            workspace_id=ws,
            actor_user_id=actor_user_id,
            action=AUDIT_ACTION,
            entity_type="source_takedown",
            entity_id=takedown.id,
            reason=_AUDIT_REASON,
            new_state=removed,
            context={
                "source_key": takedown.source_key,
                "case_id": case_id,
                "repeat": repeat,
                "opportunities_deleted": tenant["deleted_ids"][:_AUDIT_MAX_IDS],
                "opportunities_redacted": tenant["redacted_ids"][:_AUDIT_MAX_IDS],
            },
        )
        written += 1
    return written


def takedown_source(
    db: Session,
    *,
    source_key: str,
    reason: str,
    external_reference: str | None = None,
    actor_user_id: str | None,
) -> TakedownResult:
    """Take ``source_key`` down (or repeat a takedown) on behalf of ``actor_user_id``.

    ``reason`` and ``external_reference`` go into a new :class:`SourceTakedownCase`
    (the restricted operator record) and nowhere else. The affected workspaces' audit
    entries carry ``actor_user_id``, the fixed explanation and the case's opaque id; the
    :class:`SourceTakedown` row records none of them.
    """
    if not SOURCE_KEY_PATTERN.match(source_key):
        raise ValidationDomainError("source_key must be a lowercase slug.")
    reason = validate_reason(reason)
    external_reference = validate_external_reference(external_reference)
    lock_sources(db, [source_key], exclusive=True)  # waits for runs writing this source
    existing = db.scalar(select(SourceTakedown).where(SourceTakedown.source_key == source_key))
    known = existing is not None or any(s.source_key == source_key for s in known_sources())
    if not known and not db.scalar(
        select(func.count()).select_from(RawSignal).where(RawSignal.source_key == source_key)
    ):
        raise NotFoundError("No source with this key is registered or holds signals.")

    takedown = existing
    if takedown is None:
        takedown = SourceTakedown(source_key=source_key)
        db.add(takedown)
        db.flush()
    case = SourceTakedownCase(
        takedown_id=takedown.id,
        source_key=source_key,
        reason=reason,
        external_reference=external_reference,
        actor_user_id=actor_user_id,
    )
    db.add(case)
    db.flush()
    purged, tenants = _purge(db, source_key)
    if existing is None:
        takedown.purge_summary = purged
    else:
        # The first purge's counts are the record of what the takedown removed; a
        # repeat is appended, never written over them. Counts and a time only: the
        # actor is in the audit entries and the case record, never in this row (D-9d).
        takedown.repeat_purges = [
            *(takedown.repeat_purges or []),
            {"at": datetime.now(UTC).isoformat(), "purged": purged},
        ]
    audited = _audit_tenants(
        db,
        tenants,
        takedown=takedown,
        case_id=case.id,
        actor_user_id=actor_user_id,
        repeat=existing is not None,
    )
    db.flush()
    feed_cache.forget(source_key)
    remaining = source_signal_counts(db, source_key)
    logger.warning(
        "connector.source_taken_down",
        extra={
            "extra_fields": {
                "source_key": source_key,
                "repeat": existing is not None,
                "case_id": case.id,
                "audited_workspaces": audited,
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
        case_id=case.id,
    )


def known_sources():
    """Registry records visible to operators: the approved list, or the replay corpus."""
    from app.core.config import get_settings

    settings = get_settings()
    if settings.connector_rss_replay_origin:
        from app.connectors.replay import replay_targets

        return [t.source for t in replay_targets(settings.connector_rss_replay_origin)]
    return list(APPROVED_SOURCES)
