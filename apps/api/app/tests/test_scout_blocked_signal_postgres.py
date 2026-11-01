"""FU-267-A1 (PROPOSED): the operator signal for blocked Scouts, on PostgreSQL.

A Scout is blocked when it is in ``needs_attention`` (an admitted external operation with no
known end, eligible for reconciliation). The signal is two gauges set by the worker's registry
heartbeat (``app.jobs.blocked_signal``) and the ``scouts`` block of the operator overview,
both from ``app.scouting_requests.execution.blocked_scouts`` - the aggregate of the rows the
``reconcile blocked`` view lists. This module proves, on a schema the real Alembic chain
produced, with ledger rows written by the real admission path (``RunAdmission``):

* the value for 0, 1 and several blocked Scouts across two organizations, equal to the
  ``reconcile blocked`` listing and to the Scouts whose own derived state is
  ``needs_attention`` (one predicate, never a second);
* a reconciled Scout leaves the count;
* healthy in-flight operations are never counted (every non-eligible shape);
* the gauges carry no label and pass ``validate_metric``;
* the worker's heartbeat sets them on its own schedule, with no request at all;
* one sweep is two ``SET LOCAL`` bounds, one clock reading and ONE listing statement, whatever
  the number of rows, and that statement reads the partial index
  ``uq_scout_admission_unresolved``;
* a lock on the ledger (a migration's ``ACCESS EXCLUSIVE``) costs the heartbeat at most the
  read's bound, never the lock's duration, and sets no gauge from the timed-out read (review r1
  MINOR-2).

The SQLite behaviour (the signal is absent, never zero) is in ``test_scout_blocked_signal.py``.

Gate: ``require_postgres`` -- with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI
(``CI=true``) and skip only on a developer machine.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text

from app.core.config import get_settings
from app.core.enums import ScoutRequestStatus
from app.core.metrics import (
    ALLOWED_LABELS,
    METRIC_NAMES,
    SCOUTS_BLOCKED,
    SCOUTS_BLOCKED_OLDEST_AGE_SECONDS,
    InMemoryMetrics,
    MetricError,
    configure_metrics,
    validate_metric,
)
from app.jobs import blocked_signal
from app.jobs.blocked_signal import read_timeouts_ms, record_blocked_scouts
from app.jobs.reconcile import blocked, resolve
from app.jobs.service import enqueue_scout_request
from app.jobs.worker import Worker
from app.scouting_requests.execution import blocked_scouts, derive_executions
from app.scouting_requests.models import ScoutRequest
from app.system.internal_observability_routes import internal_overview
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests.test_scout_execution_controls_postgres import _expire_lease
from app.tests.test_scout_recovery_postgres import _attempt, _deadline_in

#: The harness's fixtures (a migrated template database per module, a clone per test).
template_url = harness.template_url
world = harness.world

NEEDS_ATTENTION = "needs_attention"
#: Bound on every wait for the heartbeat thread (only a defect makes it long).
WAIT_SECONDS = 20.0


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def metrics() -> Iterator[InMemoryMetrics]:
    backend = InMemoryMetrics()
    configure_metrics(backend)
    try:
        yield backend
    finally:
        configure_metrics(None)


# --------------------------------------------------------------------------- #
# Scouts and admissions in each shape
# --------------------------------------------------------------------------- #
def _scout(w, tenant=None) -> SimpleNamespace:
    """A further Scout with a queued execute job, in ``tenant``'s tenancy (default: the
    world's), shaped like the world for the recovery helpers."""
    src = tenant or w
    with w.factory() as s:
        first = s.get(ScoutRequest, src.rid)
        req = ScoutRequest(
            organization_id=first.organization_id,
            workspace_id=first.workspace_id,
            brand_id=first.brand_id,
            location_id=first.location_id,
            name=f"Scout {uuid.uuid4().hex[:6]}",
            status=ScoutRequestStatus.QUEUED.value,
            source_types=[],
            keywords=["coffee"],
            resolved_market=harness.MARKET,
        )
        s.add(req)
        s.flush()
        job = enqueue_scout_request(
            s,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
            location_id=req.location_id,
            request_id=req.id,
        )
        s.commit()
        return SimpleNamespace(
            factory=w.factory,
            observer=w.observer,
            org=req.organization_id,
            ws=req.workspace_id,
            rid=req.id,
            job_id=job.id,
        )


