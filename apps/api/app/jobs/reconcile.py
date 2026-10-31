"""Reconcile unresolved Scout admissions (D-25 execution controls, PROPOSED).

An admission (``scout_admissions``) stays unresolved until its admitting attempt records a
known end. One whose attempt is gone (a crash) or whose end is unknown (a timeout after the
request may have been sent) keeps its Scout from admitting anything (K = 1) until an operator
reconciles it here. Reconciliation is a declaration: the operator has ascertained, by means
outside SignalNest, that the external operation is over. It changes no accounting.

    python -m app.jobs.reconcile blocked [--organization <id>] [--workspace <id>]
    python -m app.jobs.reconcile list --scout <scout_request_id>
    python -m app.jobs.reconcile resolve --admission <id> --operator <name> \\
        --reason <code> [--note <text, at most 200 characters>] \\
        [--acknowledge-unrecorded-deadline]

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

**Blocked-Scout recovery (D-25-RECOVERY-CONTRACT-01, PROPOSED).**

* The deadline is the one **persisted on the admission** (``deadline_at`` and its basis), never
  recomputed from this command's settings, and compared strictly with the database clock.
* A row created before deadlines were persisted has none (``deadline_rule = 'unrecorded'``):
  listed as "not recorded"; with no recorded end it is never eligible by time, so ``resolve``
  requires ``--acknowledge-unrecorded-deadline`` (recorded in the audit row) and still refuses
  while its attempt holds valid authority.
* ``resolve`` refuses ANY unresolved admission while its admitting attempt still holds valid
  authority, a recorded unknown end included (contract §9.3 A2): a resolve can never free the
  slot for that same live attempt's retry.
* ``blocked`` lists every Scout in ``needs_attention`` (the admissions ``resolve`` could act on),
  optionally for one organization or workspace, with the exact ``resolve`` command.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

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
    """The admitting attempt still holds valid authority (shared with the derived execution
    state, ``app.scouting_requests.execution.attempt_holds_authority``)."""
    from app.jobs.models import Job
    from app.scouting_requests.execution import attempt_holds_authority

    job = db.execute(
        select(Job.status, Job.attempt_count, Job.worker_id, Job.lease_expires_at).where(
            Job.id == row.job_id
        )
    ).first()
    return attempt_holds_authority(job, row, _db_now(db))


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


#: How the listing shows a legacy admission's missing deadline (contract §3.4).
NOT_RECORDED = "not recorded"


def _deadline(row) -> datetime | None:
    """The admission's PERSISTED deadline (contract §3.2), or ``None`` for a legacy row."""
    return _aware(row.deadline_at)


def _basis(row) -> dict:
    if row.deadline_at is None:
        return {"rule": row.deadline_rule}
    return {
        "rule": row.deadline_rule,
        "client_seconds": row.deadline_client_seconds,
        "lease_seconds": row.deadline_lease_seconds,
    }


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
            "deadline": deadline.isoformat() if deadline is not None else NOT_RECORDED,
            "deadline_basis": _basis(row),
            "deadline_passed": (now > deadline) if deadline is not None else None,
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
    db: Session,
    *,
    admission_id: str,
    operator: str,
    reason: str,
    note: str | None = None,
    acknowledge_unrecorded_deadline: bool = False,
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
    if _attempt_valid(db, row):
        # Contract §9.3 A2 (PROPOSED): whatever its end kind, an admission whose attempt still
        # holds valid authority is not reconcilable (an unknown end included: resolving it
        # would free the K = 1 slot for that same live attempt's retry).
        raise ReconciliationRefused(
            "refused: the admitting attempt still holds valid authority"
            + ("" if row.end_kind == "unknown" else " and has recorded no end")
        )
    deadline = _deadline(row)
    unrecorded_no_end = row.ended_at is None and deadline is None
    if acknowledge_unrecorded_deadline and not unrecorded_no_end:
        raise ReconciliationRefused(
            "refused: --acknowledge-unrecorded-deadline applies only to an admission with no "
            "recorded end and no recorded deadline"
        )
    if unrecorded_no_end and not acknowledge_unrecorded_deadline:
        raise ReconciliationRefused(
            "refused: the admission has no recorded end and its deadline was not recorded "
            "(created before deadlines were persisted); time alone never makes it eligible. "
            "Pass --acknowledge-unrecorded-deadline once you have ascertained the operation is over"
        )
    if row.ended_at is None and deadline is not None and not _db_now(db) > deadline:
        raise ReconciliationRefused(
            "refused: the admission has no recorded end and its deadline "
            f"({deadline.isoformat()}) has not passed (database clock); "
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
        context={
            "deadline_rule": row.deadline_rule,
            "acknowledged_unrecorded_deadline": bool(acknowledge_unrecorded_deadline),
        },
    )
    db.flush()
    return True


