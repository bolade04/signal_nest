"""D-25 blocked-Scout recovery (PROPOSED) on PostgreSQL: the lock-order fix, persisted
deadlines, reconciliation's gate, the derived state, the Run-now refusal and every Cancel shape.

Contract D-25-RECOVERY-CONTRACT-01 §3-§5 as refined in §9; nothing here is a ruling. The harness
is ``test_scout_pause_postgres``'s (an Alembic-migrated template, a clone per test, the real
worker in threads with their own connections, the route functions with a session of their own,
offline fakes only). Cancel shapes are numbered as in lane W15's probe ``raw/002``.

Gate: ``require_postgres`` (fails, never skips, under ``CI=true``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, text, update

from app.audit.models import AuditLog
from app.core.config import get_settings
from app.core.errors import ValidationDomainError
from app.jobs import pipeline
from app.jobs.admission import RunAdmission
from app.jobs.models import Job
from app.jobs.reconcile import ReconciliationRefused, list_unresolved, resolve
from app.jobs.routes import cancel_job
from app.jobs.service import enqueue_scout_request
from app.jobs.store import DurableJobStore
from app.llm.mock import MockLLMProvider
from app.llm.service import LLMService
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.routes import (
    get_scout_request,
    resume_scout_request,
    run_scout_request_endpoint,
)
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests._llm_offline import offline_settings
from app.tests.test_scout_execution_controls_postgres import Provider, _expire_lease, _use
from app.tests.test_scout_pause_postgres import (
    API_DIR,
    JOIN_SECONDS,
    Sources,
    _end_state,
    _runner,
    _start_worker,
)

template_url = harness.template_url
world = harness.world


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mock_llm(monkeypatch) -> None:
    monkeypatch.setattr(
        pipeline,
        "llm_service",
        LLMService(
            MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed),
            settings=offline_settings(),
        ),
    )


class GateSources(Sources):
    """Fixture sources whose call ``i`` (0-based, ``i < n``) parks until ``release_i[i]``."""

    def __init__(self, n: int):
        super().__init__()
        self.started_i = [threading.Event() for _ in range(n)]
        self.release_i = [threading.Event() for _ in range(n)]
        self._n, self._lock = 0, threading.Lock()

    def __call__(self, **kw):
        with self._lock:
            i, self._n = self._n, self._n + 1
        if i < len(self.started_i):
            self.started_i[i].set()
            assert self.release_i[i].wait(JOIN_SECONDS), f"fetch {i} never released"
        return super().__call__(**kw)


def _await(predicate, what: str, timeout: float = JOIN_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _request(w) -> tuple:
    with w.factory() as s:
        r = s.get(ScoutRequest, w.rid)
        return r.status, r.run_fence, r.owner_job_id


def _job(w, job_id: str) -> Job:
    with w.factory() as s:
        return s.get(Job, job_id)


def _accounting(w) -> dict:
    return {k: str(v) for k, v in harness._ledger(w).items()}


def _admissions(w) -> list[tuple]:
    with w.observer.connect() as c:
        return [
            tuple(r)
            for r in c.execute(
                text(
                    "select job_id, attempt, end_kind, resolved_at is not null"
                    " from scout_admissions where scout_request_id=:r order by admitted_at"
                ),
                {"r": w.rid},
            )
        ]


def _unresolved(w) -> int:
    return sum(1 for a in _admissions(w) if not a[3])


def _cancel(w, job_id: str) -> str:
    """The Cancel route's body (``POST /jobs/{id}/cancel``) in a session of its own."""
    with w.factory() as s:
        return cancel_job(workspace_id=w.ws, job_id=job_id, db=s, ctx=w.ctx).status


def _cancel_unchanged_accounting(w, job_id: str) -> str:
    """Cancel, asserting it writes no accounting row and resolves no admission (§1.4, §1.6)."""
    before = (_accounting(w), _admissions(w))
    status = _cancel(w, job_id)
    assert (_accounting(w), _admissions(w)) == before
    return status


def _run_now(w) -> str:
    with w.factory() as s:
        return run_scout_request_endpoint(
            workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx
        ).stats["job_id"]


def _execution(w) -> tuple:
    with w.factory() as s:
        out = get_scout_request(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx).execution
        return out.state.value, out.reason.value, out.next_action.value


def _scheduled_job(w, *, prior: str = "completed") -> str:
    """The seeded manual job retired, the request ``prior`` (an earlier run settled it) and a
    scheduled run enqueued the way the tick does (no ``prior_status``; status unchanged)."""
    with w.factory() as s:
        s.execute(update(Job).where(Job.id == w.job_id).values(status="succeeded"))
        req = s.get(ScoutRequest, w.rid)
        req.status = prior
        job = enqueue_scout_request(
            s,
            organization_id=w.org,
            workspace_id=w.ws,
            scout_request_id=w.rid,
            location_id=req.location_id,
            request_id=w.rid,
            idempotency_key=f"schedule:test:{w.rid}",
            trigger="scheduled",
            run_fence=req.run_fence,
        )
        s.commit()
        return job.id