def _other_organization(w) -> SimpleNamespace:
    """A second organization with its own workspace, Scout and queued job."""
    other = harness.World(factory=w.factory, observer=w.observer)
    harness._seed(other)
    return other


def _unknown_end(ns) -> str:
    """A model call whose end is unknown, its attempt gone (lease lapsed): eligible."""
    adm = _attempt(ns)
    admission_id, _ = adm.admit_external("model")
    adm.record_end(admission_id, known=False)
    _expire_lease(ns, ns.job_id)
    return admission_id


def _overdue(ns, seconds_past: float = 60.0) -> str:
    """A fetch with no recorded end, its attempt gone and its stored deadline passed."""
    adm = _attempt(ns)
    admission_id, _ = adm.admit_external("fetch")
    _expire_lease(ns, ns.job_id)
    _deadline_in(ns, admission_id, -seconds_past)
    return admission_id


def _legacy(ns, hours_ago: int = 2) -> str:
    """A row created before deadlines were stored: no end, no deadline, no job (eligible)."""
    admission_id = uuid.uuid4().hex
    with ns.observer.connect() as c:
        c.execute(
            text(
                "insert into scout_admissions (id, scout_request_id, organization_id,"
                " workspace_id, attempt, worker_id, operation, admitted_at) values (:id, :r, :o,"
                " :ws, 1, 'w-gone', 'model', clock_timestamp() - make_interval(hours => :h))"
            ),
            {"id": admission_id, "r": ns.rid, "o": ns.org, "ws": ns.ws, "h": hours_ago},
        )
    return admission_id


def _in_flight(ns) -> str:
    """Healthy: the attempt holds authority and its deadline is ahead."""
    adm = _attempt(ns)
    admission_id, _ = adm.admit_external("model")
    return admission_id


def _in_flight_past_its_deadline(ns) -> str:
    """The attempt still holds authority although the stored deadline has passed."""
    admission_id = _in_flight(ns)
    _deadline_in(ns, admission_id, -60)
    return admission_id


def _unknown_end_still_held(ns) -> str:
    """An unknown end whose attempt still holds authority (it may retry: not reconcilable)."""
    adm = _attempt(ns)
    admission_id, _ = adm.admit_external("model")
    adm.record_end(admission_id, known=False)
    return admission_id


def _finishing_after_a_crash(ns) -> str:
    """The attempt is gone but the stored deadline is ahead: the client may still be in it."""
    admission_id = _in_flight(ns)
    _expire_lease(ns, ns.job_id)
    return admission_id


def _known_end(ns) -> str:
    """A completed call: resolved by its known end."""
    adm = _attempt(ns)
    admission_id, _ = adm.admit_external("model")
    adm.record_end(admission_id, known=True)
    return admission_id


NOT_ELIGIBLE = {
    "in_flight": _in_flight,
    "in_flight_past_its_deadline": _in_flight_past_its_deadline,
    "unknown_end_still_held": _unknown_end_still_held,
    "finishing_after_a_crash": _finishing_after_a_crash,
    "known_end": _known_end,
}


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _summary(w):
    with w.factory() as s:
        return blocked_scouts(s)


def _sweep(w, metrics: InMemoryMetrics) -> tuple[float | None, float | None]:
    """One sweep exactly as the heartbeat makes it; the two gauge values it left."""
    record_blocked_scouts(w.factory)
    return (
        metrics.gauge_value(SCOUTS_BLOCKED),
        metrics.gauge_value(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS),
    )


def _overview_scouts(w) -> dict:
    with w.factory() as s:
        return internal_overview(db=s, _operator=None).scouts.model_dump()


def _listed(w, **scope) -> set[str]:
    """The Scouts the operator's ``reconcile blocked`` view lists."""
    with w.factory() as s:
        return {item["scout_request_id"] for item in blocked(s, **scope)["blocked"]}


