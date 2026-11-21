"""W21 residuals O-F123-1, O-F123-2 and O-F123-3, through the real paths, on both engines.

The operator authorized them on 2026-11-21 (register §53):

* **O-F123-1.** "protect the generation requester's current membership and authorization at the
  final draft-write boundary. Demonstrate the repair on SQLite and preserve PostgreSQL's
  protections."
* **O-F123-2.** "prevent an admitted model call's usage from disappearing during SQLite contention.
  Preserve exactly-once accounting through existing mechanisms, or refuse safely before sending the
  call. If this requires a migration, new durable subsystem or accounting-policy change, present
  that concrete design before expanding implementation."
* **O-F123-3.** "demonstrate automatic queue recovery after the competing writer releases its lock.
  Repair only if recovery is actually defective."
* For all three: "Use real application paths, both transaction orderings and both engines.
  Preserve legitimate earlier writes, historical records, supported configurations and existing
  accounting policy."

**The paths.** The generate route and the ``creative.generate`` job run by the real worker
(``JobRunner.poll_once``, and for O-F123-3 the process loop itself, ``Worker._loop``); the brief
capture route; the ``scout_request.execute`` job run by the real worker; the membership routes for
the competing change. The mock provider, or the offline priced provider (the real Anthropic
adapter over an httpx ``MockTransport``, ``test_briefs_postgres._priced``) for the shared ledger.
SQLite is a file opened by the application's own ``app.db.session.build_engine`` (busy timeout
five seconds); PostgreSQL is a database of its own migrated to head (``TEST_POSTGRES_URL``). Each
side is its own request or worker on its own thread, session and connection; the competing writer
of O-F123-2 and O-F123-3 is a separate raw connection that holds SQLite's writer lock (``BEGIN
IMMEDIATE``) or a read transaction, or, on PostgreSQL, a row lock on the rows the meter or the
claim writes.

**What is read back,** raw, on the world's witness engine after both sides finish: the committed
rows, the answer or the job's final state and error and its events, the notifications, and the
accounting: ``llm_usage_events`` and ``llm_budget_reservations``, exactly once per admitted model
call, none erased, none doubled.

**O-F123-2: Design B.** The operator chose "Design B: reserve first" (2026-11-21; coordinator
ruling F123R-1): on SQLite the brief capture and creative generation reserve each call before it
is sent, in a transaction of their own, and are refused before sending when that cannot commit;
the settlement is another transaction of its own. The O-F123-2 cases assert the requirement that
answer set: the admitted call's record exactly once (the usage row and the settled reservation; or,
where the settlement cannot commit, the reservation at its worst case, which recovery charges
once), never doubled; and a call that cannot be reserved is not sent. Both orderings of a writer
(before the call, during it), a reader at the commit, both waits of the worker, a retry, the sweep,
and the precondition (nothing held at any reservation, settlement or call) are run on SQLite;
PostgreSQL's paths are unchanged, which a statement-list test pins. The Scout run is outside
Design B: its reader-at-commit case stays a strict xfail naming that disclosed limit.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.briefs import service as brief_service
from app.core.config import Settings, get_settings
from app.creative import service as creative_service
from app.creative.models import ContentReview, CreativeDraft
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import build_engine
from app.jobs.admission import RunAdmission
from app.jobs.models import Job, JobEvent
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner, Worker
from app.llm import budget as ledger
from app.llm import service as llm_module
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent
from app.main import app
from app.notifications.models import Notification
from app.signals.models import RawSignal
from app.tests._creative_support import CEILINGS, CreativeWorld, postgres_creative_world
from app.tests._seed_support import clear_seeded_runs
from app.tests.test_final_write_boundaries import (
    APPROVAL_READ,
    BRIEF_ROW_LOCK,
    CHANGED,
    CHANGES,
    GENERATION_STORED,
    MEMBERSHIP_READ,
    MODEL_RETURNED,
    ROLE_AFTER,
    _capturable,
    _capture,
    _capture_state,
    _change_membership,
    _first_insert,
    _generate,
    _generation_state,
    _hold_change_before_commit,
    _hold_the_model_call,
    _in_flight,
    _is_postgres,
    _LockedAt,
    _mark_the_model_call,
    _member_elsewhere,
    _membership_audits,
    _org,
    _positions,
    _role,
    _role_in,
    _signal_the_model_call,
    _waits,
)
from app.tests.test_i5e_export_serialization import _Gate, _lock_waits, _Runner
from app.tests.test_i5e_export_serialization_sqlite import (
    _answer,
    _report,
    _Trace,
    application_sqlite_world,
)

#: The competing membership change, made by the organization's owner through the real routes.
CHANGE = pytest.mark.parametrize("change", CHANGES)
#: (a) also with a change the requester's role set still admits: it must not refuse the draft.
CHANGES_AND_A_PROMOTION = pytest.mark.parametrize("change", (*CHANGES, "promoted"))
#: The refusal the generation job already uses for a requester who may not generate
#: (``app.creative.service._requester``): ``AUTHORIZATION`` is not retryable, so the job ends
#: ``failed``.
REQUESTER_REFUSED = ("failed", "authorization", "requester_not_permitted")
#: The one O-F123-2 residual Design B does not cover (a strict xfail).
SCOUT_LIMIT = (
    "O-F123-2, the Scout run on SQLite, a disclosed limit: the operator chose Design B for the "
    "brief capture and creative generation only (2026-11-21, coordinator ruling F123R-1); a "
    "Scout run keeps the same-session meter, which writes its usage rows in the run's own "
    "transaction, so a reader that blocks its commit and then the worker's after-rollback write "
    "makes them disappear (lane F123R hand-off, Stage B)."
)
#: The capture's existing refusal of a call that is not sent (Design B: its reservation could
#: not commit): ``BriefGenerationUnavailableError``.
REFUSED_BEFORE_SENDING = (503, "brief_generation_unavailable",
                          "A brief cannot be generated right now. Try again later.")
#: The job's existing transient refusal of a call that is not sent (retried).
GENERATION_REFUSED_BEFORE_SENDING = ("retry_wait", "transient", "model_unavailable")
#: Marks, in a statement trace, the moment the provider is asked.
PROVIDER_CALLED = "-- the provider is called"
#: SQLite's busy timeout as the application sets it (``app.db.session.build_engine``).
BUSY_TIMEOUT = 5.0
#: A writer or reader released after one busy-timeout wait and before a second one ends.
PAST_ONE_WAIT = 7.5
#: Released after two waits (the usage write's and the worker's after-rollback write's) and before
#: a third (the worker's failure write) ends.
PAST_TWO_WAITS = 12.5
#: Released well inside the first wait.
WITHIN_ONE_WAIT = 2.5
TERMINAL = ("succeeded", "failed", "dead_lettered", "cancelled")


# --------------------------------------------------------------------------- #
# Worlds and settings
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def base(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with application_sqlite_world(tmp_path_factory.mktemp("f123r") / "app.db", "f123r") as w:
            yield w
        return
    with postgres_creative_world("f123r") as w:  # skips unless TEST_POSTGRES_URL is set
        # Bounded waits on every connection opened from here on: a lock that is never released
        # fails its statement instead of hanging the module.
        with w.witness.connect() as conn:
            name = conn.execute(text("SELECT current_database()")).scalar_one()
            conn.execute(text(f'ALTER DATABASE "{name}" SET lock_timeout = \'45s\''))
            conn.commit()
        w.engine.dispose()
        w.witness.dispose()
        w.client = TestClient(app, raise_server_exceptions=False)
        yield w


@pytest.fixture(autouse=True)
def settings_for_the_races(monkeypatch) -> None:
    """Every generation ceiling set; the global capability flags off (each workspace is opened by
    override rows); the job lease renewal kept out of every race window unless a test sets it."""
    settings = get_settings()
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(settings, name, value)
    for flag in ("recommendation_briefs_enabled", "creative_generation_enabled",
                 "content_export_enabled", "notifications_analytics_enabled"):
        monkeypatch.setattr(settings, flag, False)
    monkeypatch.setattr(settings, "worker_heartbeat_seconds", 25.0)
    monkeypatch.setattr(settings, "worker_lease_seconds", 60.0)


def _retire_unfinished_jobs(w: CreativeWorld) -> None:
    """A job this test left waiting for a retry is ended here, so no later test's worker runs it."""
    with w.witness.connect() as conn:
        conn.execute(text(
            "UPDATE jobs SET status = 'failed', worker_id = NULL, lease_token = NULL,"
            " lease_expires_at = NULL WHERE workspace_id = :ws AND status NOT IN"
            " ('succeeded', 'failed', 'dead_lettered', 'cancelled')"
        ), {"ws": w.ws})
        conn.commit()


# --------------------------------------------------------------------------- #
# The competing writer or reader (O-F123-2, O-F123-3)
# --------------------------------------------------------------------------- #
class _SqliteHold:
    """A connection of its own (not the application's engine) on the world's SQLite file that
    holds the database's writer lock (``BEGIN IMMEDIATE``) or, as a reader, a read transaction
    (its SHARED lock keeps every other connection from committing), until released."""

    def __init__(self, w: CreativeWorld, *, reader: bool = False):
        self._raw = sqlite3.connect(w.engine.url.database, timeout=0, isolation_level=None,
                                    check_same_thread=False)
        self.reader = reader
        self.taken_at = self.released_at = None

    def take(self) -> _SqliteHold:
        if self.reader:
            self._raw.execute("BEGIN")
            self._raw.execute("SELECT count(*) FROM jobs").fetchall()
        else:
            self._raw.execute("BEGIN IMMEDIATE")
        self.taken_at = time.monotonic()
        return self

    def release(self) -> None:
        if self.released_at is None:
            self._raw.execute("ROLLBACK")
            self._raw.close()
            self.released_at = time.monotonic()

    def release_at(self, when: float) -> threading.Timer:
        timer = threading.Timer(max(0.0, when - time.monotonic()), self.release)
        timer.start()
        return timer


