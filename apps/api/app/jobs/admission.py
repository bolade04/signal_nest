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
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.core.enums import ScoutRequestStatus
from app.core.logging import get_logger

logger = get_logger("signalnest.jobs.admission")

SessionFactory = Callable[[], Session]

#: Run outcomes (``result_summary.outcome``) a cooperative stop can produce (D-25, PROPOSED).
#: The run was stopped at an admission because the request was paused (or paused and
#: resumed: its fence moved) after this run claimed it.
STOPPED_REQUEST_PAUSED = "stopped_request_paused"
#: The run was stopped because the request left ``running`` with its fence unchanged (a
#: rival run of the same fence settled it). Shares #267's literal.
STATUS_CHANGED_DURING_RUN = "status_changed_during_run"
#: The request row no longer exists (its organization was erased).
REQUEST_GONE = "request_gone"
#: The skip literal for a claim refused by a pause or a moved fence (#267's literal).
SKIPPED_REQUEST_PAUSED = "skipped_request_paused"

_RUNNING = ScoutRequestStatus.RUNNING.value
_PAUSED = ScoutRequestStatus.PAUSED.value
_COMPLETED = ScoutRequestStatus.COMPLETED.value

#: A deadlock or serialization failure is retried at most this many times in all.
_CONFLICT_ATTEMPTS = 3


class RunStopped(Exception):  # noqa: N818 - a control-flow signal, not an error class
    """An admission was refused: the run must stop at this boundary.

    Deliberately not an ``LLMError`` (nor any other class a pipeline handler catches), so
    it propagates out of the pipeline to the durable handler.
    """

    def __init__(self, outcome: str, operation: str):
        super().__init__(f"scout run stopped at {operation}: {outcome}")
        #: One of :data:`STOPPED_REQUEST_PAUSED`, :data:`STATUS_CHANGED_DURING_RUN`,
        #: :data:`REQUEST_GONE`.
        self.outcome = outcome
        #: Where the run stopped: ``fetch`` | ``post_fetch`` | ``model`` | ``commit``.
        self.operation = operation


def _refusal(row: Any, fence: int) -> str | None:
    """Why ``row`` (``status, run_fence`` or ``None``) refuses fence ``fence``; ``None`` admits."""
    if row is None:
        return REQUEST_GONE
    status, current = row
    if status == _PAUSED or current != fence:
        return STOPPED_REQUEST_PAUSED
    if status != _RUNNING:
        return STATUS_CHANGED_DURING_RUN
    return None