def _needing_attention(w) -> set[str]:
    """The Scouts whose own derived execution state is ``needs_attention``."""
    with w.factory() as s:
        requests = list(s.scalars(select(ScoutRequest)))
        return {
            rid
            for rid, out in derive_executions(s, requests).items()
            if out.state.value == NEEDS_ATTENTION
        }


def _assert_one_predicate(w, expected: set[str]) -> None:
    """The count equals the CLI's listing and the derived state, Scout for Scout."""
    assert _listed(w) == expected
    assert _needing_attention(w) == expected
    assert _summary(w).count == len(expected)


# --------------------------------------------------------------------------- #
# 0, 1 and several blocked Scouts across two organizations
# --------------------------------------------------------------------------- #
def test_no_blocked_scout_reads_zero_not_absent(world, metrics):
    w = world
    summary = _summary(w)
    assert (summary.count, summary.oldest_since, summary.oldest_age_seconds) == (0, None, None)
    assert _sweep(w, metrics) == (0.0, 0.0)
    assert _overview_scouts(w) == {
        "ledger_kept": True,
        "blocked_count": 0,
        "oldest_blocked_since": None,
        "oldest_blocked_age_seconds": None,
    }
    _assert_one_predicate(w, set())


def test_one_blocked_scout_is_counted_with_its_age(world, metrics):
    w = world
    _in_flight(_scout(w))  # a healthy neighbour, never counted
    _unknown_end(w)
    summary = _summary(w)
    assert summary.count == 1
    with w.observer.connect() as c:
        ended = c.execute(
            text("select ended_at from scout_admissions where scout_request_id=:r"),
            {"r": w.rid},
        ).scalar_one()
    assert summary.oldest_since == ended  # since the unknown end was recorded
    assert 0.0 <= summary.oldest_age_seconds < 120.0
    count, age = _sweep(w, metrics)
    assert count == 1.0 and 0.0 <= age < 120.0
    view = _overview_scouts(w)
    assert view["ledger_kept"] is True and view["blocked_count"] == 1
    assert view["oldest_blocked_since"] == ended
    assert 0.0 <= view["oldest_blocked_age_seconds"] < 120.0
    _assert_one_predicate(w, {w.rid})


def test_several_blocked_scouts_across_two_organizations(world, metrics):
    w = world
    org_a_second = _scout(w)
    org_b = _other_organization(w)
    org_b_second = _scout(w, org_b)
    assert org_b.org != w.org
    _unknown_end(w)
    _overdue(org_a_second, seconds_past=60)
    _legacy(org_b, hours_ago=2)  # the oldest: blocked since its admission, two hours ago
    _overdue(org_b_second, seconds_past=300)
    for tenant in (w, org_b):  # one healthy in-flight Scout in each organization
        _in_flight(_scout(w, tenant))
    expected = {w.rid, org_a_second.rid, org_b.rid, org_b_second.rid}

    summary = _summary(w)
    assert summary.count == 4
    two_hours = 2 * 3600.0
    assert two_hours <= summary.oldest_age_seconds < two_hours + 120.0
    count, age = _sweep(w, metrics)
    assert count == 4.0 and two_hours <= age < two_hours + 120.0
    view = _overview_scouts(w)
    assert view["blocked_count"] == 4
    assert two_hours <= view["oldest_blocked_age_seconds"] < two_hours + 120.0
    _assert_one_predicate(w, expected)
    # The fleet-wide count is the sum of the per-organization listings.
    assert _listed(w, organization_id=w.org) == {w.rid, org_a_second.rid}
    assert _listed(w, organization_id=org_b.org) == {org_b.rid, org_b_second.rid}


def test_the_overview_carries_no_customer_content(world):
    w = world
    _unknown_end(w)
    view = _overview_scouts(w)
    assert set(view) == {
        "ledger_kept",
        "blocked_count",
        "oldest_blocked_since",
        "oldest_blocked_age_seconds",
    }
    blob = json.dumps(view, default=str)
    with w.factory() as s:
        req = s.get(ScoutRequest, w.rid)
        identifiers = (req.id, req.organization_id, req.workspace_id, req.name, w.job_id)
    for value in identifiers:
        assert value not in blob