def _manual_prior(w, prior: str = "completed") -> None:
    """Record on the seeded manual job the status Run now replaced (as the route does)."""
    with w.factory() as s:
        job = s.get(Job, w.job_id)
        job.payload = {**job.payload, "prior_status": prior}
        s.commit()


def _deadline_in(w, admission_id: str, seconds: float) -> None:
    """Move an admission's persisted deadline to ``seconds`` from the database clock, keeping
    ``admitted_at`` consistent with its basis (the passage of time, as an intervention)."""
    with w.observer.connect() as c:
        c.execute(
            text(
                "update scout_admissions a set deadline_at = t.ts + make_interval(secs => :s),"
                " admitted_at = t.ts + make_interval(secs => :s)"
                " - make_interval(secs => a.deadline_client_seconds + a.deadline_lease_seconds)"
                " from (select clock_timestamp() as ts) t where a.id = :i"
            ),
            {"s": seconds, "i": admission_id},
        )


def _attempt(
    w, *, attempt=1, token="tok-a", worker="w-a", live=True, status="running"
) -> RunAdmission:
    expires = datetime.now(UTC) + (timedelta(minutes=10) if live else -timedelta(minutes=10))
    with w.factory() as s:
        s.execute(
            update(Job)
            .where(Job.id == w.job_id)
            .values(
                status=status,
                worker_id=worker,
                attempt_count=attempt,
                lease_token=token,
                lease_expires_at=expires,
            )
        )
        s.execute(
            update(ScoutRequest)
            .where(ScoutRequest.id == w.rid)
            .values(
                status="running",
                owner_job_id=w.job_id,
                owner_attempt=attempt,
                owner_lease_token=token,
            )
        )
        s.commit()
    return RunAdmission(
        scout_request_id=w.rid,
        organization_id=w.org,
        workspace_id=w.ws,
        fence=0,
        session_factory=w.factory,
        job_id=w.job_id,
        worker_id=worker,
        attempt=attempt,
        lease_token=token,
        claimed=True,
        pre_claim_status="queued",
    )


def _set_job(w, **values) -> None:
    with w.factory() as s:
        s.execute(update(Job).where(Job.id == w.job_id).values(**values))
        s.commit()


# --------------------------------------------------------------------------- #
# §9.1: the fenced transaction locks the job row before the request (a defect fix)
# --------------------------------------------------------------------------- #
def _lock_waiter(w, exclude: int) -> bool:
    with w.observer.connect() as c:
        return bool(
            c.execute(
                text(
                    "select count(*) from pg_stat_activity where wait_event_type = 'Lock'"
                    " and pid <> :p and datname = current_database()"
                    " and query ilike '%for no key update%'"
                ),
                {"p": exclude},
            ).scalar_one()
        )


@pytest.mark.parametrize("path", ["cancel"])
def test_the_fenced_transaction_locks_the_job_row_before_the_request(world, monkeypatch, path):
    """Recovery contract §9.1 (PROPOSED) on the Cancel path, which R-C adds to the fenced
    transaction (the refusal-at-bound path is ``test_scout_fenced_lock_order_postgres``): a
    stale admission check waits for the fenced ``finish_cancel``; the Scout ends idle."""
    w = world
    sources = Sources(gate=True, count=5 if path == "refusal_at_bound" else None)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    _mock_llm(monkeypatch)
    if path == "refusal_at_bound":
        monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", 2)
    transition = "fail" if path == "refusal_at_bound" else "finish_cancel"
    at_transition, stale_waiting = threading.Event(), threading.Event()
    real = getattr(DurableJobStore, transition)

    def held(self, db, job, **kw):
        at_transition.set()
        stale_waiting.wait(10)  # the stale admission queues on the request first
        return real(self, db, job, **kw)

    monkeypatch.setattr(DurableJobStore, transition, held)
    worker = _start_worker(w)
    assert sources.started.wait(JOIN_SECONDS)  # claimed, running, parked in its fetch
    if path == "cancel":
        assert _cancel(w, w.job_id) == "cancel_requested"
    stale = w.engine.connect()
    stale.begin()
    stale_pid = stale.execute(text("select pg_backend_pid()")).scalar_one()
    stale.execute(text("select status from jobs where id = :j for share"), {"j": w.job_id}).all()
    sources.release.set()
    _await(lambda: at_transition.is_set() or _lock_waiter(w, stale_pid), "the fenced transaction")
    error: list[BaseException] = []

    def second_half():
        try:
            stale.execute(
                text("select status from scout_requests where id = :r for share"), {"r": w.rid}
            ).all()
            stale.commit()
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)
            stale.rollback()

    t = threading.Thread(target=second_half, daemon=True)
    t.start()
    time.sleep(0.3)
    stale_waiting.set()
    t.join(30)
    stale.close()
    assert worker.join_ok() in (True, False)
    assert not error, [type(getattr(e, "orig", e)).__name__ for e in error]
    end = _end_state(w)
    if path == "refusal_at_bound":
        assert (end.job_status, end.status) == ("failed", "failed")
        assert end.stats.get("refused")
    else:
        assert (end.job_status, end.status) == ("cancelled", "draft")


