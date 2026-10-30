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
end). Idempotent. Audited (``scout_admission.reconciled``). No API or contract change.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import select, update
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
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires is not None and expires > datetime.now(UTC)


def list_unresolved(db: Session, scout_request_id: str) -> dict:
    """The Scout's unresolved admissions, in the three categories (contract §8.1.11)."""
    from app.scouting_requests.models import ScoutAdmission, ScoutRequest

    request = db.get(ScoutRequest, scout_request_id)
    owner = (
        {
            "job_id": request.owner_job_id,
            "attempt": request.owner_attempt,
        }
        if request is not None and request.owner_job_id
        else None
    )
    out = {"scout_request_id": scout_request_id, "current_owner": owner, "outstanding": [],
           "unknown_outcome": []}
    rows = db.scalars(
        select(ScoutAdmission).where(
            ScoutAdmission.scout_request_id == scout_request_id,
            ScoutAdmission.resolved_at.is_(None),
        )
    ).all()
    for row in rows:
        item = {"admission_id": row.id, "operation": row.operation, "job_id": row.job_id,
                "attempt": row.attempt, "admitted_at": row.admitted_at.isoformat(),
                "attempt_holds_authority": _attempt_valid(db, row)}
        out["unknown_outcome" if row.end_kind == "unknown" else "outstanding"].append(item)
    return out


def resolve(db: Session, *, admission_id: str, operator: str, reason: str,
            note: str | None = None) -> bool:
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
        select(ScoutAdmission).where(ScoutAdmission.id == admission_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ReconciliationRefused("no such admission")
    if row.resolved_at is not None:
        return False
    if row.end_kind != "unknown" and _attempt_valid(db, row):
        raise ReconciliationRefused(
            "refused: the admitting attempt still holds valid authority and has recorded no end"
        )
    now = datetime.now(UTC)
    db.execute(
        update(ScoutAdmission)
        .where(ScoutAdmission.id == admission_id, ScoutAdmission.resolved_at.is_(None))
        .values(resolved_at=now, resolved_by=operator,
                resolution_note=f"{reason}: {note}" if note else reason, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    record_audit(db, organization_id=row.organization_id, workspace_id=row.workspace_id,
                 action="scout_admission.reconciled", entity_type="scout_admission",
                 entity_id=row.id, reason=reason)
    db.flush()
    return True


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI shell
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
            changed = resolve(db, admission_id=args.admission, operator=args.operator,
                              reason=args.reason, note=args.note)
        except ReconciliationRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2
        db.commit()
        print("reconciled" if changed else "already resolved")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