# --------------------------------------------------------------------------- #
# Reconciliation leaves the count; healthy operations never enter it
# --------------------------------------------------------------------------- #
def test_a_reconciled_scout_leaves_the_count(world, metrics):
    w = world
    second = _scout(w)
    first_id = _unknown_end(w)
    second_id = _overdue(second)
    assert _sweep(w, metrics)[0] == 2.0
    with w.factory() as s:
        assert resolve(s, admission_id=first_id, operator="ops", reason="provider_confirmed")
        s.commit()
    assert _sweep(w, metrics)[0] == 1.0
    _assert_one_predicate(w, {second.rid})
    with w.factory() as s:
        assert resolve(s, admission_id=second_id, operator="ops", reason="worker_crashed")
        s.commit()
    assert _sweep(w, metrics) == (0.0, 0.0)
    _assert_one_predicate(w, set())


@pytest.mark.parametrize("shape", sorted(NOT_ELIGIBLE))
def test_healthy_in_flight_operations_are_never_counted(world, metrics, shape):
    w = world
    NOT_ELIGIBLE[shape](w)
    assert _summary(w).count == 0
    assert _sweep(w, metrics) == (0.0, 0.0)
    assert _overview_scouts(w)["blocked_count"] == 0
    _assert_one_predicate(w, set())


def test_an_operation_enters_the_count_only_once_its_deadline_passes(world, metrics):
    """The deadline gate is the derivation's: strictly after the stored deadline."""
    w = world
    admission_id = _finishing_after_a_crash(w)
    _deadline_in(w, admission_id, 30)
    assert _sweep(w, metrics)[0] == 0.0
    _deadline_in(w, admission_id, -1)
    assert _sweep(w, metrics)[0] == 1.0
    _assert_one_predicate(w, {w.rid})


# --------------------------------------------------------------------------- #
# Labels, cost, index
# --------------------------------------------------------------------------- #
def test_the_gauges_carry_no_label_and_pass_validation(world, metrics):
    w = world
    for name in (SCOUTS_BLOCKED, SCOUTS_BLOCKED_OLDEST_AGE_SECONDS):
        assert name in METRIC_NAMES
        validate_metric(name, {})  # the labels the sweep sets: none
        with pytest.raises(MetricError):
            validate_metric(name, {"organization_id": w.org})  # a tenant id is refused
    assert {"organization_id", "workspace_id", "scout_request_id"}.isdisjoint(ALLOWED_LABELS)
    _unknown_end(w)
    record_blocked_scouts(w.factory)
    gauges = metrics._gauges  # the in-memory series: (name, labels) -> value
    assert set(gauges) == {
        (SCOUTS_BLOCKED, ()),
        (SCOUTS_BLOCKED_OLDEST_AGE_SECONDS, ()),
    }


def _statements(w):
    seen: list[tuple[str, object]] = []

    def _record(_conn, _cursor, statement, parameters, _context, _many):
        seen.append((statement, parameters))

    event.listen(w.engine, "before_cursor_execute", _record)
    return seen, lambda: event.remove(w.engine, "before_cursor_execute", _record)


def _listings(seen) -> list[tuple[str, object]]:
    return [(st, params) for st, params in seen if "FROM scout_admissions" in st]


@pytest.mark.parametrize("blocked_scouts_n", [0, 1, 4])
def test_one_sweep_is_one_clock_reading_and_one_statement(world, metrics, blocked_scouts_n):
    """Two ``SET LOCAL`` bounds, one clock reading, ONE listing - whatever the row count; the
    bounds end with the read's transaction (the pooled connection keeps the engine's own)."""
    w = world
    tenants = [w, *(_scout(w) for _ in range(max(blocked_scouts_n - 1, 0)))]
    for tenant in tenants[:blocked_scouts_n]:
        _unknown_end(tenant)
    _in_flight(_scout(w))
    seen, stop = _statements(w)
    try:
        record_blocked_scouts(w.factory, timeouts_ms=(2000, 5000))
    finally:
        stop()
    assert metrics.gauge_value(SCOUTS_BLOCKED) == float(blocked_scouts_n)
    statements = [statement for statement, _ in seen]
    assert len(statements) == 4, statements
    assert statements[0] == "SET LOCAL lock_timeout = '2000ms'"
    assert statements[1] == "SET LOCAL statement_timeout = '5000ms'"
    assert "clock_timestamp" in statements[2]
    [(listing, _)] = _listings(seen)
    assert listing == statements[3] and "resolved_at IS NULL" in listing
    with w.factory() as s:  # SET LOCAL did not outlive the read's transaction
        assert s.execute(text("show lock_timeout")).scalar_one() == "30s"
        assert s.execute(text("show statement_timeout")).scalar_one() == "30s"