# --------------------------------------------------------------------------- #
# R-D: persisted deadlines (§3, §9.2)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("llm_timeout", "egress_total", "lease", "model_span", "fetch_span"),
    [(30, 20.0, 30.0, 120.0, 50.0), (7, 4.5, 11.0, 32.0, 15.5)],
)
def test_each_admission_persists_its_deadline_and_basis_from_one_clock_reading(
    world, monkeypatch, llm_timeout, egress_total, lease, model_span, fetch_span
):
    w = world
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_timeout_seconds", llm_timeout)
    monkeypatch.setattr(settings, "connector_egress_total_timeout_seconds", egress_total)
    monkeypatch.setattr(settings, "worker_lease_seconds", lease)
    _use(monkeypatch, Provider())
    assert _start_worker(w).join_ok() is True
    with w.observer.connect() as c:
        rows = c.execute(
            text(
                "select operation, deadline_rule, deadline_client_seconds, deadline_lease_seconds,"
                " extract(epoch from deadline_at - admitted_at) from scout_admissions"
                " where scout_request_id = :r"
            ),
            {"r": w.rid},
        ).all()
    assert rows and {r[1] for r in rows} == {"client_plus_lease"}
    for operation, _rule, client, lease_s, span in rows:
        assert lease_s == lease
        assert float(span) == (model_span if operation == "model" else fetch_span)
        assert client + lease_s == float(span)


def test_the_admission_insert_takes_one_database_clock_reading(world, monkeypatch):
    """§9.2.2 (mutant D9's killer): ``admitted_at`` and ``deadline_at`` come from one reading."""
    w = world
    inserts: list[str] = []

    def capture(_conn, _cursor, statement, *_a):
        if "INSERT INTO scout_admissions" in statement:
            inserts.append(statement)

    event.listen(w.engine, "before_cursor_execute", capture)
    try:
        adm = _attempt(w)
        adm.admit_external("model")
    finally:
        event.remove(w.engine, "before_cursor_execute", capture)
    assert len(inserts) == 1
    assert inserts[0].lower().count("clock_timestamp()") == 1


def test_reader_settings_never_move_a_persisted_deadline(world, monkeypatch):
    """§3.2 (mutants D1-D4): a later settings change can neither shorten nor lengthen an
    existing admission's gate, for the reconciler, the listing and the derived state."""
    w = world
    adm = _attempt(w)
    admission_id, _ = adm.admit_external("model")  # persisted with the defaults (120 s)
    _set_job(w, status="cancel_requested")  # the attempt no longer holds authority
    _deadline_in(w, admission_id, 30)  # admitted 90 s ago: a 3.5 s reader deadline has passed
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_timeout_seconds", 1)
    monkeypatch.setattr(settings, "worker_lease_seconds", 0.5)
    with w.factory() as s:
        with pytest.raises(ReconciliationRefused, match="has not passed"):
            resolve(s, admission_id=admission_id, operator="ops", reason="operator_decision")
        [item] = list_unresolved(s, w.rid)["current_owner"]
        assert item["deadline_passed"] is False
        assert item["deadline_basis"] == {
            "rule": "client_plus_lease",
            "client_seconds": 90.0,
            "lease_seconds": 30.0,
        }
    assert _execution(w)[0] == "in_progress"
    # ...and larger settings do not lengthen a deadline that has passed.
    monkeypatch.setattr(settings, "llm_timeout_seconds", 120)
    monkeypatch.setattr(settings, "worker_lease_seconds", 3600)
    _deadline_in(w, admission_id, -2)
    assert _execution(w)[:2] == ("needs_attention", "operation_overdue")
    with w.factory() as s:
        assert resolve(s, admission_id=admission_id, operator="ops", reason="operator_decision")
        s.commit()


def test_the_reconcile_command_with_other_settings_keeps_the_persisted_gate(world):
    w = world
    adm = _attempt(w)
    admission_id, _ = adm.admit_external("fetch")  # 20 + 30 s persisted
    _set_job(w, status="cancel_requested")
    _deadline_in(w, admission_id, 30)  # admitted 20 s ago: the CLI's own 11.5 s has passed
    env = dict(os.environ)
    env.update(
        DATABASE_URL=w.engine.url.render_as_string(hide_password=False),
        WORKER_HEARTBEAT_SECONDS="1",
        WORKER_LEASE_SECONDS="11",
        CONNECTOR_EGRESS_TOTAL_TIMEOUT_SECONDS="0.5",
    )
    env.pop("SN_MIGRATION_MODE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.jobs.reconcile",
            "resolve",
            "--admission",
            admission_id,
            "--operator",
            "ops",
            "--reason",
            "operator_decision",
        ],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "has not passed" in result.stderr
    assert _unresolved(w) == 1