def _competing_writer(db_file: str, hold: float = 0.0) -> Callable[[], dict]:
    """A competing writer as the application opens SQLite (a five-second busy timeout): it asks
    for the writer lock, records how long it waited and whether it got it, then lets go."""

    def compete() -> dict:
        raw = sqlite3.connect(db_file, timeout=BUSY_TIMEOUT, isolation_level=None)
        started = time.monotonic()
        try:
            raw.execute("BEGIN IMMEDIATE")
            waited = time.monotonic() - started
            time.sleep(hold)
            raw.execute("ROLLBACK")
            return {"got the lock": True, "waited": round(waited, 2)}
        except sqlite3.OperationalError as exc:
            return {"got the lock": False, "error": str(exc),
                    "waited": round(time.monotonic() - started, 2)}
        finally:
            raw.close()

    return compete


class _PgRowHold:
    """A PostgreSQL transaction of its own holding ``FOR UPDATE`` on one row until released."""

    def __init__(self, w: CreativeWorld, statement: str, params: dict):
        self._conn = w.witness.connect()
        self._tx = self._conn.begin()
        found = self._conn.execute(text(statement), params).all()
        assert found, f"nothing to lock: {statement} {params}"
        self.taken_at = time.monotonic()

    def release(self) -> None:
        if self._tx.is_active:
            self._tx.rollback()
            self._conn.close()


def _timed(fn: Callable) -> Callable[[], tuple]:
    def run() -> tuple:
        started = time.monotonic()
        result = fn()
        return result, started, time.monotonic()

    return run


def _job_events(w: CreativeWorld, job_id: str) -> list[str]:
    with w.witness.connect() as conn:
        rows = conn.execute(
            select(JobEvent.event_type).where(JobEvent.job_id == job_id)
            .order_by(JobEvent.created_at, JobEvent.id)
        ).scalars().all()
    return [r for r in rows if r != "enqueued"]


def _job_summary(w: CreativeWorld, job_id: str) -> dict:
    job = w.job(job_id)
    return {
        "job": (job["status"], job["last_error_code"], job["last_error_summary"]),
        "attempts": job["attempt_count"],
        "lease left": (job["worker_id"], job["lease_token"], job["lease_expires_at"]),
    }


def _usage_of_job(w: CreativeWorld, job_id: str) -> dict:
    return {
        "usage rows of the job": w.count(LLMUsageEvent, job_id=job_id),
        "ledger reservations of the job": w.count(LLMBudgetReservation, job_id=job_id),
    }


# =========================================================================== #
# O-F123-1: the generation requester at the final draft-write boundary
# =========================================================================== #
@CHANGES_AND_A_PROMOTION
def test_o1_a_requester_change_during_the_model_call_decides_the_draft(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a) The job has passed every gate, the requester (the marketer) included, and is in its
    model call. The owner removes the requester, demotes them to viewer, or promotes them to
    admin, through the membership routes.

    * SQLite: the job holds nothing during the call, so the change commits at once. The admitted
      call finishes; a removal or a demotion must then prevent the draft: the job ends ``failed``
      with the refusal it already uses for a requester who may not generate (``authorization``,
      ``requester_not_permitted``), with no draft, review, audit row or notification, and the
      call's usage row and ledger reservation kept, once. A promotion does not prevent it.
    * PostgreSQL (the protection to preserve): the job holds the organization ``KEY SHARE`` across
      the call, so the change waits at its organization ``FOR UPDATE`` until the job has
      committed; the draft is stored, then the change commits."""
    w = _org(base, "o1a")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    generating = _in_flight(w.drain, gate, "the generation")
    try:
        with _Trace(w.engine) as trace:
            changing = _Runner(lambda: _change_membership(w, change))
            changing.start()
            waited_at = _waits(w, trace, changing)
    finally:
        gate.release.set()
    drained = generating.outcome()
    changed = changing.outcome()
    stored = change == "promoted" or _is_postgres(w)
    _report("O-F123-1 (a)", {
        "the change waited at": waited_at,
        "change": _answer(changed),
        "drained": drained,
        **_generation_state(w, brief_id, job_id),
        "membership": _role(w, w.users["marketer"]),
        "membership audit rows": _membership_audits(w),
    }, {
        "the change waited at": "organizations FOR UPDATE" if _is_postgres(w) else None,
        "change": CHANGED[change],
        "drained": 1,
        "job": GENERATION_STORED if stored else REQUESTER_REFUSED,
        "drafts (brief, version)": [("the parent", 1)] if stored else [],
        "content reviews": 1 if stored else 0,
        "draft audit rows": 1 if stored else 0,
        "draft.generated notifications": 1 if stored else 0,
        "usage rows of the job": 1,
        "ledger reservations of the job": 1,
        "each draft names the job's usage row": [True] if stored else [],
        "the parent brief's decisions": ["approved"],
        "membership": ROLE_AFTER[change],
        "membership audit rows": 1,
    })


@CHANGE
def test_o1_a_requester_change_in_flight_at_the_boundary_is_waited_for_and_prevents_the_draft(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a') SQLite. The job's model call has answered; nothing of it is written. The change is
    then held with its write flushed and uncommitted (it holds the writer lock). The job is
    released and must wait at its first write (the usage rows), the change commits, and the job
    must read the committed membership and store nothing; usage kept once. A re-read placed before
    the job's writer lock would read the membership as it was and store the draft. (Under
    Design B the call is settled in a transaction of its own, so the job's first wait is its
    ``BEGIN IMMEDIATE`` before version allocation.)

    PostgreSQL has no such ordering: the job holds the organization ``KEY SHARE`` from before the
    call, so a membership change cannot hold its organization ``FOR UPDATE`` when the call returns
    (it waits for the job: case (a))."""
    if _is_postgres(base):
        pytest.skip("PostgreSQL: the change waits for the job from before the call, case (a)")
    w = _org(base, "o1i")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    model, held = _Gate(), _Gate()
    _hold_the_model_call(monkeypatch, model, after=True)
    _hold_change_before_commit(monkeypatch, change, held)
    generating = _in_flight(w.drain, model, "the generation")
    try:
        changing = _in_flight(lambda: _change_membership(w, change), held, "the change")
        with _Trace(w.engine) as trace:
            model.release.set()
            waited_at = _waits(w, trace, generating)
    finally:
        model.release.set()
        held.release.set()
    _report("O-F123-1 (a')", {
        "the job waited at": waited_at,
        "change": _answer(changing.outcome()),
        "drained": generating.outcome(),
        **_generation_state(w, brief_id, job_id),
        "membership": _role(w, w.users["marketer"]),
    }, {
        "the job waited at": "BEGIN IMMEDIATE",
        "change": CHANGED[change],
        "drained": 1,
        "job": REQUESTER_REFUSED,
        "drafts (brief, version)": [],
        "content reviews": 0,
        "draft audit rows": 0,
        "draft.generated notifications": 0,
        "usage rows of the job": 1,
        "ledger reservations of the job": 1,
        "each draft names the job's usage row": [],
        "the parent brief's decisions": ["approved"],
        "membership": ROLE_AFTER[change],
    })


@CHANGE
def test_o1_a_requester_change_committed_after_the_call_before_the_writer_lock_prevents_the_draft(
    base: CreativeWorld, monkeypatch, change: str
):
    """(a''), SQLite (Design B). The job's call has been reserved, made and settled in
    transactions of their own, so the job holds no lock when it reaches its ``BEGIN IMMEDIATE``
    before version allocation; it is held there. The change commits. The job then takes the lock
    and must read the committed membership: nothing stored, the call's record once. A re-read
    placed before the lock would miss the change."""
    if _is_postgres(base):
        pytest.skip("PostgreSQL: the change waits for the job from before the call, case (a)")
    w = _org(base, "o1j")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    monkeypatch.setattr(creative_service, "hold_sqlite_writer_lock",
                        gate.before(creative_service.hold_sqlite_writer_lock))
    generating = _in_flight(w.drain, gate, "the generation")
    try:
        changed = _change_membership(w, change)
    finally:
        gate.release.set()
    _report("O-F123-1 (a'')", {
        "change": _answer(changed),
        "drained": generating.outcome(),
        **_generation_state(w, brief_id, job_id),
        "membership": _role(w, w.users["marketer"]),
    }, {
        "change": CHANGED[change],
        "drained": 1,
        "job": REQUESTER_REFUSED,
        "drafts (brief, version)": [],
        "content reviews": 0,
        "draft audit rows": 0,
        "draft.generated notifications": 0,
        "usage rows of the job": 1,
        "ledger reservations of the job": 1,
        "each draft names the job's usage row": [],
        "the parent brief's decisions": ["approved"],
        "membership": ROLE_AFTER[change],
    })


@CHANGE
def test_o1_a_generation_past_its_boundary_stores_its_draft_and_the_requester_change_waits(
    base: CreativeWorld, monkeypatch, change: str
):
    """(b) The job is held after every re-read, right before its draft row is built (PostgreSQL:
    the organization ``KEY SHARE`` since before the call and the brief ``FOR UPDATE``; SQLite: the
    writer lock since its usage rows). The change starts and must wait (the organization ``FOR
    UPDATE``; its first write). The draft, its review, its audit row and its notification commit,
    then the change; they stay exactly as written."""
    w = _org(base, "o1b")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    monkeypatch.setattr(creative_service, "account_snapshot",
                        gate.before(creative_service.account_snapshot))
    generating = _in_flight(w.drain, gate, "the generation")
    try:
        with _Trace(w.engine) as trace:
            changing = _Runner(lambda: _change_membership(w, change))
            changing.start()
            waited_at = _waits(w, trace, changing)
    finally:
        gate.release.set()
    drained = generating.outcome()

    def records() -> dict:
        return {
            "drafts": w.rows(CreativeDraft, workspace_id=w.ws),
            "reviews": w.rows(ContentReview, workspace_id=w.ws),
            "audit": w.rows(AuditLog, action="creative_draft.created", workspace_id=w.ws),
            "notifications": w.rows(Notification, workspace_id=w.ws,
                                    event_code="draft.generated"),
        }

    written = records()
    changed = changing.outcome()
    _report("O-F123-1 (b)", {
        "the change waited at": waited_at,
        "drained": drained,
        "change": _answer(changed),
        **_generation_state(w, brief_id, job_id),
        "the draft's records unchanged by the change": records() == written,
        "membership": _role(w, w.users["marketer"]),
        "membership audit rows": _membership_audits(w),
    }, {
        "the change waited at": (
            "organizations FOR UPDATE" if _is_postgres(w)
            else ("delete organization_members" if change == "removed"
                  else "update organization_members")
        ),
        "drained": 1,
        "change": CHANGED[change],
        "job": GENERATION_STORED,
        "drafts (brief, version)": [("the parent", 1)],
        "content reviews": 1,
        "draft audit rows": 1,
        "draft.generated notifications": 1,
        "usage rows of the job": 1,
        "ledger reservations of the job": 1,
        "each draft names the job's usage row": [True],
        "the parent brief's decisions": ["approved"],
        "the draft's records unchanged by the change": True,
        "membership": ROLE_AFTER[change],
        "membership audit rows": 1,
    })