def _index_names(plan: dict) -> set[tuple[str, str]]:
    found = set()
    stack = [plan]
    while stack:
        node = stack.pop()
        if "Index Name" in node:
            found.add((node.get("Relation Name"), node["Index Name"]))
        stack.extend(node.get("Plans", []))
    return found


def test_the_sweep_statement_reads_the_unresolved_partial_index(world, metrics):
    """With a realistic history (many resolved admissions, few outstanding) the planner reads
    the outstanding rows through ``uq_scout_admission_unresolved``, not the whole ledger."""
    w = world
    with w.observer.connect() as c:
        c.execute(
            text(
                "insert into scout_admissions (id, scout_request_id, organization_id,"
                " workspace_id, attempt, worker_id, operation, admitted_at, ended_at, end_kind,"
                " resolved_at, created_at, updated_at) select md5(g::text), :r, :o, :ws, 1,"
                " 'w-old', 'model', now() - interval '1 day', now() - interval '1 day', 'known',"
                " now() - interval '1 day', now(), now() from generate_series(1, 20000) g"
            ),
            {"r": w.rid, "o": w.org, "ws": w.ws},
        )
    _unknown_end(_scout(w))
    with w.observer.connect() as c:
        c.execute(text("analyze scout_admissions"))
    seen, stop = _statements(w)
    try:
        assert record_blocked_scouts(w.factory).count == 1
    finally:
        stop()
    [(statement, parameters)] = _listings(seen)
    with w.observer.connect() as c:
        plan = c.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters).scalar_one()
    plan = plan if isinstance(plan, list) else json.loads(plan)
    print("BLOCKED-SIGNAL-PLAN", json.dumps(plan))
    assert ("scout_admissions", "uq_scout_admission_unresolved") in _index_names(plan[0]["Plan"])


# --------------------------------------------------------------------------- #
# The emit path: the worker's registry heartbeat, with no traffic
# --------------------------------------------------------------------------- #
def _await(predicate, what: str):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_the_worker_heartbeat_sets_the_gauges_on_its_schedule_without_traffic(
    world, metrics, monkeypatch
):
    """No request is made: the worker's own heartbeat thread reads and sets the gauges every
    beat, so they follow a block and its reconciliation with nothing else running."""
    w = world
    sweeps: list[float] = []
    real = blocked_signal.blocked_scouts

    def counted(db):
        sweeps.append(time.monotonic())
        return real(db)

    monkeypatch.setattr(blocked_signal, "blocked_scouts", counted)
    settings = get_settings().model_copy(update={"worker_heartbeat_seconds": 0.05})
    worker = Worker(settings=settings, session_factory=w.factory)
    admission_id = _unknown_end(w)
    stop, thread = worker._start_registry_heartbeat()
    try:
        _await(lambda: metrics.gauge_value(SCOUTS_BLOCKED) == 1.0, "the blocked gauge")
        assert metrics.gauge_value(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS) >= 0.0
        with w.factory() as s:
            assert resolve(s, admission_id=admission_id, operator="ops", reason="worker_crashed")
            s.commit()
        _await(lambda: metrics.gauge_value(SCOUTS_BLOCKED) == 0.0, "the gauge to clear")
        _await(lambda: len(sweeps) >= 3, "three sweeps")
    finally:
        stop.set()
        thread.join(timeout=WAIT_SECONDS)
    assert not thread.is_alive()
    gaps = [b - a for a, b in zip(sweeps, sweeps[1:], strict=False)]
    assert all(gap >= 0.04 for gap in gaps), gaps  # one sweep per beat, never a busy loop
    assert metrics.gauge_value(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS) == 0.0


