"""Reconcile unresolved Scout admissions (D-25 execution controls, PROPOSED).

An admission (``scout_admissions``) stays unresolved until its admitting attempt records a
known end. One whose attempt is gone (a crash) or whose end is unknown (a timeout after the
request may have been sent) keeps its Scout from admitting anything (K = 1) until an operator
reconciles it here. Reconciliation is a declaration: the operator has ascertained, by means
outside SignalNest, that the external operation is over. It changes no accounting.

    python -m app.jobs.reconcile list --scout <scout_request_id>
    python -m app.jobs.reconcile resolve --admission <id> --operator <name> \\
        --reason <code> [--note <text, at most 200 characters>]

Refused over a valid in-flight admission (its attempt still holds authority and has recorded no
end), and (contract §9.3) over any admission with no recorded end until its own deadline
(``admitted_at`` + the operation's client deadline + the lease, the waiting rule's) has passed
by the database clock: until then SignalNest's own client may still be in the call. After the
deadline only the external party's side is unknown; reconciling is the operator's declaration
that it is not still working. Idempotent. Audited (``scout_admission.reconciled``). No API or
contract change.

The listing separates exactly three categories: the current owner's admissions, earlier owners'
outstanding admissions, and admissions with an unknown end; each with its job's status and
``lease_expires_at``, its deadline and whether the deadline has passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

#: The reason codes reconciliation accepts (the note adds at most 200 characters).
REASON_CODES = frozenset(
    {"worker_crashed", "outcome_confirmed_ended", "provider_confirmed", "operator_decision"}
)
NOTE_MAX = 200


class ReconciliationRefused(Exception):  # noqa: N818 - a refusal, reported to the operator
    """The admission may not be reconciled (or the request was malformed)."""


def _attempt_valid(db: Session, row) -> bool:
    from app.jobs.models import Job

    job = db.execute(
        select(Job.status, Job.attempt_count, Job.worker_id, Job.lease_expires_at).where(
            Job.id == row.job_id
        )
    ).first()
    if job is None or job.status not in ("claimed", "running"):
        return False
    if job.attempt_count != row.attempt or job.worker_id != row.worker_id:
        return False
    expires = job.lease_expires_at
    if expires is None:
        return False
    return _aware(expires) > _db_now(db)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _db_now(db: Session) -> datetime:
    """Database time on PostgreSQL, like every lease and deadline comparison (contract
    §8.1.6); the process clock on SQLite."""
    if db.get_bind().dialect.name == "postgresql":
        return _aware(db.execute(select(func.clock_timestamp())).scalar_one())
    return datetime.now(UTC)


def _deadline(row) -> datetime:
    """The admission's own deadline: the waiting rule's (``admission_deadline_seconds``)."""
    from app.jobs.admission import admission_deadline_seconds

    return _aware(row.admitted_at) + timedelta(seconds=admission_deadline_seconds(row.operation))


#: The listing's three categories (contract §8.1.11, §9.3).
CURRENT_OWNER = "current_owner"
EARLIER_OWNERS_OUTSTANDING = "earlier_owners_outstanding"
UNKNOWN_END = "unknown_end"


def list_unresolved(db: Session, scout_request_id: str) -> dict:
    """The Scout's unresolved admissions in exactly three categories (contract §9.3): the
    current owner's (no recorded end), earlier owners' outstanding ones (no recorded end) and
    those with an unknown end; each with its job's status and lease expiry, its deadline and
    whether that deadline has passed by the database clock."""
    from app.jobs.models import Job
    from app.scouting_requests.models import ScoutAdmission, ScoutRequest

    request = db.get(ScoutRequest, scout_request_id)
    owner = (
        (request.owner_job_id, request.owner_attempt)
        if request is not None and request.owner_job_id
        else None
    )
    now = _db_now(db)
    out: dict = {
        "scout_request_id": scout_request_id,
        "owner": {"job_id": owner[0], "attempt": owner[1]} if owner else None,
        CURRENT_OWNER: [],
        EARLIER_OWNERS_OUTSTANDING: [],
        UNKNOWN_END: [],
    }
    rows = db.scalars(
        select(ScoutAdmission)
        .where(
            ScoutAdmission.scout_request_id == scout_request_id,
            ScoutAdmission.resolved_at.is_(None),
        )
        .order_by(ScoutAdmission.admitted_at)
    ).all()
    for row in rows:
        job = db.execute(
            select(Job.status, Job.lease_expires_at).where(Job.id == row.job_id)
        ).first()
        deadline = _deadline(row)
        item = {
            "admission_id": row.id,
            "operation": row.operation,
            "job_id": row.job_id,
            "attempt": row.attempt,
            "worker_id": row.worker_id,
            "admitted_at": _aware(row.admitted_at).isoformat(),
            "ended_at": _aware(row.ended_at).isoformat() if row.ended_at else None,
            "end_kind": row.end_kind,
            "job_status": job.status if job else None,
            "lease_expires_at": (
                _aware(job.lease_expires_at).isoformat() if job and job.lease_expires_at else None
            ),
            "deadline": deadline.isoformat(),
            "deadline_passed": now > deadline,
            "attempt_holds_authority": _attempt_valid(db, row),
        }
        if row.end_kind == "unknown":
            category = UNKNOWN_END
        elif owner is not None and (row.job_id, row.attempt) == owner:
            category = CURRENT_OWNER
        else:
            category = EARLIER_OWNERS_OUTSTANDING
        out[category].append(item)
    return out


def resolve(
    db: Session, *, admission_id: str, operator: str, reason: str, note: str | None = None
) -> bool:
    """Mark one unresolved admission resolved; returns False if it already was. Flushes."""
    from app.audit.service import record_audit
    from app.scouting_requests.models import ScoutAdmission

    if reason not in REASON_CODES:
        raise ReconciliationRefused(f"unknown reason code {reason!r}")
    if not operator or len(operator) > 200:
        raise ReconciliationRefused("an operator name (at most 200 characters) is required")
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise ReconciliationRefused(f"the note is limited to {NOTE_MAX} characters")
    row = db.execute(
        select(ScoutAdmission)
        .where(ScoutAdmission.id == admission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise ReconciliationRefused("no such admission")
    if row.resolved_at is not None:
        return False
    if row.end_kind != "unknown" and _attempt_valid(db, row):
        raise ReconciliationRefused(
            "refused: the admitting attempt still holds valid authority and has recorded no end"
        )
    if row.ended_at is None and not _db_now(db) > _deadline(row):
        raise ReconciliationRefused(
            "refused: the admission has no recorded end and its deadline "
            f"({_deadline(row).isoformat()}) has not passed (database clock); "
            "SignalNest's client may still be in the call"
        )
    now = datetime.now(UTC)
    result = db.execute(
        update(ScoutAdmission)
        .where(ScoutAdmission.id == admission_id, ScoutAdmission.resolved_at.is_(None))
        .values(
            resolved_at=now,
            resolved_by=operator,
            resolution_note=f"{reason}: {note}" if note else reason,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False  # resolved concurrently: idempotent
    record_audit(
        db,
        organization_id=row.organization_id,
        workspace_id=row.workspace_id,
        action="scout_admission.reconciled",
        entity_type="scout_admission",
        entity_id=row.id,
        reason=reason,
    )
    db.flush()
    return True


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI shell
    import app.db.models  # noqa: F401 - completes Base.metadata for this process (the audit row)
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(prog="python -m app.jobs.reconcile")
    sub = parser.add_subparsers(dest="command", required=True)
    lister = sub.add_parser("list")
    lister.add_argument("--scout", required=True)
    resolver = sub.add_parser("resolve")
    resolver.add_argument("--admission", required=True)
    resolver.add_argument("--operator", required=True)
    resolver.add_argument("--reason", required=True, choices=sorted(REASON_CODES))
    resolver.add_argument("--note")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        if args.command == "list":
            print(json.dumps(list_unresolved(db, args.scout), indent=2))
            return 0
        try:
            changed = resolve(
                db,
                admission_id=args.admission,
                operator=args.operator,
                reason=args.reason,
                note=args.note,
            )
        except ReconciliationRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2
        db.commit()
        print("reconciled" if changed else "already resolved")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