def test_o1_a_requester_removed_here_but_an_editor_elsewhere_is_refused(
    base: CreativeWorld, monkeypatch
):
    """(a), the re-read's scope, SQLite. The requester is also a marketer of another organization.
    Removed from this one during the model call, the job must refuse the draft: the re-read is the
    job's organization's membership; the one elsewhere is untouched and does not admit it."""
    if _is_postgres(base):
        pytest.skip("PostgreSQL: the removal waits for the job from before the call, case (a)")
    w = _org(base, "o1x")
    other = _member_elsewhere(base, w)
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    generating = _in_flight(w.drain, gate, "the generation")
    try:
        changed = _change_membership(w, "removed")
    finally:
        gate.release.set()
    generating.outcome()
    state = _generation_state(w, brief_id, job_id)
    _report("O-F123-1 scope", {
        "change": _answer(changed),
        "job": state["job"],
        "drafts (brief, version)": state["drafts (brief, version)"],
        "usage rows of the job": state["usage rows of the job"],
        "membership elsewhere": _role_in(w, other, w.users["marketer"]),
    }, {
        "change": CHANGED["removed"],
        "job": REQUESTER_REFUSED,
        "drafts (brief, version)": [],
        "usage rows of the job": 1,
        "membership elsewhere": "marketer",
    })


def test_o1_the_requesters_membership_is_read_after_the_model_call_the_brief_lock_and_the_approval(
    base: CreativeWorld, monkeypatch
):
    """The job's statements after the model call: the brief lock, the parent's approval (F-2),
    then the requester's membership role (fresh, a column read with no lock of its own), then the
    draft insert. The requester's earlier read, before the call, stays. Printed: every statement
    after the call (the statement list before and after the repair)."""
    w = _org(base, "o1o")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    with _Trace(w.engine) as trace:
        _mark_the_model_call(monkeypatch, trace)
        assert w.drain() == 1
    sent = trace.sent
    returned = sent.index(MODEL_RETURNED)
    print("\nSTATEMENTS AFTER THE MODEL CALL:\n" + "\n".join(
        f"  {i - returned:3d} {q[:160]}" for i, q in enumerate(sent) if i > returned))

    def after(prefix: str) -> list[int]:
        return [i for i in _positions(sent, prefix) if i > returned]

    insert = _first_insert(sent, "creative_drafts")
    brief_lock, approval = after(BRIEF_ROW_LOCK), after(APPROVAL_READ)
    membership = [i for i in after(MEMBERSHIP_READ) if i < insert]
    before_the_call = [i for i in _positions(sent, MEMBERSHIP_READ) if i < returned]
    _report("O-F123-1 order", {
        "job": w.job(job_id)["status"],
        "requester reads before the call": len(before_the_call),
        "requester reads after the call": len(membership),
        "brief lock < approval < requester < draft insert": bool(membership) and (
            brief_lock[0] < approval[0] < membership[0] < insert),
        "the requester read takes no lock": bool(membership) and " for " not in sent[
            membership[0]],
    }, {
        "job": "succeeded",
        "requester reads before the call": 1,
        "requester reads after the call": 1,
        "brief lock < approval < requester < draft insert": True,
        "the requester read takes no lock": True,
    })


def test_o1_sqlite_the_requester_reread_runs_while_the_writer_lock_is_held(
    base: CreativeWorld, monkeypatch
):
    """SQLite: the protection is the writer lock the job's transaction holds from its first write
    (the usage rows) to its commit. The requester's re-read is sent while that transaction is open
    and the lock is held: another connection cannot take it at that moment."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock: PostgreSQL's order is pinned by the order test")
    w = _org(base, "o1l")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    with _LockedAt(w, (MEMBERSHIP_READ,), after_model=True) as reads:
        _signal_the_model_call(monkeypatch, reads.model_returned)
        assert w.drain() == 1
    _report("O-F123-1 SQLite writer lock", {
        "job": w.job(job_id)["status"],
        "requester re-read": reads.seen,
    }, {
        "job": "succeeded",
        "requester re-read": [(MEMBERSHIP_READ, True, "held")],
    })


# =========================================================================== #
# O-F123-2: an admitted call's usage under contention (Design B on SQLite)
# =========================================================================== #
def _hold_the_provider_call(monkeypatch, gate: _Gate, provider=None) -> None:
    """The first provider call made from here on is held just before the provider is asked. On
    SQLite's reserve-first path (O-F123-2, Design B) the attempt's reservation is committed by
    then: the call is admitted. ``provider``: the process service's own (the mock) by default."""
    provider = provider or llm_module.get_llm_service()._provider
    monkeypatch.setattr(provider, "generate", gate.before(provider.generate))


def _amount(value) -> float | None:
    return None if value is None else round(float(value), 6)


def _ledger(w: CreativeWorld, *, job_id: str | None = None,
            task: str = brief_service.BRIEF_TASK) -> dict:
    """The admitted calls' record: a job's reservations and usage rows, or the workspace's
    job-less ones (a capture's), as ``(state, reserved, settled)`` and a count."""
    if job_id is not None:
        rows = w.rows(LLMBudgetReservation, job_id=job_id)
        usage = w.rows(LLMUsageEvent, job_id=job_id)
    else:
        rows = [r for r in w.rows(LLMBudgetReservation, workspace_id=w.ws) if r["job_id"] is None]
        usage = [u for u in w.rows(LLMUsageEvent, workspace_id=w.ws, task=task)
                 if u["job_id"] is None]
    return {
        "reservations (state, reserved, settled)": sorted(
            (r["state"], _amount(r["reserved_usd"]), _amount(r["settled_usd"])) for r in rows),
        "usage rows": len(usage),
    }


def _sweep_with_the_worker(w: CreativeWorld, hours: float = 1.0) -> bool:
    """One poll of the real worker with its clock ``hours`` ahead: lease recovery, then the
    reservation sweep (``app.llm.budget.sweep``), then a claim. Returns whether a job ran."""
    ahead = datetime.now(UTC) + timedelta(hours=hours)
    runner = JobRunner(settings=get_settings(), session_factory=w.factory,
                       store=DurableJobStore(), clock=lambda: ahead)
    return runner.poll_once(worker_id="w-f123r-sweep")