@dataclass
class RunAdmission:
    """The fence, claim and admissions of ONE attempt of ONE ``scout_request.execute`` job.

    The tenant predicates come from the durable job row's context (never the payload), so
    a mis-enqueued id can neither claim nor admit another tenant's request.
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
    #: Set by :meth:`claim`.
    claimed: bool = False
    pre_claim_status: str | None = None
    #: Admissions granted, per operation kind (observability and tests).
    admitted: dict[str, int] = field(default_factory=dict)
    #: The savepoint of the run's write phase (see :meth:`begin_writes`).
    _savepoint: Any = field(default=None, repr=False)

    # -- statements -------------------------------------------------------------------
    def _model(self):
        from app.scouting_requests.models import ScoutRequest

        return ScoutRequest

    def _row(self):
        model = self._model()
        return select(model.status, model.run_fence).where(
            model.id == self.scout_request_id,
            model.organization_id == self.organization_id,
            model.workspace_id == self.workspace_id,
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

    # -- the claim --------------------------------------------------------------------
    def claim(self, *, now: datetime) -> str | None:
        """Write ``running`` for this run in a committed transaction of its own.

        Returns ``None`` when claimed, else the skip outcome: :data:`SKIPPED_REQUEST_PAUSED`
        (the request is paused, or its fence moved since this job was enqueued) or
        :data:`REQUEST_GONE`. The row is read ``FOR NO KEY UPDATE`` first (a no-op on
        SQLite), and the UPDATE repeats every condition, so the claim is a compare-and-set
        either way.
        """
        model = self._model()

        def work(session: Session) -> str | None:
            row = session.execute(self._row().with_for_update(key_share=True)).first()
            if row is None:
                return REQUEST_GONE
            status, current = row
            if status == _PAUSED or current != self.fence:
                return SKIPPED_REQUEST_PAUSED
            result = session.execute(
                update(model)
                .where(
                    model.id == self.scout_request_id,
                    model.organization_id == self.organization_id,
                    model.workspace_id == self.workspace_id,
                    model.status != _PAUSED,
                    model.run_fence == self.fence,
                )
                .values(status=_RUNNING, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return SKIPPED_REQUEST_PAUSED
            self.pre_claim_status = status
            return None

        outcome = self._in_own_transaction(work)
        self.claimed = outcome is None
        return outcome

    # -- admissions -------------------------------------------------------------------
    def check(self, session: Session, operation: str) -> None:
        """The admission read, in ``session``'s transaction: ``FOR SHARE`` on PostgreSQL.

        Raises :class:`RunStopped` unless the request is ``running`` with this run's fence.
        """
        row = session.execute(self._row().with_for_update(read=True)).first()
        refused = _refusal(row, self.fence)
        if refused is not None:
            raise RunStopped(refused, operation)

    def granted(self, operation: str) -> None:
        self.admitted[operation] = self.admitted.get(operation, 0) + 1

    def admit(self, operation: str) -> None:
        """An admission in a short committed transaction of its own."""
        self._in_own_transaction(lambda session: self.check(session, operation))
        self.granted(operation)

    # -- the run's write phase --------------------------------------------------------
    def begin_writes(self, db: Session) -> None:
        """Open the savepoint that holds every domain write of this run.

        Called by the pipeline where its writes begin (after the fetch, the takedown
        re-check and the bound check; nothing before writes). Opening it any earlier would
        start a SQLite transaction whose reads hold the database's shared lock, so the
        committed claim and a pause during the fetch could not commit on SQLite.

        SQLite: until its first write the run has only read, so pysqlite has not begun a
        transaction, and a SAVEPOINT would itself start one. SQLite then treats RELEASE of
        that outermost savepoint as COMMIT, which would commit the run's writes before the
        worker's fenced completion (a lease lost at completion could no longer roll them
        back). The transaction is therefore begun explicitly first; the savepoint is then
        nested in it, as it always is on PostgreSQL.
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
        """The guarded terminal write, in the run's own transaction.

        ``completed``, ``stats`` and ``last_run_at`` in ONE statement, only while the request
        is still ``running`` with this run's fence. Anything else is a stop at the commit
        boundary (:class:`RunStopped`, operation ``commit``): the run's findings are then
        discarded with its savepoint, so they become visible only if this write commits
        before a pause (§6.2 Q-1, PROPOSED: discarding the stopped run's uncommitted
        findings is a candidate choice, not a ruling).
        """
        model = self._model()
        result = db.execute(
            update(model)
            .where(
                model.id == self.scout_request_id,
                model.organization_id == self.organization_id,
                model.workspace_id == self.workspace_id,
                model.status == _RUNNING,
                model.run_fence == self.fence,
            )
            .values(status=_COMPLETED, stats=stats, last_run_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            refused = _refusal(db.execute(self._row()).first(), self.fence)
            raise RunStopped(refused or STATUS_CHANGED_DURING_RUN, "commit")
        self.granted("commit")

    # -- after a failed attempt -------------------------------------------------------
    def restore_writer(self, *, now: datetime) -> Callable[[Session], None] | None:
        """An after-rollback writer that puts back the pre-claim status (§6.2 Q-2 (B),
        PROPOSED).

        A failed attempt's rollback no longer undoes the claim (it was committed on its
        own), so the request would show ``running`` while the job waits to retry. The
        writer restores the status the claim replaced, only while the request is still
        ``running`` with this run's fence **and** this attempt still owns the job (same
        worker, same attempt, still running): a lease that was lost and reclaimed meanwhile
        makes it a no-op, so it can never undo a newer attempt's claim; and so is any other
        active execute job of the same request (a same-fence rival owning ``running``). It
        therefore never overwrites a newer pause, a resume (``draft``), a newer claim (a new
        fence or another job) or another run's terminal result. Returns ``None`` when
        nothing was claimed or there is nothing to restore.
        """
        if not self.claimed or self.pre_claim_status in (None, _RUNNING):
            return None
        model = self._model()
        pre = self.pre_claim_status

        def write(db: Session) -> None:
            from app.jobs.models import Job
            from app.jobs.status import ACTIVE_STATUSES, JobStatus, JobType

            # Run identity and lease ownership: this job, this worker, this attempt.
            owned = exists().where(
                Job.id == self.job_id,
                Job.worker_id == self.worker_id,
                Job.attempt_count == self.attempt,
                Job.status.in_([JobStatus.RUNNING.value, JobStatus.CANCEL_REQUESTED.value]),
            )
            # A rival execute job of the same request (same fence, e.g. a manual run that
            # raced a tick) may own the ``running`` now: never undo its claim.
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
                    owned,
                    ~rival,
                )
                .values(status=pre, updated_at=now)
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
    """Admit one operation of the current run; a no-op outside :func:`admitted_run`."""
    admission = _current.get()
    if admission is not None:
        admission.admit(operation)


__all__ = [
    "REQUEST_GONE",
    "SKIPPED_REQUEST_PAUSED",
    "STATUS_CHANGED_DURING_RUN",
    "STOPPED_REQUEST_PAUSED",
    "RunAdmission",
    "RunStopped",
    "admit_operation",
    "admitted_run",
    "current_admission",
]