def blocked(
    db: Session, *, organization_id: str | None = None, workspace_id: str | None = None
) -> dict:
    """Every Scout in ``needs_attention`` (recovery contract §4.3, PROPOSED): each unresolved
    admission ``resolve`` could act on (modulo the legacy acknowledgement), with its Scout, its
    tenancy, the operation, ``admitted_at``, the persisted deadline (or "not recorded") and
    its basis, whether it has passed by the database clock, the end kind (or "no recorded
    end"), the job's status and the exact ``resolve`` command. Read-only."""
    from app.scouting_requests.execution import eligible_admissions

    now = _db_now(db)
    items = []
    for row, request_status, job_status, owner in eligible_admissions(
        db, now=now, organization_id=organization_id, workspace_id=workspace_id
    ):
        deadline = _deadline(row)
        if row.end_kind == "unknown":
            category = UNKNOWN_END
        elif owner is not None and (row.job_id, row.attempt) == owner:
            category = CURRENT_OWNER
        else:
            category = EARLIER_OWNERS_OUTSTANDING
        command = (
            f"python -m app.jobs.reconcile resolve --admission {row.id} --operator <you> "
            f"--reason <{'|'.join(sorted(REASON_CODES))}> [--note <at most {NOTE_MAX} chars>]"
        )
        if row.ended_at is None and deadline is None:
            command += " --acknowledge-unrecorded-deadline"
        items.append(
            {
                "scout_request_id": row.scout_request_id,
                "organization_id": row.organization_id,
                "workspace_id": row.workspace_id,
                "request_status": request_status,
                "admission_id": row.id,
                "operation": row.operation,
                "category": category,
                "admitted_at": _aware(row.admitted_at).isoformat(),
                "deadline": deadline.isoformat() if deadline is not None else NOT_RECORDED,
                "deadline_basis": _basis(row),
                "deadline_passed": (now > deadline) if deadline is not None else None,
                "end_kind": row.end_kind or "no recorded end",
                "job_id": row.job_id,
                "job_status": job_status,
                "attempt_holds_authority": False,
                "resolve_command": command,
            }
        )
    return {"database_now": now.isoformat(), "blocked": items}


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI shell
    import app.db.models  # noqa: F401 - completes Base.metadata for this process (the audit row)
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(prog="python -m app.jobs.reconcile")
    sub = parser.add_subparsers(dest="command", required=True)
    blocker = sub.add_parser("blocked")
    blocker.add_argument("--organization")
    blocker.add_argument("--workspace")
    lister = sub.add_parser("list")
    lister.add_argument("--scout", required=True)
    resolver = sub.add_parser("resolve")
    resolver.add_argument("--admission", required=True)
    resolver.add_argument("--operator", required=True)
    resolver.add_argument("--reason", required=True, choices=sorted(REASON_CODES))
    resolver.add_argument("--note")
    resolver.add_argument("--acknowledge-unrecorded-deadline", action="store_true")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        if args.command == "blocked":
            print(
                json.dumps(
                    blocked(db, organization_id=args.organization, workspace_id=args.workspace),
                    indent=2,
                )
            )
            return 0
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
                acknowledge_unrecorded_deadline=args.acknowledge_unrecorded_deadline,
            )
        except ReconciliationRefused as exc:
            print(str(exc), file=sys.stderr)
            return 2
        db.commit()
        print("reconciled" if changed else "already resolved")
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