def _priced_capture_service(statuses: list[int], *, sleep: Callable[[float], None] | None = None,
                            on_request: Callable[[], None] | None = None):
    """The real Anthropic adapter over an httpx ``MockTransport`` (offline, priced: the shared
    ledger's path), answering the brief task's output, or each status of ``statuses`` in turn
    first (a 503 is a transient failure the service retries). Returns ``(service, requests)``."""
    import httpx

    from app.llm.providers_real import AnthropicProvider
    from app.llm.service import LLMService
    from app.tests._llm_offline import offline_settings
    from app.tests.test_briefs_postgres import EXPLAINED, KEY, MODEL

    queue, requests = list(statuses), []

    def handler(request):
        requests.append(1)
        if on_request is not None:
            on_request()
        if queue:
            return httpx.Response(queue.pop(0), json={"type": "error", "error": {
                "type": "overloaded_error", "message": "busy"}})
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": json.dumps(EXPLAINED)}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 1000, "output_tokens": 500}})

    settings = offline_settings(llm_provider="anthropic", llm_api_key=KEY, llm_model=MODEL)
    provider = AnthropicProvider(KEY, MODEL, 5,
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    return LLMService(provider, sleep=sleep or (lambda _s: None), settings=settings), requests


def _priced_creative_service(statuses: list[int]):
    """The same adapter answering a neutral creative draft (the generation task)."""
    import httpx

    from app.llm.providers_real import AnthropicProvider
    from app.llm.service import LLMService
    from app.tests._llm_offline import offline_settings
    from app.tests.test_briefs_postgres import KEY, MODEL

    queue, requests = list(statuses), []
    draft_out = {"headline": "Parking made simple",
                 "body": "A quick note: local families are asking about weekend parking.",
                 "call_to_action": "Plan your visit"}

    def handler(request):
        requests.append(1)
        if queue:
            return httpx.Response(queue.pop(0), json={"type": "error", "error": {
                "type": "overloaded_error", "message": "busy"}})
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": json.dumps(draft_out)}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 900, "output_tokens": 300}})

    settings = offline_settings(llm_provider="anthropic", llm_api_key=KEY, llm_model=MODEL)
    provider = AnthropicProvider(KEY, MODEL, 5,
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    return LLMService(provider, sleep=lambda _s: None, settings=settings), requests


def _poll_once(w: CreativeWorld) -> bool:
    """One poll of the real worker (``JobRunner.poll_once``): one attempt of one due job."""
    runner = JobRunner(settings=get_settings(), session_factory=w.factory,
                       store=DurableJobStore())
    return runner.poll_once(worker_id="w-creative")


def _use_service(monkeypatch, service) -> None:
    """Every call site's model call (``get_llm_service().run``) goes to ``service``."""
    monkeypatch.setattr(llm_module.get_llm_service(), "run", service.run)


def _capture_answer_and_briefs(w: CreativeWorld, opportunity_id: str, answer: tuple) -> dict:
    state = _capture_state(w, opportunity_id)
    return {"capture": answer,
            "captured briefs": len(state["captured briefs (version, by the marketer)"])}


@pytest.mark.parametrize("released", ["within the busy timeout", "past the busy timeout"])
def test_o2_sqlite_a_writer_present_before_the_captures_call_refuses_it_before_sending(
    base: CreativeWorld, monkeypatch, released: str
):
    """(a), the writer first, SQLite, brief capture (Design B). Another connection holds the
    writer lock when the capture reaches its model call. The call's reservation (its own short
    transaction) must wait. Released within the busy timeout: the reservation commits, the call is
    made and settled, 201, the record once. Released past it: the capture is refused **before the
    call is sent**, with the existing 503 ``brief_generation_unavailable``: no model call, no
    reservation, no usage row, no brief."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; PostgreSQL's analogue is the period-row test")
    w = _org(base, "o2f")
    calls = _count_model_calls(monkeypatch)
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    capturing = _in_flight(_timed(lambda: _capture(w, opportunity_id, record_id)), gate,
                           "the capture")
    holder = _SqliteHold(w).take()
    seconds = WITHIN_ONE_WAIT if released == "within the busy timeout" else PAST_ONE_WAIT
    try:
        with _Trace(w.engine) as trace:
            released_at = time.monotonic()
            gate.release.set()
            timer = holder.release_at(released_at + seconds)
            waited_at = _waits(w, trace, capturing)
    finally:
        gate.release.set()
        timer.join()
        holder.release()
    response, _started, ended = capturing.outcome()
    print(f"\nTIMING capture answered {ended - released_at:.2f} s after it reached the call")
    within = released == "within the busy timeout"
    _report(f"O-F123-2 capture, a writer first, {released}", {
        "the reservation waited at the period row": str(waited_at).startswith(
            "insert into llm_budget_periods"),
        "model calls": calls["model"],
        **_capture_answer_and_briefs(w, opportunity_id, _answer(response)),
        **_ledger(w),
    }, {
        "the reservation waited at the period row": True,
        "model calls": 1 if within else 0,
        "capture": (201,) if within else REFUSED_BEFORE_SENDING,
        "captured briefs": 1 if within else 0,
        "reservations (state, reserved, settled)": [("settled", 0.0, 0.0)] if within else [],
        "usage rows": 1 if within else 0,
    })


@pytest.mark.parametrize("released", ["within the busy timeout", "past the busy timeout"])
def test_o2_sqlite_a_writer_holds_the_lock_across_the_captures_usage_write(
    base: CreativeWorld, monkeypatch, released: str
):
    """(a), the writer during the call, SQLite, brief capture (Design B). The capture's
    reservation is committed and its model call is in flight when another connection takes the
    writer lock. The call returns; its settlement (a short transaction of its own) must wait.
    Released within the busy timeout: settled, 201, one brief, the usage row and the settled
    reservation once. Released past it: the settlement cannot commit, the capture answers the
    generic 500 and stores nothing, and **the reservation is the call's record**: once, at its
    worst case (0 with the mock); the worker's sweep then expires it, once, at that worst case."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; PostgreSQL's analogue is the period-row test")
    w = _org(base, "o2c")
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    _hold_the_provider_call(monkeypatch, gate)
    capturing = _in_flight(_timed(lambda: _capture(w, opportunity_id, record_id)), gate,
                           "the capture")
    holder = _SqliteHold(w).take()
    seconds = WITHIN_ONE_WAIT if released == "within the busy timeout" else PAST_ONE_WAIT
    try:
        with _Trace(w.engine) as trace:
            released_at = time.monotonic()
            gate.release.set()
            timer = holder.release_at(released_at + seconds)
            waited_at = _waits(w, trace, capturing)
    finally:
        gate.release.set()
        timer.join()
        holder.release()
    response, _started, ended = capturing.outcome()
    after_the_capture = _ledger(w)
    swept = [_sweep_with_the_worker(w), _sweep_with_the_worker(w)]
    print(f"\nTIMING capture answered {ended - released_at:.2f} s after the call returned; "
          f"holder released after {holder.released_at - released_at:.2f} s")
    within = released == "within the busy timeout"
    _report(f"O-F123-2 capture (a) {released}", {
        "the capture waited at its settlement": str(waited_at).startswith(
            "update llm_budget_reservations"),
        **_capture_answer_and_briefs(w, opportunity_id, _answer(response)),
        "after the capture": after_the_capture,
        "after two sweeps": _ledger(w),
        "the sweeps ran no job": swept,
    }, {
        "the capture waited at its settlement": True,
        "capture": (201,) if within else (500, "internal_error", "An unexpected error occurred"),
        "captured briefs": 1 if within else 0,
        "after the capture": {
            "reservations (state, reserved, settled)": [
                ("settled", 0.0, 0.0) if within else ("reserved", 0.0, None)],
            "usage rows": 1 if within else 0,
        },
        "after two sweeps": {
            "reservations (state, reserved, settled)": [
                ("settled", 0.0, 0.0) if within else ("expired", 0.0, 0.0)],
            "usage rows": 1 if within else 0,
        },
        "the sweeps ran no job": [False, False],
    })


def test_o2_sqlite_the_capture_holds_the_lock_first_and_the_competing_writer_waits(
    base: CreativeWorld, monkeypatch
):
    """(b) SQLite, brief capture. The capture has taken the writer lock for its final write (its
    ``BEGIN IMMEDIATE`` before the author re-read; its call was reserved and settled before, in
    transactions of their own) and is held there. Another writer, opened as the application opens
    SQLite, asks for the lock and must wait; the capture commits (201, one brief, its usage row and
    settled reservation once), then the other writer gets the lock."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock")
    w = _org(base, "o2b")
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    monkeypatch.setattr(brief_service, "hold_sqlite_writer_lock",
                        gate.after(brief_service.hold_sqlite_writer_lock))
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    competing = _Runner(_competing_writer(w.engine.url.database))
    try:
        competing.start()
        time.sleep(1.5)
        still_waiting = competing.is_alive()
    finally:
        gate.release.set()
    captured = capturing.outcome()
    other = competing.outcome()
    _report("O-F123-2 capture (b)", {
        "the other writer was still waiting after 1.5 s": still_waiting,
        "the other writer got the lock": other["got the lock"],
        "it waited at least 1.5 s": other["waited"] >= 1.5,
        **_capture_answer_and_briefs(w, opportunity_id, _answer(captured)),
        **_ledger(w),
    }, {
        "the other writer was still waiting after 1.5 s": True,
        "the other writer got the lock": True,
        "it waited at least 1.5 s": True,
        "capture": (201,),
        "captured briefs": 1,
        "reservations (state, reserved, settled)": [("settled", 0.0, 0.0)],
        "usage rows": 1,
    })


def test_o2_sqlite_a_reader_holds_the_captures_commit_past_the_busy_timeout(
    base: CreativeWorld, monkeypatch
):
    """(c) SQLite, brief capture, at commit (Design B). The capture has stored its brief and holds
    the writer lock; a reader then opens a read transaction on another connection and keeps it
    past the busy timeout. The capture's commit fails (the generic 500, no brief). The call's
    record does not depend on that commit: its reservation and settlement, with the usage row,
    were committed in transactions of their own, once."""
    if _is_postgres(base):
        pytest.skip("SQLite's locks")
    w = _org(base, "o2r")
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    monkeypatch.setattr(brief_service, "notify", gate.after(brief_service.notify))
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    reader = _SqliteHold(w, reader=True).take()
    released_at = time.monotonic()
    gate.release.set()
    timer = reader.release_at(released_at + PAST_ONE_WAIT)
    try:
        captured = capturing.outcome()
    finally:
        timer.join()
        reader.release()
    _report("O-F123-2 capture (c) reader at commit", {
        **_capture_answer_and_briefs(w, opportunity_id, _answer(captured)),
        **_ledger(w),
    }, {
        "capture": (500, "internal_error", "An unexpected error occurred"),
        "captured briefs": 0,
        "reservations (state, reserved, settled)": [("settled", 0.0, 0.0)],
        "usage rows": 1,
    })


def test_o2_sqlite_a_later_attempts_reservation_blocked_refuses_it_before_sending(
    base: CreativeWorld, monkeypatch
):
    """The retry case, SQLite, brief capture, priced (Design B). The first attempt's reservation
    commits, its call answers 503 (transient) and is settled at its worst case with its usage row.
    During the service's back-off another connection takes the writer lock past the busy timeout.
    The second attempt's reservation cannot commit, so it is refused before its call is sent: the
    capture answers the existing 503 ``brief_generation_unavailable``; one call made, one
    reservation (settled), one usage row; no brief."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock")
    w = _org(base, "o2t")
    opportunity_id, record_id = _capturable(w)
    holder = _SqliteHold(w)
    timers: list[threading.Timer] = []

    def back_off(_seconds: float) -> None:
        holder.take()
        timers.append(holder.release_at(holder.taken_at + PAST_ONE_WAIT))

    priced, requests = _priced_capture_service([503], sleep=back_off)
    _use_service(monkeypatch, priced)
    try:
        response = _capture(w, opportunity_id, record_id)
    finally:
        for timer in timers:
            timer.join()
        holder.release()
    ledger = _ledger(w)
    _report("O-F123-2 capture, a retry's reservation blocked", {
        **_capture_answer_and_briefs(w, opportunity_id, _answer(response)),
        "calls sent": len(requests),
        "reservation states": [r[0] for r in ledger["reservations (state, reserved, settled)"]],
        "the settled reservation was charged its worst case": [
            r[1] == r[2] and r[1] > 0 for r in ledger["reservations (state, reserved, settled)"]],
        "usage rows": ledger["usage rows"],
    }, {
        "capture": REFUSED_BEFORE_SENDING,
        "captured briefs": 0,
        "calls sent": 1,
        "reservation states": ["settled"],
        "the settled reservation was charged its worst case": [True],
        "usage rows": 1,
    })


def test_o2_sqlite_an_unsettled_capture_reservation_is_expired_once_at_its_worst_case(
    base: CreativeWorld, monkeypatch
):
    """SQLite, brief capture, priced (Design B): a request-path reservation (no job) whose
    settlement cannot commit stays ``reserved`` at its worst case ``w`` (already counted by the
    month's ``reserved_usd``), and the worker's reservation sweep expires it, once, charging
    ``w``: the period's ``settled_usd`` grows by ``w`` once, however many sweeps run."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; the shared ledger's PostgreSQL recovery is pinned "
                    "elsewhere")
    w = _org(base, "o2x")
    opportunity_id, record_id = _capturable(w)
    priced, requests = _priced_capture_service([])
    gate = _Gate()
    _hold_the_provider_call(monkeypatch, gate, priced._provider)
    _use_service(monkeypatch, priced)
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    holder = _SqliteHold(w).take()
    gate.release.set()
    timer = holder.release_at(holder.taken_at + PAST_ONE_WAIT)
    try:
        captured = capturing.outcome()
    finally:
        timer.join()
        holder.release()

    def period() -> tuple:
        [row] = w.rows(LLMBudgetPeriod, workspace_id=w.ws)
        return _amount(row["reserved_usd"]), _amount(row["settled_usd"])

    before = (_ledger(w), period())
    _sweep_with_the_worker(w)
    once = (_ledger(w), period())
    _sweep_with_the_worker(w)
    twice = (_ledger(w), period())
    [(_state, worst, _settled)] = before[0]["reservations (state, reserved, settled)"]
    _report("O-F123-2 the sweep expires a capture's reservation", {
        "capture": _answer(captured),
        "calls sent": len(requests),
        "worst case is priced": worst > 0,
        "before the sweep": before,
        "after one sweep": once,
        "after two sweeps": twice,
    }, {
        "capture": (500, "internal_error", "An unexpected error occurred"),
        "calls sent": 1,
        "worst case is priced": True,
        "before the sweep": ({"reservations (state, reserved, settled)": [
            ("reserved", worst, None)], "usage rows": 0}, (worst, 0.0)),
        "after one sweep": ({"reservations (state, reserved, settled)": [
            ("expired", worst, worst)], "usage rows": 0}, (worst, worst)),
        "after two sweeps": ({"reservations (state, reserved, settled)": [
            ("expired", worst, worst)], "usage rows": 0}, (worst, worst)),
    })


def test_o2_sqlite_a_writer_present_before_the_generation_call_refuses_it_before_sending(
    base: CreativeWorld, monkeypatch
):
    """(a), the writer first, SQLite, the generation job under the real worker (Design B).
    Another connection holds the writer lock when the job reaches its model call, past the busy
    timeout. The call's reservation cannot commit, so the attempt is refused before the call is
    sent, with the job's existing transient refusal (``model_unavailable``, to be retried): no
    model call, no reservation, no usage row, no draft."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; PostgreSQL's analogue is the period-row test")
    w = _org(base, "o2h")
    calls = _count_model_calls(monkeypatch)
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    generating = _in_flight(lambda: _poll_once(w), gate, "the generation")
    holder = _SqliteHold(w).take()
    gate.release.set()
    timer = holder.release_at(holder.taken_at + PAST_ONE_WAIT)
    try:
        polled = generating.outcome()
    finally:
        timer.join()
        holder.release()
    summary = _job_summary(w, job_id)
    observed = {"polled": polled, "job": summary["job"], "model calls": calls["model"],
                "drafts": w.count(CreativeDraft, brief_id=brief_id), **_ledger(w, job_id=job_id)}
    _retire_unfinished_jobs(w)
    _report("O-F123-2 generation, a writer first", observed, {
        "polled": True,
        "job": GENERATION_REFUSED_BEFORE_SENDING,
        "model calls": 0,
        "drafts": 0,
        "reservations (state, reserved, settled)": [],
        "usage rows": 0,
    })


