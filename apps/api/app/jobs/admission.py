"""Cooperative pause of a scout run: the run fence, the committed claim and admissions.

D-25 candidate (PROPOSED, pending the operator's ruling; contract
``D-25-COOPERATIVE-PAUSE-CONTRACT-01`` §6). Nothing here is accepted product behaviour.

* **Run fence.** ``scout_requests.run_fence`` is a durable per-request counter. Every pause
  advances it (``routes.pause_scout_request``); every ``scout_request.execute`` job carries
  the value it was enqueued under (``payload.run_fence``; a legacy job without the key is
  fence 0). A run may continue only while the request is ``running`` **and** its fence is
  still the job's, so a run queued, claimed, retried or recovered before a pause can never
  continue after it, even once the request has been resumed.
* **Claim.** :meth:`RunAdmission.claim` writes ``running`` in a short transaction of its
  own (a session from the attempt's bind, the seam the LLM ledger already uses) and
  commits it before any fetch. The run's long transaction therefore holds no update lock
  on the request row while it fetches or calls a model: only the ``FOR KEY SHARE`` its
  foreign-key inserts take, which conflicts neither with the pause's UPDATE nor with an
  admission (measured on PostgreSQL 16, lane W13-PAUSECOOP raw/010).
* **Admission.** :meth:`RunAdmission.check` reads the row ``FOR SHARE``: that lock
  conflicts with the pause's UPDATE, so every admission is ordered strictly before the
  pause's commit (admitted: in flight) or after it (refused). ``FOR KEY SHARE`` would not
  conflict and must not be used. An admission is made before every live fetch attempt,
  before every model attempt (inside the reservation's transaction when the attempt is
  priced on the shared ledger), once after the fetch, and at the commit boundary (the
  guarded terminal UPDATE, :meth:`RunAdmission.finish`).
* **Stop.** A refused admission raises :class:`RunStopped`, deliberately *not* an
  ``LLMError``, so no degradation handler absorbs it. The durable handler rolls back the
  run's savepoint (the stopped run's own writes are discarded; accounting committed in
  the ledger's own transactions stays) and the job ends ``succeeded`` with
  ``result_summary.outcome`` :data:`STOPPED_REQUEST_PAUSED` (or
  :data:`STATUS_CHANGED_DURING_RUN` / :data:`REQUEST_GONE`).

What this does **not** do: it cannot recall an operation already admitted. After the
pause commits, nothing new is admitted, but at most one already-admitted operation per run
(one model attempt, or one fetch attempt with its redirect chain) can still complete, and
its charge stands. On SQLite a pause during a run's write phase still waits on the
database write lock (``busy_timeout``); the prompt pause is a PostgreSQL property.

The opt-in is :func:`admitted_run`, a context variable the production handler sets. Code
that calls ``pipeline._run`` without it (the replay demo seed, direct test calls) keeps
the in-session claim and makes no admissions.

**Execution controls (D-25-EXECUTION-CONTROL-CONTRACT-01, PROPOSED).** On top of the above:

* **O1.** Every admission, terminal write and status restore checks the attempt's identity as
  captured at its claim (``job_id``, ``attempt``, ``worker_id``, ``lease_token``, fence) against
  fresh job and request rows read ``FOR SHARE`` in the admitting transaction, with the lease
  still running by database time. An attempt never adopts a re-read token, attempt or owner.
  A missing identity fails closed (refused, never admitted).
* **O2.** The Scout records ONE execution owner (``scout_requests.owner_*``), set by the
  committed claim's compare-and-set, taken over only once the recorded owner's authority has
  lapsed by fresh observation, cleared by the pause, the run's end, its stop or failure.
* **K = 1.** Every external operation (a connector fetch attempt, a model attempt) commits a
  ``scout_admissions`` row first; the partial unique index allows one unresolved row per Scout.
  A row is resolved only by a known recorded end (written by the admitting attempt, guarded by
  the row's own identity) or by reconciliation (``app.jobs.reconcile``) - never by lease
  expiry, recovery, Cancel, Pause -> Resume, a new owner or elapsed time. An admission refused
  by an outstanding row waits (no lock held) up to that row's own deadline; an unknown end is
  never waited for. Lock order: job -> request -> ledger -> period -> reservation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, bindparam, case, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import ScoutRequestStatus
from app.core.logging import get_logger

logger = get_logger("signalnest.jobs.admission")

SessionFactory = Callable[[], Session]

#: Run outcomes (``result_summary.outcome``) a cooperative stop can produce (D-25, PROPOSED).
STOPPED_REQUEST_PAUSED = "stopped_request_paused"
STATUS_CHANGED_DURING_RUN = "status_changed_during_run"
REQUEST_GONE = "request_gone"
SKIPPED_REQUEST_PAUSED = "skipped_request_paused"
#: Execution controls (PROPOSED): another attempt owns the Scout (claim refused / owner
#: superseded), or this attempt lost its job lease (token rotated, job no longer running).
SKIPPED_OWNER_ACTIVE = "skipped_owner_active"
STOPPED_OWNER_SUPERSEDED = "stopped_owner_superseded"
STOPPED_LEASE_LOST = "stopped_lease_lost"

#: Causes of a refused admission (RunStopped.cause); the handler maps each to an end state.
CAUSE_REQUEST_PAUSED = "request_paused"
CAUSE_STATUS_CHANGED = "status_changed"
CAUSE_REQUEST_GONE = "request_gone"
CAUSE_OWNER_SUPERSEDED = "owner_superseded"
CAUSE_CANCEL_REQUESTED = "cancel_requested"
CAUSE_LEASE_LAPSED = "lease_lapsed"
CAUSE_LEASE_LOST = "lease_lost"
CAUSE_IDENTITY_INCOMPLETE = "identity_incomplete"

_OUTCOME = {
    CAUSE_REQUEST_PAUSED: STOPPED_REQUEST_PAUSED,
    CAUSE_STATUS_CHANGED: STATUS_CHANGED_DURING_RUN,
    CAUSE_REQUEST_GONE: REQUEST_GONE,
    CAUSE_OWNER_SUPERSEDED: STOPPED_OWNER_SUPERSEDED,
    CAUSE_LEASE_LOST: STOPPED_LEASE_LOST,
}

_RUNNING = ScoutRequestStatus.RUNNING.value
_PAUSED = ScoutRequestStatus.PAUSED.value
_COMPLETED = ScoutRequestStatus.COMPLETED.value
_LIVE_JOB = ("claimed", "running")

#: A deadlock or serialization failure is retried at most this many times in all.
_CONFLICT_ATTEMPTS = 3
#: The unique index that enforces K = 1.
_K_INDEX = "uq_scout_admission_unresolved"
#: A K = 1 refusal whose blocking admission is already resolved when read is retried at once,
#: at most this many times in a row (more would mean the index refuses with nothing unresolved).
_RESOLVED_MEANWHILE_RETRIES = 3


class RunStopped(Exception):  # noqa: N818 - a control-flow signal, not an error class
    """An admission was refused: the run must stop at this boundary.

    Deliberately not an ``LLMError`` (nor any other class a pipeline handler catches), so
    it propagates out of the pipeline to the durable handler.
    """

    def __init__(self, outcome: str, operation: str, cause: str | None = None):
        super().__init__(f"scout run stopped at {operation}: {cause or outcome}")
        self.outcome = outcome
        self.operation = operation
        #: Why authority was refused (``CAUSE_*``); the handler maps it to an end state.
        self.cause = cause or outcome


class AdmissionUnresolved(Exception):  # noqa: N818 - a definite end, not a crash
    """An external operation could not be admitted: the Scout's one allowed unresolved
    admission (K = 1) stayed unresolved past its deadline, or ended with an unknown outcome.
    The waiting never resolves it (PROPOSED)."""

    def __init__(self, blocking_id: str, operation: str):
        super().__init__(f"admission refused at {operation}: {blocking_id} unresolved")
        self.blocking_id = blocking_id
        self.operation = operation


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def is_k_violation(exc: BaseException) -> bool:
    text_ = str(getattr(exc, "orig", exc))
    return isinstance(exc, IntegrityError) and (
        _K_INDEX in text_ or "scout_admissions.scout_request_id" in text_
    )


@dataclass
class RunAdmission:
    """The fence, ownership, claim and admissions of ONE attempt of ONE execute job.

    The tenant predicates and the identity come from the durable job row's context and the
    worker's claim (never the payload or a re-read), so a mis-enqueued id can neither claim
    nor admit another tenant's request, and a stale attempt cannot borrow a newer identity.
    """

    scout_request_id: str
    organization_id: str
    workspace_id: str
    #: The fence the job was enqueued under (``payload.run_fence``; legacy jobs: 0).
    fence: int
    session_factory: SessionFactory
    job_id: str | None = None
    worker_id: str | None = None
    attempt: int = 0
    #: The lease token captured at the claim (O1); ``None`` fails closed.
    lease_token: str | None = None
    #: Set by :meth:`claim`.
    claimed: bool = False
    pre_claim_status: str | None = None
    #: Admissions granted, per operation kind (observability and tests).
    admitted: dict[str, int] = field(default_factory=dict)
    #: The savepoint of the run's write phase (see :meth:`begin_writes`).
    _savepoint: Any = field(default=None, repr=False)
    _dialect: str | None = field(default=None, repr=False)
    #: The attempt's execution-boundary time from the worker's clock: the lease reference
    #: on SQLite (development and tests); PostgreSQL always uses ``clock_timestamp()``.
    process_now: datetime | None = None

    # -- statements -------------------------------------------------------------------
    def _model(self):
        from app.scouting_requests.models import ScoutRequest

        return ScoutRequest

    def _row(self):
        model = self._model()
        return select(
            model.status,
            model.run_fence,
            model.owner_job_id,
            model.owner_attempt,
            model.owner_lease_token,
        ).where(
            model.id == self.scout_request_id,
            model.organization_id == self.organization_id,
            model.workspace_id == self.workspace_id,
        )

    def _now(self, session: Session):
        """Database time on PostgreSQL (``clock_timestamp()``, contract §8.1.6); on SQLite the
        attempt's worker clock (a pinned test clock stays consistent with its leases)."""
        if session.get_bind().dialect.name == "postgresql":
            return func.clock_timestamp()
        return bindparam(None, self.process_now or datetime.now(UTC), type_=DateTime(timezone=True))

    def identity_complete(self) -> bool:
        return bool(self.job_id and self.worker_id and self.lease_token and self.attempt >= 1)

    def _is_me(self, model):
        return and_(
            model.owner_job_id == self.job_id,
            model.owner_attempt == self.attempt,
            model.owner_lease_token == self.lease_token,
        )

    def _job_authority(self, session: Session):
        """``EXISTS``: this attempt still holds its job (O1), by fresh observation."""
        from app.jobs.models import Job

        return exists().where(
            Job.id == self.job_id,
            Job.status.in_(_LIVE_JOB),
            Job.worker_id == self.worker_id,
            Job.attempt_count == self.attempt,
            Job.lease_token == self.lease_token,
            Job.lease_expires_at > self._now(session),
        )

    def _owner_valid(self, session: Session, model):
        """``EXISTS``: the recorded owner (whoever it is) still holds valid authority."""
        from app.jobs.models import Job

        return exists().where(
            Job.id == model.owner_job_id,
            Job.status.in_(_LIVE_JOB),
            Job.attempt_count == model.owner_attempt,
            Job.lease_token == model.owner_lease_token,
            Job.lease_expires_at > self._now(session),
        )

    def _in_own_transaction(self, work: Callable[[Session], Any]) -> Any:
        """Run ``work`` on a session of its own and commit it; roll back on any error.

        A deadlock or serialization failure (SQLSTATE 40P01 / 40001) is retried on a fresh
        session, at most :data:`_CONFLICT_ATTEMPTS` times in all; anything else re-raises.
        """
        from app.llm.budget import is_retryable_conflict

        for attempt in range(1, _CONFLICT_ATTEMPTS + 1):
            session = self.session_factory()
            try:
                result = work(session)
                session.commit()
                return result
            except BaseException as exc:
                session.rollback()
                if attempt == _CONFLICT_ATTEMPTS or not is_retryable_conflict(exc):
                    raise
                time.sleep(0.05 * attempt)
            finally:
                session.close()
        raise AssertionError("unreachable")  # pragma: no cover

    # -- authority (O1, O2) -----------------------------------------------------------
    def _cause(self, job: Any, row: Any) -> str | None:
        """Why this attempt holds no valid authority, from fresh job and request rows."""
        if row is None:
            return CAUSE_REQUEST_GONE
        status, current, owner_job, owner_attempt, owner_token = row
        if status == _PAUSED or current != self.fence:
            return CAUSE_REQUEST_PAUSED
        if job is None:
            return CAUSE_LEASE_LOST
        job_status, worker_id, attempt, token, lease_ok = job
        if job_status == "cancel_requested":
            return CAUSE_CANCEL_REQUESTED
        if (
            job_status not in _LIVE_JOB
            or worker_id != self.worker_id
            or attempt != self.attempt
            or token != self.lease_token
        ):
            return CAUSE_LEASE_LOST
        if not lease_ok:
            return CAUSE_LEASE_LAPSED
        if (owner_job, owner_attempt, owner_token) != (self.job_id, self.attempt, self.lease_token):
            return CAUSE_OWNER_SUPERSEDED
        if status != _RUNNING:
            return CAUSE_STATUS_CHANGED
        return None

    def _read_job(self, session: Session, *, lock: bool):
        from app.jobs.models import Job

        stmt = select(
            Job.status,
            Job.worker_id,
            Job.attempt_count,
            Job.lease_token,
            Job.lease_expires_at > self._now(session),
        ).where(Job.id == self.job_id)
        return session.execute(stmt.with_for_update(read=True) if lock else stmt).first()

    def check(self, session: Session, operation: str) -> None:
        """The admission read, in ``session``'s transaction: job row, then request row, both
        ``FOR SHARE`` on PostgreSQL. Raises :class:`RunStopped` unless the attempt's claimed
        identity is still current, the request is ``running`` with this fence and this
        attempt is its owner."""
        if not self.identity_complete():
            raise RunStopped(CAUSE_IDENTITY_INCOMPLETE, operation, CAUSE_IDENTITY_INCOMPLETE)
        job = self._read_job(session, lock=True)
        row = session.execute(self._row().with_for_update(read=True)).first()
        cause = self._cause(job, row)
        if cause is not None:
            raise RunStopped(_OUTCOME.get(cause, cause), operation, cause)

    def granted(self, operation: str) -> None:
        self.admitted[operation] = self.admitted.get(operation, 0) + 1

    def admit(self, operation: str) -> None:
        """An authority-only admission (the post-fetch boundary) in a short transaction."""
        self._in_own_transaction(lambda session: self.check(session, operation))
        self.granted(operation)

    # -- the claim (O1 + O2 owner compare-and-set) -------------------------------------
    def claim(self, *, now: datetime) -> str | None:
        """Write ``running`` and this attempt as the Scout's owner, in a committed
        transaction of its own. Returns ``None`` when claimed, else the skip outcome
        (:data:`SKIPPED_REQUEST_PAUSED`, :data:`REQUEST_GONE`, :data:`SKIPPED_OWNER_ACTIVE`).
        A missing identity raises :class:`RunStopped` (fails closed)."""
        if not self.identity_complete():
            raise RunStopped(CAUSE_IDENTITY_INCOMPLETE, "claim", CAUSE_IDENTITY_INCOMPLETE)
        model = self._model()

        def work(session: Session) -> str | None:
            job = self._read_job(session, lock=True)
            row = session.execute(self._row().with_for_update(key_share=True)).first()
            if row is None:
                return REQUEST_GONE
            status, current = row[0], row[1]
            if status == _PAUSED or current != self.fence:
                return SKIPPED_REQUEST_PAUSED
            if (
                job is None
                or job[0] not in _LIVE_JOB
                or (job[1], job[2], job[3]) != (self.worker_id, self.attempt, self.lease_token)
                or not job[4]
            ):
                raise RunStopped(CAUSE_LEASE_LOST, "claim", CAUSE_LEASE_LOST)
            result = session.execute(
                update(model)
                .where(
                    model.id == self.scout_request_id,
                    model.organization_id == self.organization_id,
                    model.workspace_id == self.workspace_id,
                    model.status != _PAUSED,
                    model.run_fence == self.fence,
                    or_(
                        model.owner_job_id.is_(None),
                        self._is_me(model),
                        ~self._owner_valid(session, model),
                    ),
                )
                .values(
                    status=_RUNNING,
                    owner_job_id=self.job_id,
                    owner_attempt=self.attempt,
                    owner_lease_token=self.lease_token,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return SKIPPED_OWNER_ACTIVE
            self.pre_claim_status = status
            return None

        outcome = self._in_own_transaction(work)
        self.claimed = outcome is None
        return outcome

    # -- external operations (K = 1) ---------------------------------------------------
    def _deadline_seconds(self, operation: str) -> float:
        from app.core.config import get_settings
        from app.llm.budget import HTTP_TIMEOUT_PHASES

        settings = get_settings()
        if operation == "model":
            own = HTTP_TIMEOUT_PHASES * float(settings.llm_timeout_seconds)
        else:
            own = float(settings.connector_egress_total_timeout_seconds)
        return own + float(settings.worker_lease_seconds)

    def admit_external(
        self,
        operation: str,
        *,
        attempt_key: str | None = None,
        extra: Callable[[Session], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> tuple[str | None, Any]:
        """Admit ONE external operation: authority, the ledger row and ``extra`` (the LLM
        reservation, when priced) commit together. Returns ``(admission_id, extra_result)``.

        Refused by the Scout's unresolved admission (K = 1): wait, holding no lock, while
        that admission is outstanding and before its own deadline (database time),
        re-checking this attempt's authority on every try; raise :class:`AdmissionUnresolved`
        once it has an unknown end or its deadline has passed. The wait never resolves it.
        """
        from app.scouting_requests.models import ScoutAdmission

        if not self._ledger_enabled():
            # SQLite (development and tests): the run's own connection holds the database
            # write lock during its write phase, so a separate-connection ledger insert cannot
            # commit. The authority check (a read) stays; the ledger and K = 1 are
            # PostgreSQL-only, like the shared LLM ledger.
            result = self._in_own_transaction(
                lambda session: (
                    self.check(session, operation),
                    extra(session) if extra is not None else None,
                )[1]
            )
            self.granted(operation)
            return None, result

        def work(session: Session) -> tuple[str, Any]:
            self.check(session, operation)
            row = ScoutAdmission(
                scout_request_id=self.scout_request_id,
                organization_id=self.organization_id,
                workspace_id=self.workspace_id,
                job_id=self.job_id,
                attempt=self.attempt,
                worker_id=self.worker_id,
                operation=operation,
                attempt_key=attempt_key,
                admitted_at=self._now(session),  # database time: the deadline's origin
            )
            session.add(row)
            session.flush()
            return row.id, (extra(session) if extra is not None else None)

        delay = 0.25
        resolved_meanwhile = 0
        while True:
            try:
                admission_id, result = self._in_own_transaction(work)
            except IntegrityError as exc:
                if not is_k_violation(exc):
                    raise
                blocking = self._blocking(operation)
                if blocking is None:
                    resolved_meanwhile += 1
                    if resolved_meanwhile > _RESOLVED_MEANWHILE_RETRIES:
                        raise
                    continue  # resolved meanwhile: try again at once
                blocking_id, ended, expired = blocking
                if ended or expired:
                    raise AdmissionUnresolved(blocking_id, operation) from None
                sleep(delay)
                delay = min(delay * 2, 2.0)
                continue
            self.granted(operation)
            return admission_id, result

    def _ledger_enabled(self) -> bool:
        if self._dialect is None:
            session = self.session_factory()
            try:
                self._dialect = session.get_bind().dialect.name
            finally:
                session.close()
        return self._dialect == "postgresql"

    def _blocking(self, operation: str) -> tuple[str, bool, bool] | None:
        """The Scout's unresolved admission: ``(id, has_ended, deadline_passed)``."""
        from app.scouting_requests.models import ScoutAdmission

        def read(session: Session):
            row = session.execute(
                select(
                    ScoutAdmission.id,
                    ScoutAdmission.ended_at,
                    ScoutAdmission.operation,
                    ScoutAdmission.admitted_at,
                ).where(
                    ScoutAdmission.scout_request_id == self.scout_request_id,
                    ScoutAdmission.resolved_at.is_(None),
                )
            ).first()
            if row is None:
                return None
            now = session.execute(select(self._now(session))).scalar_one()
            deadline = _aware(row[3]) + timedelta(seconds=self._deadline_seconds(row[2]))
            return row[0], row[1] is not None, _aware(now) > deadline

        return self._in_own_transaction(read)

    def end_statement(self, admission_id: str, *, known: bool):
        """The recorded end of one admission (guarded by the row's own identity only, so a
        stale attempt can still record its known end; a reconciled row keeps its
        resolution; an erased row matches nothing)."""
        from app.scouting_requests.models import ScoutAdmission

        stamp = datetime.now(UTC)
        return (
            update(ScoutAdmission)
            .where(
                ScoutAdmission.id == admission_id,
                ScoutAdmission.job_id == self.job_id,
                ScoutAdmission.attempt == self.attempt,
                ScoutAdmission.ended_at.is_(None),
            )
            .values(
                ended_at=stamp,
                end_kind="known" if known else "unknown",
                resolved_at=case(
                    (ScoutAdmission.resolved_at.is_not(None), ScoutAdmission.resolved_at),
                    else_=stamp if known else None,
                ),
                updated_at=stamp,
            )
            .execution_options(synchronize_session=False)
        )

    def record_end(self, admission_id: str | None, *, known: bool) -> None:
        if admission_id is None:
            return
        self._in_own_transaction(
            lambda session: session.execute(self.end_statement(admission_id, known=known))
        )

    # -- the run's write phase --------------------------------------------------------
    def begin_writes(self, db: Session) -> None:
        """Open the savepoint that holds every domain write of this run.

        Called by the pipeline where its writes begin (after the fetch, the takedown
        re-check and the bound check; nothing before writes). On SQLite the DBAPI
        transaction is begun explicitly first: a SAVEPOINT that starts a transaction is
        committed by its RELEASE, which would commit the run's writes before the worker's
        fenced completion.
        """
        connection = db.connection()
        if connection.dialect.name == "sqlite":
            raw = connection.connection.dbapi_connection
            if raw is not None and not raw.in_transaction:
                connection.exec_driver_sql("BEGIN")
        self._savepoint = db.begin_nested()

    def discard_writes(self) -> None:
        """A stop: roll the run's own writes back (its findings are discarded)."""
        savepoint, self._savepoint = self._savepoint, None
        if savepoint is not None and savepoint.is_active:
            savepoint.rollback()

    def keep_writes(self) -> None:
        """The run completed: release the savepoint into the attempt's transaction."""
        savepoint, self._savepoint = self._savepoint, None
        if savepoint is not None and savepoint.is_active:
            savepoint.commit()

    # -- the commit boundary ----------------------------------------------------------
    def finish(self, db: Session, *, stats: dict, now: datetime) -> None:
        """The guarded terminal write, in the run's own transaction: ``completed``, stats and
        ``last_run_at`` in ONE statement, only while the request is ``running`` with this
        fence, this attempt is its owner and still holds its job (O1); the owner is cleared.
        Anything else is a stop at the commit boundary (findings discarded with the
        savepoint, PROPOSED §6.2 Q-1)."""
        model = self._model()
        result = db.execute(
            update(model)
            .where(
                model.id == self.scout_request_id,
                model.organization_id == self.organization_id,
                model.workspace_id == self.workspace_id,
                model.status == _RUNNING,
                model.run_fence == self.fence,
                self._is_me(model),
                self._job_authority(db),
            )
            .values(
                status=_COMPLETED,
                stats=stats,
                last_run_at=now,
                updated_at=now,
                owner_job_id=None,
                owner_attempt=None,
                owner_lease_token=None,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            job = self._read_job(db, lock=False)
            row = db.execute(self._row()).first()
            cause = self._cause(job, row) or CAUSE_STATUS_CHANGED
            raise RunStopped(_OUTCOME.get(cause, cause), "commit", cause)
        self.granted("commit")

    # -- owner release and status restore ---------------------------------------------
    def holds_job(self):
        """``EXISTS``: the job row still carries this attempt's claim (this worker, attempt
        and lease token) in an active status (contract §9.1: a request write that can commit
        outside the worker's token-fenced job transaction carries the full identity). No
        lease-time test: a lapsed but not recovered lease still releases its own owner."""
        from app.jobs.models import Job

        return exists().where(
            Job.id == self.job_id,
            Job.worker_id == self.worker_id,
            Job.attempt_count == self.attempt,
            Job.lease_token == self.lease_token,
            Job.status.in_((*_LIVE_JOB, "cancel_requested")),
        )

    def release_owner_statement(self):
        """Clear the owner, only while it is still this attempt and this attempt still
        holds its job (a stale attempt never writes the request, §9.1)."""
        model = self._model()
        return (
            update(model)
            .where(model.id == self.scout_request_id, self._is_me(model), self.holds_job())
            .values(owner_job_id=None, owner_attempt=None, owner_lease_token=None)
            .execution_options(synchronize_session=False)
        )

    def release_owner_writer(self) -> Callable[[Session], None] | None:
        if not self.claimed:
            return None
        return lambda db: db.execute(self.release_owner_statement())

    def restore_writer(self, *, now: datetime) -> Callable[[Session], None] | None:
        """An after-rollback writer that puts back the pre-claim status (§6.2 Q-2 (B),
        PROPOSED), and clears the owner.

        Conditional on run identity (job, worker, attempt, lease token), the lease still
        running, the fence, this attempt being the owner, and no rival active execute job. It
        therefore never overwrites a newer pause, a resume, a newer claim or owner, or another
        run's terminal result. Returns ``None`` when nothing was claimed or nothing to restore.
        """
        if not self.claimed or self.pre_claim_status in (None, _RUNNING):
            return None
        model = self._model()
        pre = self.pre_claim_status

        def write(db: Session) -> None:
            from app.jobs.models import Job
            from app.jobs.status import ACTIVE_STATUSES, JobStatus, JobType

            owned = exists().where(
                Job.id == self.job_id,
                Job.worker_id == self.worker_id,
                Job.attempt_count == self.attempt,
                Job.lease_token == self.lease_token,
                Job.lease_expires_at > self._now(db),
                Job.status.in_([JobStatus.RUNNING.value, JobStatus.CANCEL_REQUESTED.value]),
            )
            rival = exists().where(
                Job.scout_request_id == self.scout_request_id,
                Job.id != self.job_id,
                Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value,
                Job.status.in_([status.value for status in ACTIVE_STATUSES]),
            )
            db.execute(
                update(model)
                .where(
                    model.id == self.scout_request_id,
                    model.organization_id == self.organization_id,
                    model.workspace_id == self.workspace_id,
                    model.status == _RUNNING,
                    model.run_fence == self.fence,
                    self._is_me(model),
                    owned,
                    ~rival,
                )
                .values(
                    status=pre,
                    updated_at=now,
                    owner_job_id=None,
                    owner_attempt=None,
                    owner_lease_token=None,
                )
                .execution_options(synchronize_session=False)
            )

        return write


#: The admission of the run the durable handler is executing (see :func:`admitted_run`).
_current: ContextVar[RunAdmission | None] = ContextVar("scout_run_admission", default=None)


@contextmanager
def admitted_run(admission: RunAdmission) -> Iterator[RunAdmission]:
    """Make ``admission`` govern the ``pipeline._run`` called inside this block."""
    token = _current.set(admission)
    try:
        yield admission
    finally:
        _current.reset(token)


def current_admission() -> RunAdmission | None:
    return _current.get()


def admit_operation(operation: str) -> None:
    """An authority-only admission of the current run; a no-op outside :func:`admitted_run`."""
    admission = _current.get()
    if admission is not None:
        admission.admit(operation)


def admit_external_operation(operation: str) -> str | None:
    """Admit ONE external operation of the current run (ledger row, K = 1); returns the
    admission id to record its end with, or ``None`` outside :func:`admitted_run`."""
    admission = _current.get()
    if admission is None:
        return None
    admission_id, _ = admission.admit_external(operation)
    return admission_id


def record_external_end(admission_id: str | None, *, known: bool) -> None:
    """Record the end of an admission made by :func:`admit_external_operation`."""
    admission = _current.get()
    if admission is not None and admission_id is not None:
        admission.record_end(admission_id, known=known)


__all__ = [
    "REQUEST_GONE",
    "SKIPPED_OWNER_ACTIVE",
    "SKIPPED_REQUEST_PAUSED",
    "STATUS_CHANGED_DURING_RUN",
    "STOPPED_LEASE_LOST",
    "STOPPED_OWNER_SUPERSEDED",
    "STOPPED_REQUEST_PAUSED",
    "AdmissionUnresolved",
    "RunAdmission",
    "RunStopped",
    "admit_external_operation",
    "admit_operation",
    "admitted_run",
    "current_admission",
    "record_external_end",
]
