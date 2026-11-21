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

**O-F123-2's open cases** are marked ``xfail(strict=True)``: each asserts what the operator's item
requires (the admitted call's usage kept exactly once) and fails on the product today. No product
change for O-F123-2 is in this module's change: the lane's hand-off presents the design for a
ruling (see its reason string). A fix that makes one pass turns it into a strict XPASS failure,
so its marker must then be removed.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

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
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
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
#: Why the O-F123-2 cases that lose usage today are strict xfails, not fixed here.
O2_OPEN = (
    "O-F123-2: on SQLite an admitted model call's usage rows are written after the call; a "
    "writer or reader that keeps the lock past every bounded wait makes them disappear. Closing "
    "every such interleaving needs a durable pre-call record (an accounting-policy change) or a "
    "durable after-the-request writer (a new subsystem): the design is presented for a ruling "
    "and is not implemented (lane F123R hand-off)."
)
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
    the job's writer lock would read the membership as it was and store the draft.

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
        "the job waited at": "insert llm_budget_reservations",
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
# O-F123-2: an admitted call's usage under contention
# =========================================================================== #
def _capture_with_the_model_call_held(w: CreativeWorld, monkeypatch) -> tuple[str, _Gate, _Runner]:
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    capturing = _in_flight(_timed(lambda: _capture(w, opportunity_id, record_id)), gate,
                           "the capture")
    return opportunity_id, gate, capturing


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


@pytest.mark.parametrize("released", [
    "within the busy timeout",
    pytest.param("past the busy timeout", marks=pytest.mark.xfail(strict=True, reason=O2_OPEN)),
])
def test_o2_sqlite_a_writer_holds_the_lock_across_the_captures_usage_write(
    base: CreativeWorld, monkeypatch, released: str
):
    """(a) SQLite, brief capture. While the capture is in its model call another connection takes
    the writer lock. The admitted call returns; the capture's first write (its usage rows) must
    wait. The holder lets go within the busy timeout, or past it. Either way the admitted call's
    usage row and reservation must be kept exactly once (and a brief stored only with a 201).
    Within the timeout this holds today; past it the capture answers the generic 500 and its usage
    rows go with the rollback (FX-A c3, F123 cap 028)."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; PostgreSQL's analogue is the period-row test")
    w = _org(base, "o2c")
    opportunity_id, gate, capturing = _capture_with_the_model_call_held(w, monkeypatch)
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
    observed, expected = _capture_accounting(w, opportunity_id, _answer(response))
    print(f"\nTIMING capture answered {ended - released_at:.2f} s after the call returned; "
          f"holder released after {holder.released_at - released_at:.2f} s")
    _report(f"O-F123-2 capture (a) {released}", {"the capture waited at": waited_at, **observed},
            {"the capture waited at": "insert llm_budget_reservations", **expected})


def test_o2_sqlite_the_capture_holds_the_lock_first_and_the_competing_writer_waits(
    base: CreativeWorld, monkeypatch
):
    """(b) SQLite, brief capture. The capture has written its usage rows (it holds the writer lock)
    and is held before its author re-read. Another writer, opened as the application opens SQLite,
    asks for the lock and must wait; the capture commits (201, one brief, its usage row and
    reservation once), then the other writer gets the lock."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock")
    w = _org(base, "o2b")
    opportunity_id, record_id = _capturable(w)
    gate = _Gate()
    monkeypatch.setattr(brief_service, "_reread_author_role",
                        gate.before(brief_service._reread_author_role))
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
    observed, expected = _capture_accounting(w, opportunity_id, _answer(captured))
    _report("O-F123-2 capture (b)", {
        "the other writer was still waiting after 1.5 s": still_waiting,
        "the other writer got the lock": other["got the lock"],
        "it waited at least 1.5 s": other["waited"] >= 1.5,
        **observed,
    }, {
        "the other writer was still waiting after 1.5 s": True,
        "the other writer got the lock": True,
        "it waited at least 1.5 s": True,
        **{**expected, "capture": (201,), "captured briefs": 1},
    })


@pytest.mark.xfail(strict=True, reason=O2_OPEN)
def test_o2_sqlite_a_reader_holds_the_captures_commit_past_the_busy_timeout(
    base: CreativeWorld, monkeypatch
):
    """(c) SQLite, brief capture, at commit. The capture has written everything (its usage rows,
    the brief, its audit row) and holds the writer lock; a reader then opens a read transaction on
    another connection. The capture's commit needs every reader gone: it waits the busy timeout,
    then fails. The admitted call's usage must still be kept exactly once. Today the capture
    answers the generic 500 and its usage rows go with the rollback."""
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
    observed, expected = _capture_accounting(w, opportunity_id, _answer(captured))
    _report("O-F123-2 capture (c) reader at commit", observed, expected)


@pytest.mark.parametrize("released", [
    "past the usage write's wait",
    pytest.param("past the after-rollback write's wait",
                 marks=pytest.mark.xfail(strict=True, reason=O2_OPEN)),
])
def test_o2_sqlite_a_writer_holds_the_lock_across_the_generation_jobs_usage_write(
    base: CreativeWorld, monkeypatch, released: str
):
    """(a) SQLite, the generation job under the real worker. While the job is in its model call
    another connection takes the writer lock. The job's first write after the call (its usage rows)
    fails after the busy timeout; the worker rolls the attempt back and re-writes the usage rows in
    a fresh transaction (its after-rollback write), which waits the busy timeout again.

    Released between the two waits (FX-A c3's case): the after-rollback write keeps the usage once
    and the job waits for a retry. Released only after both: the after-rollback write fails too,
    the worker fails the job for good (``after_rollback_write_failed``), and the admitted call's
    usage must still be kept once; today it is not."""
    if _is_postgres(base):
        pytest.skip("SQLite's writer lock; PostgreSQL's analogue is the period-row test")
    w = _org(base, "o2g")
    brief_id = w.seed_brief(w.ws)
    job_id = _generate(w, brief_id)
    gate = _Gate()
    _hold_the_model_call(monkeypatch, gate, after=False)
    generating = _in_flight(w.drain, gate, "the generation")
    holder = _SqliteHold(w).take()
    seconds = PAST_ONE_WAIT if released == "past the usage write's wait" else PAST_TWO_WAITS
    released_at = time.monotonic()
    gate.release.set()
    timer = holder.release_at(released_at + seconds)
    try:
        drained = generating.outcome()
    finally:
        timer.join()
        holder.release()
    ended = time.monotonic()
    summary = _job_summary(w, job_id)
    usage = _usage_of_job(w, job_id)
    drafts = w.count(CreativeDraft, brief_id=brief_id)
    _retire_unfinished_jobs(w)
    print(f"\nTIMING the attempt ended {ended - released_at:.2f} s after the call returned; "
          f"job {summary['job']}")
    _report(f"O-F123-2 generation {released}", {
        "drained": drained, "drafts": drafts, **usage,
    }, {
        "drained": 1, "drafts": 0,
        "usage rows of the job": 1, "ledger reservations of the job": 1,
    })
    if released == "past the usage write's wait":
        assert summary["job"] == ("retry_wait", "transient", "OperationalError"), summary


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


@pytest.mark.xfail(strict=True, reason=O2_OPEN)
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