def test_o2_sqlite_a_writer_holds_the_lock_across_the_generation_jobs_usage_write(
    base: CreativeWorld, monkeypatch
):
    """(a), the writer during the call, SQLite, the generation job under the real worker
    (Design B). The job's reservation is committed and its model call is in flight when another
    connection takes the writer lock, released past one busy-timeout wait. The settlement cannot
    commit; the attempt fails (transient) and waits for its retry, its reservation left at its
    worst case (0 with the mock). The retry's meter releases that reservation at its worst case
    (the shared ledger's existing rebind recovery) and makes its own call, settled. So each of the
    two admitted calls is recorded exactly once: one released, one settled with its usage row."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; PostgreSQL's analogue is the period-row test")
    monkeypatch.setattr(get_settings(), "job_retry_base_seconds", 0.2)
    monkeypatch.setattr(get_settings(), "job_retry_max_seconds", 0.5)
    w = _org(base, "o2g")
    calls = _count_model_calls(monkeypatch)
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_provider_call(monkeypatch, gate)
    generating = _in_flight(lambda: _poll_once(w), gate, "the generation")
    holder = _SqliteHold(w).take()
    released_at = time.monotonic()
    gate.release.set()
    timer = holder.release_at(released_at + PAST_ONE_WAIT)
    try:
        polled = generating.outcome()
    finally:
        timer.join()
        holder.release()
    first = {**_job_summary(w, job_id), **_ledger(w, job_id=job_id)}
    print(f"\nTIMING the first attempt ended {time.monotonic() - released_at:.2f} s after the "
          f"call returned; job {first['job']}")
    deadline = time.monotonic() + 20
    while w.job(job_id)["status"] != "succeeded" and time.monotonic() < deadline:
        w.drain()
        time.sleep(0.2)
    _report("O-F123-2 generation, a writer during the call", {
        "polled": polled,
        "after the first attempt": {k: first[k] for k in (
            "job", "reservations (state, reserved, settled)", "usage rows")},
        "after the retry": {
            "status": w.job(job_id)["status"],
            "attempts": w.job(job_id)["attempt_count"],
            **_ledger(w, job_id=job_id),
        },
        "model calls": calls["model"],
        "drafts": w.count(CreativeDraft, brief_id=brief_id),
    }, {
        "polled": True,
        "after the first attempt": {
            "job": ("retry_wait", "transient", "OperationalError"),
            "reservations (state, reserved, settled)": [("reserved", 0.0, None)],
            "usage rows": 0,
        },
        "after the retry": {
            "status": "succeeded",
            "attempts": 2,
            "reservations (state, reserved, settled)": [("released", 0.0, 0.0),
                                                        ("settled", 0.0, 0.0)],
            "usage rows": 1,
        },
        "model calls": 2,
        "drafts": 1,
    })


def test_o2_sqlite_a_writer_past_both_waits_during_the_generation_call_is_recovered_once(
    base: CreativeWorld, monkeypatch
):
    """(a), past both waits, SQLite, the generation job under the real worker loop (Design B).
    The job's reservation is committed and its call is in flight when another connection takes the
    writer lock for 12.5 s. The settlement cannot commit, nor can the attempt's failure write: the
    loop survives, and once the job's lease has expired and the lock is free, lease recovery
    requeues it; the retry's meter releases the first call's reservation at its worst case and
    makes its own call, settled. Each admitted call is recorded exactly once; the job ends with
    its draft."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock")
    settings = get_settings()
    monkeypatch.setattr(settings, "worker_lease_seconds", 3.0)
    monkeypatch.setattr(settings, "worker_heartbeat_seconds", 1.0)
    w = _org(base, "o2z")
    calls = _count_model_calls(monkeypatch)
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_provider_call(monkeypatch, gate)
    holder = _SqliteHold(w)
    with _LoopWatch(w, monkeypatch) as loop:
        gate.wait("the generation")
        holder.take()
        gate.release.set()
        timer = holder.release_at(holder.taken_at + PAST_TWO_WAITS)
        try:
            status = _until_terminal(w, job_id, seconds=60.0)
        finally:
            timer.join()
            holder.release()
        alive = loop.alive()
    summary = _job_summary(w, job_id)
    _report("O-F123-2 generation, a writer past both waits", {
        "terminal": status,
        "the loop kept running": alive,
        "job": summary["job"],
        "attempts": summary["attempts"],
        "model calls": calls["model"],
        "drafts": w.count(CreativeDraft, brief_id=brief_id),
        **_ledger(w, job_id=job_id),
        "events": _job_events(w, job_id),
    }, {
        "terminal": "succeeded",
        "the loop kept running": True,
        "job": GENERATION_STORED,
        "attempts": 2,
        "model calls": 2,
        "drafts": 1,
        "reservations (state, reserved, settled)": [("released", 0.0, 0.0),
                                                    ("settled", 0.0, 0.0)],
        "usage rows": 1,
        "events": ["claimed", "started", "lease_recovered", "claimed", "started", "succeeded"],
    })


def _nothing_held(db_file: str) -> str:
    """``free`` when no connection holds any lock on the database (a ``BEGIN EXCLUSIVE`` with no
    busy timeout succeeds: no reader's SHARED lock, no writer's RESERVED or PENDING lock)."""
    raw = sqlite3.connect(db_file, timeout=0, isolation_level=None)
    try:
        raw.execute("BEGIN EXCLUSIVE")
        raw.execute("ROLLBACK")
        return "free"
    except sqlite3.OperationalError as exc:
        return "held" if "locked" in str(exc) else str(exc)
    finally:
        raw.close()


@pytest.mark.parametrize("flow", ["capture-mock", "capture-priced", "generation-mock",
                                  "generation-priced"])