def test_a_lock_on_the_ledger_costs_the_heartbeat_only_the_reads_bound(
    world, metrics, monkeypatch, caplog
):
    """Review r1 MINOR-2: hold ACCESS EXCLUSIVE on ``scout_admissions`` (what a migration's DDL
    takes) for HOLD seconds, far longer than the read's bound. The sweep's read times out
    (``lock_timeout``) instead of waiting, is logged as a failed sweep, sets no gauge, and the
    registry heartbeat that shares its loop is late by at most about the bound."""
    hold = 3.0
    w = world
    _unknown_end(w)
    settings = get_settings().model_copy(
        update={"worker_heartbeat_seconds": 0.05, "worker_stale_after_seconds": 2.05}
    )
    lock_ms, statement_ms = read_timeouts_ms(heartbeat_seconds=0.05, stale_after_seconds=2.05)
    assert (lock_ms, statement_ms) == (500, 500)  # a quarter of the 2 s margin
    bound = statement_ms / 1000.0
    worker = Worker(settings=settings, session_factory=w.factory)
    worker.register()
    beats: list[float] = []
    real_heartbeat = worker._registry.heartbeat

    def counted_heartbeat(db, worker_id, **kw):
        beats.append(time.monotonic())
        return real_heartbeat(db, worker_id, **kw)

    reads: list[tuple[str, float]] = []
    real_read = blocked_signal.blocked_scouts

    def observed_read(db):
        try:
            result = real_read(db)
        except Exception as exc:
            reads.append((type(exc).__name__, time.monotonic()))
            raise
        reads.append(("ok", time.monotonic()))
        return result

    gauge_sets: list[float] = []
    real_set = blocked_signal.set_blocked_gauges

    def observed_set(backend, summary):
        gauge_sets.append(time.monotonic())
        return real_set(backend, summary)

    monkeypatch.setattr(worker._registry, "heartbeat", counted_heartbeat)
    monkeypatch.setattr(blocked_signal, "blocked_scouts", observed_read)
    monkeypatch.setattr(blocked_signal, "set_blocked_gauges", observed_set)
    caplog.set_level("WARNING", logger="signalnest.jobs.blocked_signal")
    stop, thread = worker._start_registry_heartbeat()
    try:
        _await(lambda: len(beats) >= 3 and gauge_sets, "beats and a first gauge")
        locker = w.engine.connect()
        locker.execute(text("lock table scout_admissions in access exclusive mode"))
        locked_at = time.monotonic()
        time.sleep(hold)
        released_at = time.monotonic()
        locker.rollback()
        locker.close()
        _await(lambda: any(t > released_at for t in gauge_sets), "a gauge after the release")
    finally:
        stop.set()
        thread.join(timeout=WAIT_SECONDS)
    assert not thread.is_alive()
    gaps = [b - a for a, b in zip(beats, beats[1:], strict=False)]
    in_hold = [
        b - a
        for a, b in zip(beats, beats[1:], strict=False)
        if locked_at <= b <= released_at + bound
    ]
    print(
        "BLOCKED-SIGNAL-LOCK hold", hold, "bound", bound, "max gap", round(max(gaps), 3),
        "beats in hold", len(in_hold), "timed-out reads",
        sum(1 for kind, t in reads if kind != "ok" and locked_at <= t <= released_at + bound),
    )
    # The heartbeat kept beating through the lock, each beat late by at most about the bound.
    assert len(in_hold) >= 3, in_hold
    assert max(gaps) <= bound + 0.05 + 1.0, gaps
    assert max(gaps) < hold - 1.0, gaps
    # Reads during the lock timed out (OperationalError: lock_timeout), none waited it out.
    timed_out = [t for kind, t in reads if kind == "OperationalError"]
    assert len([t for t in timed_out if locked_at <= t <= released_at + bound]) >= 2, reads
    assert not [t for kind, t in reads if kind == "ok" and locked_at + 0.2 < t < released_at]
    # ...and set no gauge: the last value stands until a read completes again.
    assert not [t for t in gauge_sets if locked_at + 0.2 < t < released_at], gauge_sets
    assert metrics.gauge_value(SCOUTS_BLOCKED) == 1.0
    messages = [r.getMessage() for r in caplog.records]
    assert "worker.blocked_scouts_sweep_failed" in messages, messages