def test_the_gate_and_the_state_read_the_database_clock(world, monkeypatch):
    from datetime import datetime as real_datetime

    from app.jobs import reconcile as rec
    from app.scouting_requests import execution as exe

    class Future(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(days=1)

    w = world
    adm = _attempt(w)
    admission_id, _ = adm.admit_external("model")
    _set_job(w, status="cancel_requested")
    monkeypatch.setattr(rec, "datetime", Future)
    monkeypatch.setattr(exe, "datetime", Future)
    with w.factory() as s:
        with pytest.raises(ReconciliationRefused):
            resolve(s, admission_id=admission_id, operator="ops", reason="operator_decision")
    assert _execution(w)[0] == "in_progress"


def _alembic(w, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = w.engine.url.render_as_string(hide_password=False)
    env.pop("SN_MIGRATION_MODE", None)
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _step_down(w) -> None:
    """Down to c6f1d8e2a4b7 (the ledger without deadlines). W16 (PROPOSED) adds one revision
    on top (e7b3c5a9d1f4) and Project Phase 5 CF-1 another (f3a9c1e7b2d5), so that is three
    guarded steps down from the code head."""
    for _ in range(3):
        token = _alembic(w, "app.db.migrate", "downgrade-confirmation", "-1")
        assert token.returncode == 0, token.stderr
        down = _alembic(w, "alembic", "-x", f"confirm={token.stdout.strip()}", "downgrade", "-1")
        assert down.returncode == 0, down.stdout + down.stderr


def _scouts(w, n: int) -> list[str]:
    import uuid

    ids = []
    with w.observer.connect() as c:
        for _ in range(n):
            rid = uuid.uuid4().hex
            c.execute(
                text(
                    "insert into scout_requests (id, organization_id, workspace_id, brand_id,"
                    " location_id, campaign_id, name, status, source_types, keywords,"
                    " product_profile_id, resolved_market, notes, last_run_at, stats, run_fence,"
                    " created_at, updated_at) select :n, organization_id, workspace_id, brand_id,"
                    " location_id, campaign_id, name, 'failed', source_types, keywords,"
                    " product_profile_id, resolved_market, notes, last_run_at, stats, 0, now(),"
                    " now() from scout_requests where id=:r"
                ),
                {"n": rid, "r": w.rid},
            )
            ids.append(rid)
    return ids


def test_the_migration_marks_existing_rows_unrecorded_and_never_backfills(world):
    """§3.4, §9.2.3 (mutants D5/D6): rows created before the revision get no deadline; an
    unknown end stays eligible; a no-end row needs the acknowledgement and is still refused
    while its attempt holds authority; downgrade -> upgrade keeps them unrecorded."""
    import uuid

    w = world
    _step_down(w)  # to c6f1d8e2a4b7: the ledger without deadlines
    with w.observer.connect() as c:
        assert c.execute(text("select version_num from alembic_version")).scalar_one() == (
            "c6f1d8e2a4b7"
        )
    s_unknown, s_no_end, s_resolved, s_live = _scouts(w, 4)
    rows = {}
    with w.observer.connect() as c:
        for label, rid, ended, kind, resolved, job in (
            ("unknown", s_unknown, True, "unknown", False, None),
            ("no_end", s_no_end, False, None, False, None),
            ("resolved", s_resolved, True, "known", True, None),
            ("live", s_live, False, None, False, w.job_id),
        ):
            rows[label] = uuid.uuid4().hex
            c.execute(
                text(
                    "insert into scout_admissions (id, scout_request_id, organization_id,"
                    " workspace_id, job_id, attempt, worker_id, operation, admitted_at, ended_at,"
                    " end_kind, resolved_at, created_at, updated_at) values (:id, :r, :o, :ws, :j,"
                    " 1, 'w-live', 'model', clock_timestamp() - interval '2 days',"
                    " case when :e then clock_timestamp() end, :k,"
                    " case when :res then clock_timestamp() end, now(), now())"
                ),
                {
                    "id": rows[label],
                    "r": rid,
                    "o": w.org,
                    "ws": w.ws,
                    "j": job,
                    "e": ended,
                    "k": kind,
                    "res": resolved,
                },
            )
        c.execute(
            text(
                "update jobs set status='running', worker_id='w-live', attempt_count=1,"
                " lease_token='tok', lease_expires_at=clock_timestamp() + interval '1 hour'"
                " where id=:j"
            ),
            {"j": w.job_id},
        )
    up = _alembic(w, "alembic", "upgrade", "head")
    assert up.returncode == 0, up.stderr
    with w.observer.connect() as c:
        marked = c.execute(
            text(
                "select id, deadline_rule, deadline_at, deadline_client_seconds,"
                " deadline_lease_seconds from scout_admissions"
            )
        ).all()
    assert {r[0]: r[1:] for r in marked} == {
        v: ("unrecorded", None, None, None) for v in rows.values()
    }
    with w.factory() as s:
        assert resolve(s, admission_id=rows["unknown"], operator="ops", reason="provider_confirmed")
        with pytest.raises(ReconciliationRefused, match="acknowledge-unrecorded-deadline"):
            resolve(s, admission_id=rows["no_end"], operator="ops", reason="worker_crashed")
        with pytest.raises(ReconciliationRefused, match="holds valid authority"):
            resolve(
                s,
                admission_id=rows["live"],
                operator="ops",
                reason="worker_crashed",
                acknowledge_unrecorded_deadline=True,
            )
        assert resolve(
            s,
            admission_id=rows["no_end"],
            operator="ops",
            reason="worker_crashed",
            acknowledge_unrecorded_deadline=True,
        )
        s.commit()
        audit = s.scalars(select(AuditLog).where(AuditLog.entity_id == rows["no_end"])).one()
        assert audit.context["acknowledged_unrecorded_deadline"] is True
    # Downgrade (the guarded -1) and upgrade again: the rows survive, still unrecorded.
    _step_down(w)
    with w.observer.connect() as c:
        assert c.execute(text("select count(*) from scout_admissions")).scalar_one() == 4
    assert _alembic(w, "alembic", "upgrade", "head").returncode == 0
    with w.observer.connect() as c:
        assert set(c.execute(text("select deadline_rule from scout_admissions")).scalars()) == {
            "unrecorded"
        }


def test_a_waiter_behind_a_legacy_row_gives_up_at_its_own_bound(world, monkeypatch):
    """§3.4: a legacy no-end row has no deadline; a waiter behind it uses its own bound (the
    end of a wait resolves nothing): an admission two hours old is past it at once."""
    import uuid

    w = world
    with w.observer.connect() as c:
        legacy = uuid.uuid4().hex
        c.execute(
            text(
                "insert into scout_admissions (id, scout_request_id, organization_id,"
                " workspace_id, attempt, worker_id, operation, admitted_at) values (:id, :r, :o,"
                " :ws, 1, 'w-gone', 'model', clock_timestamp() - interval '2 hours')"
            ),
            {"id": legacy, "r": w.rid, "o": w.org, "ws": w.ws},
        )
    _use(monkeypatch, Provider())
    assert _start_worker(w).join_ok() is True
    end = _end_state(w)
    assert end.job_error == ("non_retryable", "admission_unresolved")
    assert _admissions(w)[0][3] is False  # still unresolved


# --------------------------------------------------------------------------- #
# §9.3: A2 and the race with a late known end (A11)
# --------------------------------------------------------------------------- #
def test_resolve_refuses_an_unknown_end_while_its_attempt_holds_authority(world):
    w = world
    adm = _attempt(w)
    admission_id, _ = adm.admit_external("model")
    adm.record_end(admission_id, known=False)
    with w.factory() as s:
        with pytest.raises(ReconciliationRefused, match="holds valid authority"):
            resolve(s, admission_id=admission_id, operator="ops", reason="provider_confirmed")
    assert _execution(w)[0] == "in_progress"
    _expire_lease(w, w.job_id)
    assert _execution(w)[:2] == ("needs_attention", "operation_outcome_unknown")
    with w.factory() as s:
        assert resolve(s, admission_id=admission_id, operator="ops", reason="provider_confirmed")
        s.commit()


@pytest.mark.parametrize("first", ["resolve", "late_end"])
def test_reconciliation_racing_a_late_known_end_has_one_outcome(world, first):
    w = world
    adm = _attempt(w)
    admission_id, _ = adm.admit_external("model")
    _set_job(w, status="cancel_requested")
    _deadline_in(w, admission_id, -1)
    before = _accounting(w)
    if first == "resolve":
        holder = w.factory()
        assert resolve(
            holder, admission_id=admission_id, operator="ops", reason="operator_decision"
        )
        late = threading.Thread(
            target=lambda: adm.record_end(admission_id, known=True), daemon=True
        )
        late.start()
        time.sleep(0.3)
        assert late.is_alive()  # the late end waits for the reconciliation's row lock
        holder.commit()
        holder.close()
        late.join(30)
    else:
        adm.record_end(admission_id, known=True)
        with w.factory() as s:
            assert (
                resolve(s, admission_id=admission_id, operator="ops", reason="operator_decision")
                is False
            )
            s.commit()
    with w.observer.connect() as c:
        row = c.execute(
            text(
                "select end_kind, resolved_at is not null, resolved_by from scout_admissions"
                " where id=:i"
            ),
            {"i": admission_id},
        ).one()
        audits = c.execute(
            text("select count(*) from audit_logs where entity_id=:i"), {"i": admission_id}
        ).scalar_one()
    assert row[0] == "known" and row[1] is True
    assert (row[2], audits) == (("ops", 1) if first == "resolve" else (None, 0))
    assert _accounting(w) == before


# --------------------------------------------------------------------------- #
# R-V on PostgreSQL: the refusal for a really blocked Scout
# --------------------------------------------------------------------------- #
def test_run_now_is_refused_for_a_scout_blocked_by_a_model_timeout(world, monkeypatch):
    w = world
    provider = Provider(timeout={1})
    _use(monkeypatch, provider)
    assert _start_worker(w).join_ok() is True
    assert _end_state(w).job_error == ("non_retryable", "admission_unresolved")
    assert _execution(w) == ("needs_attention", "operation_outcome_unknown", "contact_support")
    with w.factory() as s:
        jobs_before = len(s.scalars(select(Job.id)).all())
    with pytest.raises(ValidationDomainError) as refused:
        _run_now(w)
    assert refused.value.code == "scout_needs_attention"
    with w.factory() as s:
        assert len(s.scalars(select(Job.id)).all()) == jobs_before


# --------------------------------------------------------------------------- #
# R-C: every Cancel shape (§5.2, §9.5.1)
# --------------------------------------------------------------------------- #
def _usable(w) -> None:
    """Run now is accepted from the end state (the request is not stranded)."""
    assert _run_now(w)


def test_1a_a_manual_run_cancelled_before_its_claim_returns_to_its_prior_status(world):
    w = world
    _manual_prior(w, "completed")
    assert _cancel_unchanged_accounting(w, w.job_id) == "cancelled"
    assert _request(w) == ("completed", 0, None)
    assert _execution(w) == ("stopped", "run_cancelled", "run_now")
    _usable(w)


def test_1a_a_legacy_job_without_a_prior_status_returns_to_draft(world):
    w = world
    assert _cancel(w, w.job_id) == "cancelled"
    assert _request(w)[0] == "draft"


def test_1b_a_scheduled_run_cancelled_before_its_claim_stays_usable(world):
    w = world
    sid = _scheduled_job(w)
    assert _cancel_unchanged_accounting(w, sid) == "cancelled"
    assert _request(w)[0] == "completed"
    _usable(w)


def test_1c_a_retry_wait_after_a_lapsed_lease_cancelled(world, monkeypatch):
    w = world
    _manual_prior(w, "failed")
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    _mock_llm(monkeypatch)
    worker = _start_worker(w)
    assert sources.started.wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    sources.release.set()
    assert worker.join_ok() is True
    assert (_job(w, w.job_id).status, _request(w)[0]) == ("retry_wait", "running")
    assert _cancel_unchanged_accounting(w, w.job_id) == "cancelled"
    assert _request(w) == ("failed", 0, None)
    _usable(w)


def test_1d_pending_after_recovery_with_a_zombie_in_flight(world, monkeypatch):
    w = world
    _manual_prior(w, "completed")
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    zombie = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    with w.factory() as s:
        assert DurableJobStore().recover_expired_leases(s, now=datetime.now(UTC)) == 1
        s.commit()
    assert _request(w)[2] == w.job_id  # owner: the dead attempt 1
    assert _cancel_unchanged_accounting(w, w.job_id) == "cancelled"
    assert _request(w) == ("completed", 0, None)  # the stale owner of THIS job is cleared
    assert _unresolved(w) == 1 and _execution(w)[0] == "in_progress"
    provider.release.set()
    zombie.join_ok()
    assert len(provider.calls) == 1  # the zombie admitted nothing further
    assert _unresolved(w) == 0 and _request(w)[0] == "completed"
    _usable(w)


def test_1e_cancel_after_the_job_claim_before_the_scout_claim(world, monkeypatch):
    from app.jobs import worker as worker_mod

    w = world
    _manual_prior(w, "failed")
    monkeypatch.setattr(pipeline, "fetch_signals", Sources())
    _mock_llm(monkeypatch)
    real = worker_mod.JobRunner._cancel_checker

    def checker(self, job_id):
        inner, fired = real(self, job_id), []

        def probe():
            if not fired:
                fired.append(_cancel(w, job_id))
            return inner()

        return probe

    monkeypatch.setattr(worker_mod.JobRunner, "_cancel_checker", checker)
    assert _start_worker(w).join_ok() is True
    assert _job(w, w.job_id).status == "cancelled"
    assert _request(w) == ("failed", 0, None)
    _usable(w)


def test_2a_a_manual_run_cancelled_during_an_admitted_call(world, monkeypatch):
    w = world
    _manual_prior(w, "completed")
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    worker = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    assert _cancel_unchanged_accounting(w, w.job_id) == "cancel_requested"
    assert _unresolved(w) == 1 and _execution(w)[0] == "in_progress"
    provider.release.set()
    assert worker.join_ok() is True
    assert _job(w, w.job_id).status == "cancelled"
    assert _request(w) == ("completed", 0, None)
    assert len(provider.calls) == 1 and _unresolved(w) == 0  # resolved by its own known end
    assert _execution(w) == ("stopped", "run_cancelled", "run_now")
    _usable(w)


def test_2b_a_scheduled_run_cancelled_during_an_admitted_call_stays_usable(world, monkeypatch):
    w = world
    sid = _scheduled_job(w, prior="completed")
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    worker = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    # §9.5.5: the first claim recorded the status it replaced on the job.
    assert _job(w, sid).payload["prior_status"] == "completed"
    assert _cancel(w, sid) == "cancel_requested"
    provider.release.set()
    assert worker.join_ok() is True
    assert (_job(w, sid).status, _request(w)[0]) == ("cancelled", "completed")
    _usable(w)


def test_3a_a_recovered_attempt_waiting_behind_the_zombies_admission(world, monkeypatch):
    w = world
    _manual_prior(w, "completed")
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    zombie = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    waiter = _start_worker(w, _runner(w))
    _await(lambda: _job(w, w.job_id).attempt_count == 2, "attempt 2 claimed")
    time.sleep(0.6)
    assert waiter.is_alive() and len(provider.calls) == 1
    assert _cancel_unchanged_accounting(w, w.job_id) == "cancel_requested"
    assert waiter.join_ok() is True
    assert _job(w, w.job_id).status == "cancelled"
    assert _request(w) == ("completed", 0, None)
    assert _unresolved(w) == 1  # the blocking admission stays unresolved
    assert _execution(w)[0] == "in_progress"  # not yet eligible (its deadline is 120 s away)
    # Becoming idle does not bypass K = 1: the next run waits behind the zombie's admission.
    new_job = _run_now(w)
    nxt = _start_worker(w, _runner(w))
    _await(lambda: _job(w, new_job).status == "running", "the next run claimed")
    time.sleep(0.6)
    assert nxt.is_alive() and len(provider.calls) == 1
    provider.release.set()
    zombie.join_ok()
    assert nxt.join_ok() is True
    assert _job(w, new_job).result_summary["outcome"] == "completed"


def test_3b_a_new_run_waiting_behind_a_paused_runs_admission(world, monkeypatch):
    w = world
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    old = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    harness._pause(w)
    with w.factory() as s:
        resume_scout_request(workspace_id=w.ws, request_id=w.rid, db=s, ctx=w.ctx)
        s.commit()
    new_id = _run_now(w)  # records prior_status = draft
    new = _start_worker(w, _runner(w))
    _await(lambda: _job(w, new_id).status == "running", "the new run claimed")
    time.sleep(0.6)
    assert new.is_alive() and len(provider.calls) == 1
    assert _cancel_unchanged_accounting(w, new_id) == "cancel_requested"
    assert new.join_ok() is True
    # The old run (fence 0) is fenced out by the pause: not a rival (§9.5.3).
    assert (_job(w, new_id).status, _request(w)[0]) == ("cancelled", "draft")
    provider.release.set()
    old.join_ok()
    assert _job(w, w.job_id).result_summary["outcome"] == "stopped_request_paused"
    assert _request(w)[0] == "draft" and _unresolved(w) == 0
    _usable(w)


def test_4_a_recovered_attempt_cancelled_while_the_original_is_alive(world, monkeypatch):
    w = world
    _manual_prior(w, "completed")
    sources = GateSources(2)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    _mock_llm(monkeypatch)
    first = _start_worker(w)
    assert sources.started_i[0].wait(JOIN_SECONDS)
    _expire_lease(w, w.job_id)
    second = _start_worker(w, _runner(w))
    assert sources.started_i[1].wait(JOIN_SECONDS)
    assert _cancel_unchanged_accounting(w, w.job_id) == "cancel_requested"
    sources.release_i[1].set()
    assert second.join_ok() is True
    assert (_job(w, w.job_id).status, _request(w)) == ("cancelled", ("completed", 0, None))
    sources.release_i[0].set()
    first.join_ok()
    assert _request(w) == ("completed", 0, None)  # attempt 1 wrote nothing (O1)
    _usable(w)


def test_a_recovered_scheduled_run_reads_the_prior_status_its_first_claim_recorded(
    world, monkeypatch
):
    """§9.5.5: attempt 2's pre-claim status is ``running``; it reads ``completed`` from the job,
    so it does not fall back to ``draft`` (mutant C10's killer)."""
    w = world
    sid = _scheduled_job(w, prior="completed")
    sources = GateSources(2)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    _mock_llm(monkeypatch)
    first = _start_worker(w)
    assert sources.started_i[0].wait(JOIN_SECONDS)
    _expire_lease(w, sid)
    second = _start_worker(w, _runner(w))
    assert sources.started_i[1].wait(JOIN_SECONDS)
    assert _cancel(w, sid) == "cancel_requested"
    sources.release_i[1].set()
    assert second.join_ok() is True
    assert _request(w)[0] == "completed"
    sources.release_i[0].set()
    first.join_ok()


def test_two_concurrent_cancels_of_two_pending_jobs_end_idle(world):
    """§9.5.3 (lane W15 probe L4): each Cancel locks the request before reading rivals."""
    w = world
    _manual_prior(w, "completed")
    second = _scheduled_job_beside(w)
    barrier = threading.Barrier(2)
    results: list = []

    def cancel(job_id):
        with w.factory() as s:
            from app.jobs.registry import resolve_cancel_hook

            job = s.get(Job, job_id)
            barrier.wait(5)
            hook = resolve_cancel_hook(job.job_type)
            DurableJobStore().request_cancel(
                s, job, on_cancelled=lambda j: hook(s, j, j.cancelled_at)
            )
            time.sleep(0.2)
            s.commit()
            results.append(job.status)

    threads = [threading.Thread(target=cancel, args=(j,)) for j in (w.job_id, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results) == ["cancelled", "cancelled"]
    # Idle, not stranded ``queued``. The value is the prior status of whichever Cancel wrote
    # last: ``completed`` from the manual job, or ``draft`` from the never-claimed scheduled job,
    # which has none recorded (§9.5.5; a stated limitation).
    assert _request(w)[0] in ("completed", "draft")


def _scheduled_job_beside(w) -> str:
    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        job = enqueue_scout_request(
            s,
            organization_id=w.org,
            workspace_id=w.ws,
            scout_request_id=w.rid,
            location_id=req.location_id,
            request_id=w.rid,
            idempotency_key="beside",
            trigger="scheduled",
            run_fence=req.run_fence,
        )
        s.commit()
        return job.id


def test_cancel_racing_a_concurrent_completion_keeps_the_completed_result(world):
    """§9.5.4: the Cancel route's job UPDATE is a compare-and-set on the status it read. The
    completion commits while the Cancel's UPDATE waits on the row; the Cancel then matches no
    row, re-reads ``succeeded`` and writes nothing (mutant C12's killer)."""
    w = world
    _attempt(w)
    with w.factory() as s:
        s.execute(update(ScoutRequest).where(ScoutRequest.id == w.rid).values(status="running"))
        s.commit()
    route, completer = w.factory(), w.engine.connect()
    try:
        read = route.get(Job, w.job_id)  # the route read it running
        tx = completer.begin()
        completer.execute(
            text(
                "update jobs set status='succeeded', worker_id=null, lease_token=null where id=:j"
            ),
            {"j": w.job_id},
        )
        completer.execute(
            text(
                "update scout_requests set status='completed', owner_job_id=null,"
                " owner_attempt=null, owner_lease_token=null where id=:r"
            ),
            {"r": w.rid},
        )
        done: list = []
        cancel = threading.Thread(
            target=lambda: done.append(DurableJobStore().request_cancel(route, read).status),
            daemon=True,
        )
        cancel.start()
        time.sleep(0.3)
        assert cancel.is_alive()  # the Cancel's UPDATE waits for the completion's row lock
        tx.commit()
        cancel.join(30)
        route.commit()
        assert done == ["succeeded"]
    finally:
        route.close()
        completer.close()
    job = _job(w, w.job_id)
    assert (job.status, job.cancel_requested_at) == ("succeeded", None)
    assert _request(w)[0] == "completed"


def test_the_cancel_idle_write_commits_only_with_the_fenced_cancel(world, monkeypatch):
    """§5.1/§9.5: the idle write runs inside the attempt's fenced ``finish_cancel`` transaction.
    If the attempt loses its job just before that transaction (a re-claim commits), nothing is
    written over the replacement: it keeps ``running`` and its ownership (mutant C5's killer)."""
    from app.jobs.worker import JobRunner

    w = world
    _manual_prior(w, "completed")
    provider = Provider(park={1})
    _use(monkeypatch, provider)
    real_write_fenced = JobRunner._write_fenced

    def reclaimed_then_fenced(self, db, writers, **kwargs):
        if writers:
            with w.factory() as other:
                other.execute(
                    update(Job)
                    .where(Job.id == w.job_id)
                    .values(
                        status="running",
                        attempt_count=Job.attempt_count + 1,
                        worker_id="w-replacement",
                        lease_token="tok-replacement",
                        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
                    )
                )
                other.execute(
                    update(ScoutRequest)
                    .where(ScoutRequest.id == w.rid)
                    .values(
                        owner_job_id=w.job_id, owner_attempt=2, owner_lease_token="tok-replacement"
                    )
                )
                other.commit()
        return real_write_fenced(self, db, writers, **kwargs)

    monkeypatch.setattr(JobRunner, "_write_fenced", reclaimed_then_fenced)
    worker = _start_worker(w)
    assert provider.parked.wait(JOIN_SECONDS)
    assert _cancel(w, w.job_id) == "cancel_requested"
    provider.release.set()
    worker.join_ok()
    assert _request(w) == ("running", 0, w.job_id)
    job = _job(w, w.job_id)
    assert (job.status, job.lease_token) == ("running", "tok-replacement")


class _Waited(Exception):
    """Raised by the fake sleep: the admission waited instead of ending."""


@pytest.mark.parametrize("persisted", ["passed", "ahead"])
def test_the_waiting_rule_reads_the_persisted_deadline(world, monkeypatch, persisted):
    """§3.2 for the waiting rule (mutant D1's killer): a waiter's own settings decide neither
    way. ``passed``: the blocking row's stored deadline has passed while the waiter's settings
    would allow an hour: the admission ends at once (``AdmissionUnresolved``). ``ahead``: the
    stored deadline is a minute away while the waiter's settings allow 3.5 s: it waits."""
    from app.jobs.admission import AdmissionUnresolved

    w = world
    first = _attempt(w, attempt=1, token="tok-a", worker="w-a")
    blocking, _ = first.admit_external("model")  # outstanding, no end
    _deadline_in(w, blocking, -1 if persisted == "passed" else 60)
    waiter = _attempt(w, attempt=2, token="tok-b", worker="w-b")
    settings = get_settings()
    if persisted == "passed":
        monkeypatch.setattr(settings, "llm_timeout_seconds", 120)
        monkeypatch.setattr(settings, "worker_lease_seconds", 3600)
    else:
        monkeypatch.setattr(settings, "llm_timeout_seconds", 1)
        monkeypatch.setattr(settings, "worker_lease_seconds", 0.5)

    def no_sleep(_seconds):
        raise _Waited()

    if persisted == "passed":
        with pytest.raises(AdmissionUnresolved):
            waiter.admit_external("model", sleep=no_sleep)
    else:
        with pytest.raises(_Waited):
            waiter.admit_external("model", sleep=no_sleep)
    assert _unresolved(w) == 1