def test_o2_sqlite_nothing_is_held_at_any_reservation_settlement_or_call(
    base: CreativeWorld, monkeypatch, flow: str
):
    """The precondition of Design B (coordinator ruling F123R-1 §3), SQLite. At every reservation
    (each in a short transaction of its own on a second connection), every settlement, and every
    provider call of a retrying call (the priced provider answers 503 twice: the service's
    ``llm_max_retries`` = 2 retries, three attempts), no connection holds any lock: not the
    caller's own session (the capture request's, the generation job's), not a read transaction,
    not a writer. A ``BEGIN EXCLUSIVE`` with no busy timeout succeeds at each point. The journal
    mode the application's engine factory leaves is the rollback journal (``delete``)."""
    if _is_postgres(base):
        pytest.skip("SQLite's locks")
    w = _org(base, f"o2p{flow[0]}{flow[-1]}")
    db_file = w.engine.url.database
    seen: dict[str, list[str]] = {"reservation": [], "settlement": [], "provider call": []}

    def probing(kind: str, real: Callable) -> Callable:
        def probed(*args, **kwargs):
            seen[kind].append(_nothing_held(db_file))
            return real(*args, **kwargs)

        return probed

    monkeypatch.setattr(ledger, "reserve", probing("reservation", ledger.reserve))
    monkeypatch.setattr(ledger, "settle", probing("settlement", ledger.settle))
    priced = flow.endswith("priced")
    from app.tests._llm_offline import offline_settings

    # Every attempt of a retrying call: the priced service's own ``llm_max_retries`` + 1.
    attempts = offline_settings().llm_max_retries + 1 if priced else 1
    if flow.startswith("capture"):
        service, _requests = _priced_capture_service([503] * (attempts - 1))
    else:
        service, _requests = _priced_creative_service([503] * (attempts - 1))
    if priced:
        _use_service(monkeypatch, service)
        provider = service._provider
    else:
        provider = llm_module.get_llm_service()._provider
    monkeypatch.setattr(provider, "generate", probing("provider call", provider.generate))
    if flow.startswith("capture"):
        opportunity_id, record_id = _capturable(w)
        outcome = _answer(_capture(w, opportunity_id, record_id))
        record = _ledger(w)
    else:
        brief_id = w.seed_brief(w.ws)
        job_id = _generate(w, brief_id)
        w.drain()
        outcome = w.job(job_id)["status"]
        record = _ledger(w, job_id=job_id)
    with w.engine.connect() as conn:
        journal = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    _report(f"O-F123-2 precondition {flow}", {
        "journal mode": journal,
        "outcome": outcome,
        **seen,
        "reservations": len(record["reservations (state, reserved, settled)"]),
        "usage rows": record["usage rows"],
    }, {
        "journal mode": "delete",
        "outcome": (201,) if flow.startswith("capture") else "succeeded",
        "reservation": ["free"] * attempts,
        "settlement": ["free"] * attempts,
        "provider call": ["free"] * attempts,
        "reservations": attempts,
        "usage rows": attempts,
    })


def _capture_accounting(w: CreativeWorld, opportunity_id: str, answer: tuple) -> tuple[dict, dict]:
    """What the capture left, and what the operator's item requires of it: its one admitted call
    accounted for exactly once; a brief stored exactly when the capture answered 201."""
    state = _capture_state(w, opportunity_id)
    observed = {
        "capture": answer,
        "captured briefs": len(state["captured briefs (version, by the marketer)"]),
        "usage rows of the capture": state["usage rows of the capture"],
        "ledger reservations of the workspace": state["ledger reservations of the workspace"],
    }
    expected = {
        "capture": answer,
        "captured briefs": 1 if answer[:1] == (201,) else 0,
        "usage rows of the capture": 1,
        "ledger reservations of the workspace": 1,
    }
    return observed, expected


@pytest.mark.parametrize("where", ["capture", "generation"])
def test_o2_postgresql_a_period_row_lock_across_the_usage_write_is_waited_out(
    base: CreativeWorld, monkeypatch, where: str
):
    """The PostgreSQL analogue, mock provider (the same-session meter: the usage rows join the
    request's or the job's transaction). Another transaction holds the workspace's
    ``llm_budget_periods`` row ``FOR UPDATE`` from before the model call returns until past
    SQLite's busy timeout. The usage write waits at its period update (no timeout on PostgreSQL),
    then commits: the brief or the draft is stored and the usage row and reservation are there
    once."""
    if not _is_postgres(base):
        pytest.skip("PostgreSQL's row locks")
    w = _org(base, f"o2p{where[0]}")
    now = datetime.now(UTC)
    with w.session() as s:
        ledger.ensure_period(s, workspace_id=w.ws, organization_id=w.org,
                             period_start=ledger.period_start_for(now), now=now)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    if where == "capture":
        opportunity_id, record_id = _capturable(w)
        running = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    else:
        brief_id = w.seed_brief(w.ws)
        job_id = _generate(w, brief_id)
        running = _in_flight(w.drain, gate, "the generation")
    holder = _PgRowHold(w, "SELECT id FROM llm_budget_periods WHERE workspace_id = :ws FOR UPDATE",
                        {"ws": w.ws})
    try:
        gate.release.set()
        deadline = time.monotonic() + 30
        while not _lock_waits(w) and running.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        waits = _lock_waits(w)
        time.sleep(PAST_ONE_WAIT - 1.0)
        still = _lock_waits(w)
    finally:
        gate.release.set()
        holder.release()
    outcome = running.outcome()
    if where == "capture":
        observed, expected = _capture_accounting(w, opportunity_id, _answer(outcome))
        expected = {**expected, "capture": (201,), "captured briefs": 1}
    else:
        state = _generation_state(w, brief_id, job_id)
        observed = {"job": state["job"], "drafts": len(state["drafts (brief, version)"]),
                    **_usage_of_job(w, job_id)}
        expected = {"job": GENERATION_STORED, "drafts": 1, "usage rows of the job": 1,
                    "ledger reservations of the job": 1}
    print(f"\nWAITED AT {waits}")
    _report(f"O-F123-2 PostgreSQL {where}", {
        "it waited at the period row": len(waits) == 1 and "llm_budget_periods" in waits[0].lower(),
        f"still waiting {PAST_ONE_WAIT - 1.0} s later": still == waits,
        **observed,
    }, {
        "it waited at the period row": True,
        f"still waiting {PAST_ONE_WAIT - 1.0} s later": True,
        **expected,
    })


def test_o2_postgresql_the_priced_shared_ledger_settlement_waits_out_a_period_row_lock(
    base: CreativeWorld, monkeypatch
):
    """The PostgreSQL shared ledger (the offline priced provider). The capture's reservation is
    committed before the call; while the provider is answering, another transaction takes the
    period row ``FOR UPDATE`` and keeps it past SQLite's busy timeout. The settlement (its own
    transaction, after the call) waits at the period lock, then commits: 201, one brief, one priced
    usage row, one settled reservation. The capture's own transaction is not involved."""
    if not _is_postgres(base):
        pytest.skip("the shared ledger (a priced provider's meter) is PostgreSQL's")
    from app.tests.test_briefs_postgres import _priced

    w = _org(base, "o2s")
    opportunity_id, record_id = _capturable(w)
    priced, _settings, _bodies = _priced()
    gate = _Gate()
    monkeypatch.setattr(priced._provider, "generate", gate.before(priced._provider.generate))
    monkeypatch.setattr(llm_module.get_llm_service(), "run", priced.run)
    capturing = _in_flight(lambda: _capture(w, opportunity_id, record_id), gate, "the capture")
    holder = _PgRowHold(w, "SELECT id FROM llm_budget_periods WHERE workspace_id = :ws FOR UPDATE",
                        {"ws": w.ws})
    try:
        gate.release.set()
        deadline = time.monotonic() + 30
        while not _lock_waits(w) and capturing.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        waits = _lock_waits(w)
        time.sleep(PAST_ONE_WAIT - 1.0)
        still = _lock_waits(w)
    finally:
        gate.release.set()
        holder.release()
    captured = capturing.outcome()
    observed, expected = _capture_accounting(w, opportunity_id, _answer(captured))
    usage = w.rows(LLMUsageEvent, workspace_id=w.ws, task=brief_service.BRIEF_TASK)
    print(f"\nWAITED AT {waits}")
    _report("O-F123-2 PostgreSQL priced", {
        "the settlement waited at the period lock": len(waits) == 1 and waits[0].lower().startswith(
            "select llm_budget_periods.id from llm_budget_periods") and waits[0].lower().endswith(
            "for update"),
        f"still waiting {PAST_ONE_WAIT - 1.0} s later": still == waits,
        "the reservations' states": sorted(
            r["state"] for r in w.rows(LLMBudgetReservation, workspace_id=w.ws)),
        "the usage row is priced": [u["cost_usd"] > 0 for u in usage],
        **observed,
    }, {
        "the settlement waited at the period lock": True,
        f"still waiting {PAST_ONE_WAIT - 1.0} s later": True,
        "the reservations' states": ["settled"],
        "the usage row is priced": [True],
        **{**expected, "capture": (201,), "captured briefs": 1},
    })


LEDGER_TABLES = ("llm_budget_reservations", "llm_budget_periods", "llm_usage_events")


def _ledger_writes(statements: list[str]) -> list[str]:
    """``verb table`` of each write to a ledger table, in order."""
    out = []
    for q in statements:
        words = q.split()
        if not words or words[0] not in ("insert", "update", "delete"):
            continue
        table = words[2] if words[0] in ("insert", "delete") else words[1]
        if table in LEDGER_TABLES:
            out.append(f"{words[0]} {table}")
    return out


@pytest.mark.parametrize("flow", ["capture-mock", "capture-priced", "generation-mock",
                                  "generation-priced"])
def test_o2_postgresql_capture_and_generation_keep_their_ledger_paths(
    base: CreativeWorld, monkeypatch, flow: str
):
    """PostgreSQL is unchanged by Design B (coordinator ruling F123R-1 §2). With the mock, nothing
    is written to the ledger before the provider is asked, and the call's rows are written after
    it (the same-session meter, in the caller's transaction). With the offline priced provider,
    the reservation is written before the call and the settlement with the usage row after it
    (the shared ledger). Printed: every statement, for the before/after statement-list diff."""
    if not _is_postgres(base):
        pytest.skip("PostgreSQL's paths")
    w = _org(base, f"o2q{flow[0]}{flow[-1]}")
    priced = flow.endswith("priced")
    if priced:
        service = (_priced_capture_service([]) if flow.startswith("capture")
                   else _priced_creative_service([]))[0]
        _use_service(monkeypatch, service)
        provider = service._provider
    else:
        provider = llm_module.get_llm_service()._provider
    if flow.startswith("generation"):
        brief_id = w.seed_brief(w.ws)
        job_id = _generate(w, brief_id)
    else:
        opportunity_id, record_id = _capturable(w)
    with _Trace(w.engine) as trace:
        real = provider.generate

        def marked(*args, **kwargs):
            with trace._lock:
                trace.sent.append(PROVIDER_CALLED)
            return real(*args, **kwargs)

        monkeypatch.setattr(provider, "generate", marked)
        if flow.startswith("generation"):
            assert w.drain() == 1
            outcome = w.job(job_id)["status"]
        else:
            outcome = _answer(_capture(w, opportunity_id, record_id))
    sent = trace.sent
    called = sent.index(PROVIDER_CALLED)
    print(f"\nPG STATEMENTS {flow}:\n" + "\n".join(f"  {q[:200]}" for q in sent))
    _report(f"O-F123-2 PostgreSQL ledger path {flow}", {
        "outcome": outcome,
        "ledger writes before the call": _ledger_writes(sent[:called]),
        "ledger writes after the call": _ledger_writes(sent[called + 1:]),
    }, {
        "outcome": (201,) if flow.startswith("capture") else "succeeded",
        "ledger writes before the call": (
            ["insert llm_budget_periods", "update llm_budget_periods",
             "insert llm_budget_reservations"] if priced else []),
        "ledger writes after the call": (
            ["update llm_budget_reservations", "update llm_budget_periods",
             "insert llm_usage_events"] if priced else
            ["insert llm_budget_reservations", "insert llm_budget_periods",
             "update llm_budget_periods", "insert llm_usage_events"]),
    })


# --------------------------------------------------------------------------- #
# O-F123-2, the scout-run handler on SQLite (its own seeded world)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def scout_lite(tmp_path_factory) -> Iterator[tuple[sessionmaker, str]]:
    """The four-market demo seed on a SQLite file opened by the application's own engine factory
    (busy timeout five seconds), with no seeded runs."""
    db_file = tmp_path_factory.mktemp("f123r_scout") / "scout.db"
    engine = build_engine(Settings(database_url=f"sqlite:///{db_file}"))
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        clear_seeded_runs(make)
        yield make, str(db_file)
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


def _queue_scout(factory, market: str) -> str:
    from app.jobs.service import enqueue_scout_request
    from app.scouting_requests.models import ScoutRequest

    rid = seed_mod.sid("scout", market)
    with factory() as s:
        req = s.get(ScoutRequest, rid)
        req.status = "queued"
        job = enqueue_scout_request(
            s, organization_id=req.organization_id, workspace_id=req.workspace_id,
            scout_request_id=rid, location_id=req.location_id, request_id=rid,
            run_fence=req.run_fence,
        )
        s.commit()
        return job.id


def _count_model_calls(monkeypatch) -> dict:
    calls = {"model": 0}
    provider = llm_module.get_llm_service()._provider
    real = provider.generate

    def counted(*args, **kwargs):
        calls["model"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(provider, "generate", counted)
    return calls


def _scout_poll(factory) -> Callable[[], bool]:
    runner = JobRunner(settings=get_settings(), session_factory=factory, store=DurableJobStore())
    return lambda: runner.poll_once(worker_id="w-f123r-scout")


def _scout_state(factory, job_id: str, market: str) -> dict:
    with factory() as s:
        job = s.get(Job, job_id)
        return {
            "job": (job.status, job.last_error_code, job.last_error_summary),
            "usage rows of the job": s.scalar(
                select(func.count()).select_from(LLMUsageEvent).where(
                    LLMUsageEvent.job_id == job_id)),
            "raw signals of the request": s.scalar(
                select(func.count()).select_from(RawSignal).where(
                    RawSignal.scout_request_id == seed_mod.sid("scout", market))),
        }


def test_o2_sqlite_a_writer_holding_the_lock_when_a_scout_run_starts_writing_costs_no_call(
    scout_lite, monkeypatch
):
    """SQLite, the scout-run handler under the real worker. The run's transaction takes the writer
    lock at its first raw-signal insert, before its first model call, and keeps it through every
    call to its commit, so another writer can only be there first. Held there past the busy
    timeout: the insert fails, the attempt rolls back before any model call, and nothing was
    admitted, so nothing can be lost (a demonstration; it holds today)."""
    factory, db_file = scout_lite
    job_id = _queue_scout(factory, "dallas")
    raw_before = _scout_state(factory, job_id, "dallas")["raw signals of the request"]
    calls = _count_model_calls(monkeypatch)
    gate = _Gate()
    monkeypatch.setattr(RunAdmission, "begin_writes", gate.before(RunAdmission.begin_writes))
    polling = _in_flight(_scout_poll(factory), gate, "the scout run")
    raw = sqlite3.connect(db_file, timeout=0, isolation_level=None, check_same_thread=False)
    raw.execute("BEGIN IMMEDIATE")
    released_at = time.monotonic()
    gate.release.set()
    timer = threading.Timer(PAST_ONE_WAIT, lambda: raw.execute("ROLLBACK"))
    timer.start()
    try:
        polled = polling.outcome()
    finally:
        timer.join()
        raw.close()
    state = _scout_state(factory, job_id, "dallas")
    print(f"\nTIMING the attempt ended {time.monotonic() - released_at:.2f} s after release")
    _report("O-F123-2 scout run, a writer first", {
        "polled": polled, "model calls": calls["model"], "job": state["job"],
        "usage rows of the job": state["usage rows of the job"],
        "raw signals written by the run": state["raw signals of the request"] - raw_before,
    }, {
        "polled": True, "model calls": 0,
        "job": ("retry_wait", "transient", "OperationalError"),
        "usage rows of the job": 0, "raw signals written by the run": 0,
    })
    with factory() as s:
        s.execute(text("UPDATE jobs SET status = 'failed' WHERE id = :j"), {"j": job_id})
        s.commit()


@pytest.mark.xfail(strict=True, reason=SCOUT_LIMIT)
def test_o2_sqlite_a_reader_holds_a_scout_runs_commit_past_its_after_rollback_write(
    scout_lite, monkeypatch
):
    """SQLite, the scout-run handler under the real worker, at commit. The run has made its model
    calls and written their usage rows with its findings (it holds the writer lock); a reader then
    opens a read transaction on another connection and keeps it past two busy-timeout waits. The
    run's commit fails, the worker's after-rollback write of the usage rows fails the same way,
    and the job is failed for good. Every admitted call's usage must still be kept exactly once;
    today none is."""
    factory, db_file = scout_lite
    job_id = _queue_scout(factory, "london")
    calls = _count_model_calls(monkeypatch)
    gate = _Gate()
    monkeypatch.setattr(RunAdmission, "keep_writes", gate.after(RunAdmission.keep_writes))
    polling = _in_flight(_scout_poll(factory), gate, "the scout run")
    raw = sqlite3.connect(db_file, timeout=0, isolation_level=None, check_same_thread=False)
    raw.execute("BEGIN")
    raw.execute("SELECT count(*) FROM jobs").fetchall()
    released_at = time.monotonic()
    gate.release.set()
    timer = threading.Timer(PAST_TWO_WAITS, lambda: raw.execute("ROLLBACK"))
    timer.start()
    try:
        polled = polling.outcome()
    finally:
        timer.join()
        raw.close()
    state = _scout_state(factory, job_id, "london")
    print(f"\nTIMING the attempt ended {time.monotonic() - released_at:.2f} s after release; "
          f"job {state['job']}; model calls {calls['model']}")
    with factory() as s:
        s.execute(text("UPDATE jobs SET status = 'failed' WHERE id = :j"), {"j": job_id})
        s.commit()
    _report("O-F123-2 scout run, a reader at commit", {
        "polled": polled, "model calls made": calls["model"] > 0,
        "usage rows = model calls": state["usage rows of the job"] == calls["model"],
    }, {
        "polled": True, "model calls made": True, "usage rows = model calls": True,
    })


# =========================================================================== #
# O-F123-3: the real worker loop recovers after the competing writer lets go
# =========================================================================== #
class _LoopWatch:
    """The real worker process loop (``Worker._loop``, one slot), with each of its polls
    recorded: what it returned or which exception the loop caught and survived."""

    def __init__(self, w: CreativeWorld, monkeypatch, store: DurableJobStore | None = None):
        settings = get_settings()
        monkeypatch.setattr(settings, "worker_poll_interval_seconds", 0.2)
        monkeypatch.setattr(settings, "worker_concurrency", 1)
        self.worker = Worker(settings=settings, session_factory=w.factory,
                             store=store or DurableJobStore())
        self.polls: list[str] = []
        runner = self.worker._runner
        real = runner.poll_once

        def watched(**kwargs):
            try:
                ran = real(**kwargs)
            except Exception as exc:
                self.polls.append(type(exc).__name__)
                raise
            self.polls.append("ran" if ran else "idle")
            return ran

        monkeypatch.setattr(runner, "poll_once", watched)

    def __enter__(self) -> _LoopWatch:
        self.worker.start()
        return self

    def __exit__(self, *exc) -> None:
        self.worker.request_stop()
        self.worker.join(grace=15.0)

    def alive(self) -> bool:
        return all(t.is_alive() for t in self.worker._threads)


def _hold_the_claim_after_its_update(w: CreativeWorld, request) -> _Gate:
    """The worker's claim is held once, right after its ``UPDATE jobs SET status='claimed'`` has
    run on the application engine and before the claim commits: the claiming transaction then
    holds what that UPDATE took (SQLite: the writer lock, from inside the claim's SAVEPOINT;
    PostgreSQL: the job row, locked by its ``FOR UPDATE SKIP LOCKED``)."""
    gate = _Gate()

    def after(conn, cursor, statement, parameters, context, executemany) -> None:
        lowered = " ".join(statement.split()).lower()
        if (lowered.startswith("update jobs set status") and "'claimed'" in repr(parameters)
                and gate._first()):
            gate._hold()

    event.listen(w.engine, "after_cursor_execute", after)
    request.addfinalizer(lambda: event.remove(w.engine, "after_cursor_execute", after))
    return gate


def _until_terminal(w: CreativeWorld, job_id: str, seconds: float = 40.0) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = w.job(job_id)["status"]
        if status in TERMINAL:
            return status
        time.sleep(0.1)
    raise AssertionError(f"the job is still {w.job(job_id)['status']} after {seconds} s")


def _ran_once(w: CreativeWorld, brief_id: str, job_id: str) -> dict:
    return {
        **_job_summary(w, job_id),
        "drafts": w.count(CreativeDraft, brief_id=brief_id),
        **_usage_of_job(w, job_id),
    }


RAN_ONCE = {
    "job": GENERATION_STORED,
    "attempts": 1,
    "lease left": (None, None, None),
    "drafts": 1,
    "usage rows of the job": 1,
    "ledger reservations of the job": 1,
}


@pytest.mark.parametrize("released", ["within the busy timeout", "past the busy timeout"])
def test_o3_sqlite_a_writer_holds_the_lock_while_the_real_worker_polls(
    base: CreativeWorld, monkeypatch, released: str
):
    """(a) SQLite. A generation job is queued; another connection holds the writer lock; the real
    worker loop starts polling. Its claim (the poll's first write) waits the busy timeout. The
    holder lets go within it (the same poll claims the job) or past it (the claim fails with
    ``database is locked``; the loop survives and polls again). Either way the job is then claimed
    and run once, automatically: one attempt, one draft, one usage row, no lease left."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; PostgreSQL's analogue skips the locked row")
    w = _org(base, "o3a")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    holder = _SqliteHold(w).take()
    seconds = WITHIN_ONE_WAIT if released == "within the busy timeout" else PAST_ONE_WAIT
    with _LoopWatch(w, monkeypatch) as loop:
        timer = holder.release_at(holder.taken_at + seconds)
        try:
            status = _until_terminal(w, job_id)
        finally:
            timer.join()
            holder.release()
        alive = loop.alive()
    failed_polls = [p for p in loop.polls if p not in ("ran", "idle")]
    _report(f"O-F123-3 (a) {released}", {
        "terminal": status,
        "the loop kept running": alive,
        "polls that failed (each caught by the loop)": (
            "none" if not failed_polls
            else "all database-locked" if set(failed_polls) == {"OperationalError"}
            else failed_polls),
        "polls that ran a job": loop.polls.count("ran"),
        "events": _job_events(w, job_id),
        **_ran_once(w, brief_id, job_id),
    }, {
        "terminal": "succeeded",
        "the loop kept running": True,
        "polls that failed (each caught by the loop)": (
            "none" if released == "within the busy timeout" else "all database-locked"),
        "polls that ran a job": 1,
        "events": ["claimed", "started", "succeeded"],
        **RAN_ONCE,
    })


def test_o3_sqlite_a_writer_between_the_claim_and_the_start_is_recovered_by_lease_expiry(
    base: CreativeWorld, monkeypatch
):
    """(a), the second window, SQLite. The worker's claim has committed; before the attempt is
    marked running, another connection takes the writer lock past the busy timeout. Marking it
    running fails (``database is locked``); the loop survives; the job stays claimed with a lease
    and no attempt counted. Once its lease expires, a later poll returns it to the queue, claims it
    again and runs it once: one attempt, one draft, one usage row, no lease left."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock")
    settings = get_settings()
    monkeypatch.setattr(settings, "worker_lease_seconds", 3.0)
    monkeypatch.setattr(settings, "worker_heartbeat_seconds", 1.0)
    w = _org(base, "o3m")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    store = DurableJobStore()
    gate = _Gate()
    monkeypatch.setattr(store, "mark_running", gate.before(store.mark_running))
    holder = _SqliteHold(w)
    with _LoopWatch(w, monkeypatch, store) as loop:
        gate.wait("the start")
        holder.take()
        gate.release.set()
        timer = holder.release_at(holder.taken_at + PAST_ONE_WAIT)
        try:
            status = _until_terminal(w, job_id)
        finally:
            timer.join()
            holder.release()
        alive = loop.alive()
    failed_polls = [p for p in loop.polls if p not in ("ran", "idle")]
    _report("O-F123-3 (a) between the claim and the start", {
        "terminal": status,
        "the loop kept running": alive,
        "failed polls are all database-locked": bool(failed_polls) and set(failed_polls) == {
            "OperationalError"},
        "events": _job_events(w, job_id),
        **_ran_once(w, brief_id, job_id),
    }, {
        "terminal": "succeeded",
        "the loop kept running": True,
        "failed polls are all database-locked": True,
        "events": ["claimed", "lease_recovered", "claimed", "started", "succeeded"],
        **RAN_ONCE,
    })


def test_o3_sqlite_the_claim_commits_first_and_the_competing_writer_waits(
    base: CreativeWorld, monkeypatch, request
):
    """(b) SQLite. The real worker loop's claim is held after its UPDATE, before its commit (it
    holds the writer lock). Another writer, opened as the application opens SQLite, asks for the
    lock and must wait; the claim commits, the other writer gets the lock and lets go, and the job
    runs once."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock")
    w = _org(base, "o3b")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _hold_the_claim_after_its_update(w, request)
    store = DurableJobStore()
    competing = _Runner(_competing_writer(w.engine.url.database))
    with _LoopWatch(w, monkeypatch, store) as loop:
        gate.wait("the claim")
        try:
            competing.start()
            time.sleep(1.5)
            still_waiting = competing.is_alive()
        finally:
            gate.release.set()
        other = competing.outcome()
        status = _until_terminal(w, job_id)
        alive = loop.alive()
    _report("O-F123-3 (b)", {
        "the other writer was still waiting after 1.5 s": still_waiting,
        "the other writer got the lock": other["got the lock"],
        "terminal": status,
        "the loop kept running": alive,
        "events": _job_events(w, job_id),
        **_ran_once(w, brief_id, job_id),
    }, {
        "the other writer was still waiting after 1.5 s": True,
        "the other writer got the lock": True,
        "terminal": "succeeded",
        "the loop kept running": True,
        "events": ["claimed", "started", "succeeded"],
        **RAN_ONCE,
    })


def test_o3_postgresql_a_locked_job_row_is_skipped_and_claimed_once_released(
    base: CreativeWorld, monkeypatch
):
    """(a), the PostgreSQL analogue. Another transaction holds the queued job's row ``FOR
    UPDATE``. The real worker loop's claim (``FOR UPDATE SKIP LOCKED``) skips it and the loop goes
    on polling idle, with no error; once the row is released the next poll claims the job and
    runs it once."""
    if not _is_postgres(base):
        pytest.skip("PostgreSQL's row locks")
    w = _org(base, "o3p")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    holder = _PgRowHold(w, "SELECT id FROM jobs WHERE id = :j FOR UPDATE", {"j": job_id})
    with _LoopWatch(w, monkeypatch) as loop:
        try:
            time.sleep(PAST_ONE_WAIT - 4.0)
            during = list(loop.polls)
            queued = w.job(job_id)["status"]
        finally:
            holder.release()
        status = _until_terminal(w, job_id)
        alive = loop.alive()
    _report("O-F123-3 PostgreSQL (a)", {
        "while held: the job": queued,
        "while held: polls were idle, none failed": bool(during) and set(during) == {"idle"},
        "terminal": status,
        "the loop kept running": alive,
        "events": _job_events(w, job_id),
        **_ran_once(w, brief_id, job_id),
    }, {
        "while held: the job": "pending",
        "while held: polls were idle, none failed": True,
        "terminal": "succeeded",
        "the loop kept running": True,
        "events": ["claimed", "started", "succeeded"],
        **RAN_ONCE,
    })


def test_o3_postgresql_the_claim_commits_first_and_the_competing_transaction_waits(
    base: CreativeWorld, monkeypatch, request
):
    """(b), the PostgreSQL analogue. The real worker loop's claim is held after its UPDATE, before
    its commit (it holds the job row). Another transaction asks for the row ``FOR UPDATE`` and must
    wait; the claim commits, the other transaction gets the row and lets go, and the job runs
    once."""
    if not _is_postgres(base):
        pytest.skip("PostgreSQL's row locks")
    w = _org(base, "o3q")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _hold_the_claim_after_its_update(w, request)
    store = DurableJobStore()

    def compete() -> str:
        holder = _PgRowHold(w, "SELECT id, status FROM jobs WHERE id = :j FOR UPDATE",
                            {"j": job_id})
        holder.release()
        return "got the row"

    competing = _Runner(compete)
    with _LoopWatch(w, monkeypatch, store) as loop:
        gate.wait("the claim")
        try:
            competing.start()
            deadline = time.monotonic() + 30
            while not _lock_waits(w) and time.monotonic() < deadline:
                time.sleep(0.05)
            waits = _lock_waits(w)
        finally:
            gate.release.set()
        other = competing.outcome()
        status = _until_terminal(w, job_id)
        alive = loop.alive()
    _report("O-F123-3 PostgreSQL (b)", {
        "the other transaction waited at the job row": len(waits) == 1 and waits[
            0].lower().startswith("select id, status from jobs where id ="),
        "the other transaction": other,
        "terminal": status,
        "the loop kept running": alive,
        "events": _job_events(w, job_id),
        **_ran_once(w, brief_id, job_id),
    }, {
        "the other transaction waited at the job row": True,
        "the other transaction": "got the row",
        "terminal": "succeeded",
        "the loop kept running": True,
        "events": ["claimed", "started", "succeeded"],
        **RAN_ONCE,
    })
