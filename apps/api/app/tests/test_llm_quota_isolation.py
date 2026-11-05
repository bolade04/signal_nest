"""Project Phase 5C-W4 adversarial battery, item 3: quota isolation (Phase 5C plan §4 exit
criteria 7 and 9 — "usage/quota accounting is per-tenant (no cross-tenant bleed test)").

Every day here is a **scenario day**: the ledger rows carry explicit ``created_at`` values and
every check reads an explicit clock (``check_ceilings(now=…)``, ``CeilingMeter(clock=…)``,
``request_generation(now=…)`` and ``execute_generation(meter_clock=…)`` wired in through the
route and the worker) — never the wall clock, which is how the budget tests failed on the first
of a month (#279, ``102393fa``).

* **One tenant never refuses another.** A workspace whose calls, cost or in-flight
  reservations fill its day refuses only itself: a sibling workspace of the same organization
  and another organization's workspace still pass, at the check, at the route and in the job.
  The global cost ceiling is the one shared ceiling, by design (stated).
* **The per-UTC-day boundary.** A row 1 µs before midnight counts for that day only, a row at
  midnight for the next day only, across a month end, a year end and a leap day; a clock in
  another time zone reads the UTC day; the meter and the job read the clock they are given.
* **Fail closed.** Unset, NaN, ±infinity, zero, negative, boolean, string and ``Decimal``
  ceilings refuse at the route, in a queued job and in the meter itself, with no model call.
  A huge ceiling that ``Settings`` accepts is usable at the route and in a queued job
  (DEFECT-5CW4-1, fixed in P5C-FIX2).
* **PostgreSQL: the global advisory lock never deadlocks** against an organization erasure
  that arrives *during* a generation, against another tenant's generation while one tenant's
  erasure is open, or against Scout-shaped budget-ledger traffic and the ledger sweep.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import CREATIVE_GENERATION_CEILINGS, Settings, get_settings
from app.creative import routes as creative_routes
from app.creative import service as creative_service
from app.creative.models import CreativeDraft
from app.creative.schemas import CreativeDraftCreate
from app.creative.service import GENERATE_TASK, request_generation
from app.jobs.models import Job
from app.llm import budget as ledger
from app.llm import service as service_module
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.llm.quota import (
    CEILINGS_UNSET,
    DAILY_CALLS_WORKSPACE,
    DAILY_COST_GLOBAL,
    DAILY_COST_WORKSPACE,
    CeilingMeter,
    LLMCeilingExceededError,
    check_ceilings,
    daily_usage,
    day_window,
)
from app.organizations.models import User
from app.tests._creative_support import CreativeWorld, creative_world

#: A scenario day far from today and from any midnight.
SCENARIO = datetime(2031, 3, 14, 12, 0, tzinfo=UTC)
LIMITS = {
    "llm_daily_cost_ceiling_usd_global": 10.0,
    "llm_daily_cost_ceiling_usd_per_workspace": 1.0,
    "llm_max_calls_per_workspace_per_day": 3,
    "llm_max_output_tokens_per_call": 400,
}
US = timedelta(microseconds=1)


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("w4-quota") / "quota.db", "w4q") as w:
        yield w


@pytest.fixture(autouse=True)
def limits(monkeypatch) -> None:
    for name, value in LIMITS.items():
        monkeypatch.setattr(get_settings(), name, value)


@pytest.fixture(autouse=True)
def clean_ledger(world: CreativeWorld) -> None:
    """The global ceiling sums every workspace: each test reads only its own rows."""
    with world.witness.begin() as conn:
        conn.execute(LLMUsageEvent.__table__.delete())
        conn.execute(LLMBudgetReservation.__table__.delete())


@pytest.fixture
def tenants(world: CreativeWorld):
    """Two workspaces of organization A and organization B's workspace, all enabled."""
    world.enable(world.ws_b)
    return world.workspace("quota a1"), world.workspace("quota a2"), world.ws_b


def _usage(world: CreativeWorld, ws: str, at: datetime, *, n: int = 1, cost: float = 0.0,
           task: str = GENERATE_TASK) -> None:
    with world.session() as s:
        for _ in range(n):
            s.add(LLMUsageEvent(
                organization_id=world.org_of(ws), workspace_id=ws, task=task,
                provider="anthropic", model="claude-sonnet-4-6", input_tokens=10,
                output_tokens=10, cost_usd=cost, outcome="ok", created_at=at,
            ))


def _reservation(world: CreativeWorld, ws: str, at: datetime, reserved: str) -> None:
    """A reservation still in flight (no usage row yet): it counts at its worst case."""
    with world.session() as s:
        s.add(LLMBudgetReservation(
            organization_id=world.org_of(ws), workspace_id=ws,
            period_start=ledger.period_start_for(at), attempt_key=uuid.uuid4().hex,
            reserved_usd=Decimal(reserved), settled_usd=None, state="reserved",
            expires_at=at + timedelta(minutes=5), created_at=at, usage_recorded_at=None,
        ))


def _read(world: CreativeWorld, ws: str, now: datetime):
    """The ledger's reading of ``now``'s UTC day (no ceiling applied)."""
    with world.session() as s:
        return daily_usage(s, workspace_id=ws, task=GENERATE_TASK, now=now)


def _check(world: CreativeWorld, ws: str, now: datetime, **kwargs):
    with world.session() as s:
        return check_ceilings(s, get_settings(), workspace_id=ws, task=GENERATE_TASK, now=now,
                              **kwargs)


def _refused(world: CreativeWorld, ws: str, now: datetime, **kwargs) -> str | None:
    try:
        _check(world, ws, now, **kwargs)
    except LLMCeilingExceededError as exc:
        return exc.reason
    return None


# --------------------------------------------------------------------------- #
# One tenant's spend or ceiling never refuses another's
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spent", ["calls", "cost", "in_flight_reservation"])
def test_one_workspaces_spent_day_never_refuses_another(world, tenants, spent):
    a1, a2, b = tenants
    if spent == "calls":
        _usage(world, a1, SCENARIO, n=3)
        reason = DAILY_CALLS_WORKSPACE
    elif spent == "cost":
        _usage(world, a1, SCENARIO, cost=1.0, task="classify_signal")
        reason = DAILY_COST_WORKSPACE
    else:
        _reservation(world, a1, SCENARIO, "1.0")
        reason = DAILY_COST_WORKSPACE
    assert _refused(world, a1, SCENARIO) == reason
    for other in (a2, b):
        usage = _read(world, other, SCENARIO)
        assert (usage.workspace_calls, usage.workspace_cost) == (0, Decimal(0)), other
        assert _refused(world, other, SCENARIO, worst=Decimal("0.9")) is None
    # The one shared ceiling (by design): a1's spend is part of the platform's day.
    assert _read(world, b, SCENARIO).global_cost == (
        Decimal(0) if spent == "calls" else Decimal("1.000000")
    )


def test_the_global_ceiling_is_the_only_shared_one_stated(world, tenants):
    """STATED: ``llm_daily_cost_ceiling_usd_global`` bounds model spend across every
    workspace, so a day the platform has spent refuses every tenant — the only ceiling one
    tenant's spend can reach another through. The per-workspace ceilings never do."""
    a1, a2, b = tenants
    _usage(world, a1, SCENARIO, cost=0.9, n=11, task="explain_opportunity")  # 9.90
    _reservation(world, a2, SCENARIO, "0.15")  # in flight: the platform's day is 10.05
    for ws in (a1, a2, b):
        assert _refused(world, ws, SCENARIO) == (
            DAILY_COST_WORKSPACE if ws == a1 else DAILY_COST_GLOBAL
        ), ws


@pytest.fixture
def scenario_clock(monkeypatch):
    """The route and the job read ``SCENARIO`` (or what the test sets), never the wall
    clock: the route's ``request_generation`` and the handler's ``execute_generation``
    are wrapped to receive it (no product change)."""
    clock = {"now": SCENARIO}
    real_request = creative_service.request_generation
    real_execute = creative_service.execute_generation

    def request(db, **kwargs):
        return real_request(db, now=clock["now"], **kwargs)

    def execute(ctx, **kwargs):
        return real_execute(ctx, meter_clock=lambda: clock["now"], **kwargs)

    monkeypatch.setattr(creative_routes, "request_generation", request)
    monkeypatch.setattr(creative_service, "execute_generation", execute)
    return clock


def _drain(world: CreativeWorld, clock: dict) -> None:
    """The real worker on the scenario clock (jobs enqueued at a scenario time are due at
    it; the handler's ``ctx.now`` is it too)."""
    from app.jobs.store import DurableJobStore
    from app.jobs.worker import JobRunner

    runner = JobRunner(settings=get_settings(), session_factory=world.factory,
                       store=DurableJobStore(), clock=lambda: clock["now"])
    for _ in range(20):
        if not runner.poll_once(worker_id="w4-scenario"):
            return
    raise AssertionError("worker did not reach quiescence")


class _Counter:
    def __init__(self, real):
        self.real, self.calls = real, 0
        self.name, self.model = real.name, real.model

    def generate(self, request, prompt_name, prompt_version):
        self.calls += 1
        return self.real.generate(request, prompt_name, prompt_version)


@pytest.fixture
def provider(monkeypatch) -> _Counter:
    counter = _Counter(service_module.llm_service._provider)
    monkeypatch.setattr(service_module.llm_service, "_provider", counter)
    return counter


def _post(world: CreativeWorld, ws: str, brief_id: str, channel: str = "email"):
    token = world.owner_b if ws == world.ws_b else None
    return world.send("POST", f"/workspaces/{ws}/briefs/{brief_id}/drafts", "marketer",
                      {"channel": channel}, token=token)


def test_one_tenants_ceiling_never_refuses_another_at_the_route_or_in_the_job(
    world, tenants, scenario_clock, provider
):
    a1, a2, b = tenants
    briefs = {ws: world.seed_brief(ws) for ws in (a1, a2, b)}
    # a1's job is queued first; then a1's day fills (calls) before any job runs.
    queued_a1 = _post(world, a1, briefs[a1])
    assert queued_a1.status_code == 202
    _usage(world, a1, SCENARIO, n=3)
    refused = _post(world, a1, briefs[a1], channel="sms")
    assert (refused.status_code, refused.json()["error"]["code"]) == (429, "llm_quota_exceeded")
    jobs = {ws: _post(world, ws, briefs[ws]) for ws in (a2, b)}
    assert {ws: r.status_code for ws, r in jobs.items()} == {a2: 202, b: 202}
    _drain(world, scenario_clock)
    a1_job = world.job(queued_a1.json()["job_id"])
    assert (a1_job["status"], a1_job["last_error_summary"]) == (
        "failed", f"ceiling:{DAILY_CALLS_WORKSPACE}"
    )
    for ws, r in jobs.items():
        assert world.job(r.json()["job_id"])["status"] == "succeeded", ws
    assert provider.calls == 2  # a2's and b's; a1's refused before the call
    assert world.count(CreativeDraft, workspace_id=a1) == 0


def test_a_tenants_cost_ceiling_reached_by_another_tasks_spend_stays_its_own(
    world, tenants, scenario_clock, provider
):
    """Legacy-task spend (classify/explain) counts against its own workspace's cost ceiling
    only; the sibling and the other organization still generate."""
    a1, a2, b = tenants
    _usage(world, a1, SCENARIO, cost=0.5, task="classify_signal")
    _usage(world, a1, SCENARIO, cost=0.5, task="explain_opportunity")
    assert _post(world, a1, world.seed_brief(a1)).status_code == 429
    for ws in (a2, b):
        r = _post(world, ws, world.seed_brief(ws))
        assert r.status_code == 202
        _drain(world, scenario_clock)
        assert world.job(r.json()["job_id"])["status"] == "succeeded"


# --------------------------------------------------------------------------- #
# The per-UTC-day boundary, on scenario clocks
# --------------------------------------------------------------------------- #
MIDNIGHTS = {
    "month_end": datetime(2026, 12, 1, tzinfo=UTC),
    "year_end": datetime(2027, 1, 1, tzinfo=UTC),
    "into_leap_day": datetime(2028, 2, 29, tzinfo=UTC),
    "out_of_leap_day": datetime(2028, 3, 1, tzinfo=UTC),
}


@pytest.mark.parametrize("midnight", MIDNIGHTS.values(), ids=MIDNIGHTS.keys())
def test_a_row_before_midnight_counts_for_its_day_only(world, tenants, midnight):
    ws = tenants[0]
    before = midnight - US
    _usage(world, ws, before, n=3, cost=0.4)
    _reservation(world, ws, before, "0.6")
    assert _refused(world, ws, before) == DAILY_CALLS_WORKSPACE
    assert _read(world, ws, before - timedelta(hours=23, minutes=59)).workspace_calls == 3
    for now in (midnight, midnight + US, midnight + timedelta(hours=23, minutes=59)):
        usage = _read(world, ws, now)
        assert (usage.workspace_calls, usage.workspace_cost, usage.global_cost) == (
            0, Decimal(0), Decimal(0)
        ), now


@pytest.mark.parametrize("midnight", MIDNIGHTS.values(), ids=MIDNIGHTS.keys())
def test_a_row_at_midnight_counts_for_the_new_day_only(world, tenants, midnight):
    ws = tenants[0]
    _usage(world, ws, midnight, n=3, cost=0.4)
    _reservation(world, ws, midnight, "0.6")
    assert _refused(world, ws, midnight) == DAILY_CALLS_WORKSPACE
    end_of_day = midnight + timedelta(days=1) - US
    assert _read(world, ws, end_of_day).workspace_calls == 3
    assert _read(world, ws, end_of_day).workspace_cost == Decimal("1.800000")
    usage = _read(world, ws, midnight - US)
    assert (usage.workspace_calls, usage.workspace_cost, usage.global_cost) == (
        0, Decimal(0), Decimal(0)
    )
    assert _read(world, ws, midnight + timedelta(days=1)).workspace_calls == 0


@pytest.mark.parametrize(
    "local, utc_day",
    [
        # 19:30 at UTC-05:00 on 30 November is 00:30 UTC on 1 December.
        (datetime(2026, 11, 30, 19, 30, tzinfo=timezone(timedelta(hours=-5))),
         datetime(2026, 12, 1, tzinfo=UTC)),
        # 09:00 at UTC+14:00 on 1 December is 19:00 UTC on 30 November.
        (datetime(2026, 12, 1, 9, 0, tzinfo=timezone(timedelta(hours=14))),
         datetime(2026, 11, 30, tzinfo=UTC)),
        # 23:59:59.999999 at UTC+05:30 is 18:29:59.999999 UTC the same day.
        (datetime(2026, 11, 30, 23, 59, 59, 999999,
                  tzinfo=timezone(timedelta(hours=5, minutes=30))),
         datetime(2026, 11, 30, tzinfo=UTC)),
    ],
)
def test_a_clock_in_another_time_zone_reads_the_utc_day(world, tenants, local, utc_day):
    assert day_window(local) == (utc_day, utc_day + timedelta(days=1))
    ws = tenants[0]
    _usage(world, ws, utc_day + timedelta(hours=1), n=3)
    _usage(world, ws, utc_day - timedelta(hours=1), n=1)  # the UTC day before
    assert _read(world, ws, local).workspace_calls == 3
    assert _refused(world, ws, local) == DAILY_CALLS_WORKSPACE


def test_the_meter_reads_the_clock_it_is_given(world, tenants):
    """``CeilingMeter(clock=…)`` checks the day of its own clock before every attempt."""
    ws = tenants[0]
    midnight = MIDNIGHTS["month_end"]
    _usage(world, ws, midnight - US, n=3)
    service = service_module.LLMService(MockLLMProvider(),
                                        settings=service_module.llm_service._settings)

    def attempt(now: datetime):
        with world.session() as s:
            meter = CeilingMeter(get_settings(), db=s, task=GENERATE_TASK,
                                 clock=lambda: now).bind(
                s, organization_id=world.org, workspace_id=ws, scout_request_id=None
            )
            service.run(GENERATE_TASK, {"channel": "sms"}, organization_id=world.org,
                        workspace_id=ws, meter=meter)
            s.rollback()

    with pytest.raises(LLMCeilingExceededError) as refused:
        attempt(midnight - US)
    assert refused.value.reason == DAILY_CALLS_WORKSPACE
    attempt(midnight)  # the next UTC day: nothing counted yet


def test_a_queued_job_reads_the_day_of_its_attempt_not_of_its_request(
    world, tenants, scenario_clock, provider
):
    """Requested at 23:00 with one call left; the day fills before the job runs, at
    23:59:59.999999 (refused, no call); a request at midnight generates (a fresh UTC day)."""
    ws = tenants[0]
    midnight = MIDNIGHTS["year_end"]
    _usage(world, ws, midnight - timedelta(hours=5), n=2)
    brief_id = world.seed_brief(ws)
    scenario_clock["now"] = midnight - timedelta(hours=1)
    first = _post(world, ws, brief_id)
    assert first.status_code == 202
    _usage(world, ws, midnight - timedelta(minutes=30))  # the day's last call, elsewhere
    scenario_clock["now"] = midnight - US
    _drain(world, scenario_clock)
    job = world.job(first.json()["job_id"])
    assert (job["status"], job["last_error_summary"]) == (
        "failed", f"ceiling:{DAILY_CALLS_WORKSPACE}"
    )
    assert provider.calls == 0
    assert _post(world, ws, brief_id).status_code == 429  # still the full day
    scenario_clock["now"] = midnight
    second = _post(world, ws, brief_id)
    assert second.status_code == 202
    _drain(world, scenario_clock)
    assert world.job(second.json()["job_id"])["status"] == "succeeded"
    assert provider.calls == 1


# --------------------------------------------------------------------------- #
# Unusable ceilings refuse at every layer
# --------------------------------------------------------------------------- #
UNUSABLE = [None, float("nan"), float("inf"), float("-inf"), 0, 0.0, -0.0, -1, -1e-12, True,
            False, "5", Decimal("5")]
UNUSABLE_IDS = ["none", "nan", "inf", "-inf", "int0", "float0", "-0.0", "-1", "-tiny", "True",
                "False", "str", "Decimal"]


@pytest.mark.parametrize("name", CREATIVE_GENERATION_CEILINGS)
@pytest.mark.parametrize("value", UNUSABLE, ids=UNUSABLE_IDS)
def test_an_unusable_ceiling_refuses_at_the_route_in_the_job_and_in_the_meter(
    world, monkeypatch, provider, name, value
):
    ws = world.workspace("unusable")
    brief_id = world.seed_brief(ws)
    queued = world.generate(brief_id, ws=ws).json()["job_id"]  # queued while all were usable
    monkeypatch.setattr(get_settings(), name, value)
    r = world.generate(brief_id, ws=ws, channel="sms")
    assert (r.status_code, r.json()["error"]["code"]) == (503, "creative_generation_unconfigured")
    world.drain()
    job = world.job(queued)
    assert (job["status"], job["last_error_code"], job["last_error_summary"]) == (
        "failed", "policy", "ceilings_unset"
    )
    with world.session() as s:
        meter = CeilingMeter(get_settings(), db=s, task=GENERATE_TASK,
                             clock=lambda: SCENARIO).bind(
            s, organization_id=world.org, workspace_id=ws, scout_request_id=None
        )
        with pytest.raises(LLMCeilingExceededError) as refused:
            service_module.llm_service.run(GENERATE_TASK, {"channel": "sms"},
                                           organization_id=world.org, workspace_id=ws,
                                           meter=meter)
    assert refused.value.reason == CEILINGS_UNSET
    assert provider.calls == 0
    assert world.count(LLMUsageEvent, workspace_id=ws) == 0
    assert world.count(CreativeDraft, workspace_id=ws) == 0


def test_settings_refuses_the_unusable_numbers_it_can_be_given():
    for name in CREATIVE_GENERATION_CEILINGS:
        for value in (float("nan"), float("inf"), float("-inf"), 0, -1):
            with pytest.raises(ValueError):
                Settings(**{name: value})


HUGE = [1e22, sys.float_info.max]  # finite and greater than 0: valid ceilings for Settings


@pytest.mark.parametrize(
    "name", ["llm_daily_cost_ceiling_usd_global", "llm_daily_cost_ceiling_usd_per_workspace"]
)
@pytest.mark.parametrize("value", HUGE, ids=["1e22", "float_max"])
def test_a_huge_but_valid_cost_ceiling_is_usable(world, monkeypatch, name, value):
    """DEFECT-5CW4-1 (fixed in P5C-FIX2, ``quota.ceiling_amount``): a ceiling ``Settings``
    accepts is usable at any magnitude — the route queues (it used to answer 500 from 1e22
    up) and the job generates."""
    Settings(**{name: value})  # accepted: finite and greater than 0
    monkeypatch.setattr(get_settings(), name, value)
    ws = world.workspace("huge ceiling")
    r = world.generate(world.seed_brief(ws), ws=ws)
    assert r.status_code == 202, r.text
    world.drain()
    assert world.job(r.json()["job_id"])["status"] == "succeeded"


@pytest.mark.parametrize("value", HUGE, ids=["1e22", "float_max"])
def test_a_job_queued_before_its_cost_ceilings_became_huge_generates(
    world, tenants, scenario_clock, provider, monkeypatch, value
):
    """The queued-job side of DEFECT-5CW4-1 (the route side is the test above): a job queued
    under the ordinary ceilings, whose cost ceilings both become huge before it runs, passes
    the meter's per-attempt check on its scenario day and generates once; the per-workspace
    CALL ceiling still binds the same day (a huge cost ceiling lifts nothing else)."""
    ws = tenants[0]
    brief_id = world.seed_brief(ws)
    queued = _post(world, ws, brief_id)
    assert queued.status_code == 202
    for name in ("llm_daily_cost_ceiling_usd_global", "llm_daily_cost_ceiling_usd_per_workspace"):
        monkeypatch.setattr(get_settings(), name, value)
    _usage(world, ws, SCENARIO, n=2, cost=1000.0)  # far above the ordinary cost ceilings
    _drain(world, scenario_clock)
    job = world.job(queued.json()["job_id"])
    assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
    assert provider.calls == 1
    _usage(world, ws, SCENARIO)  # the scenario day's third generation call
    refused = _post(world, ws, brief_id, channel="sms")
    assert (refused.status_code, refused.json()["error"]["code"]) == (429, "llm_quota_exceeded")


# --------------------------------------------------------------------------- #
# PostgreSQL: the global advisory lock never deadlocks
# --------------------------------------------------------------------------- #
class _SlowPriced:
    """An offline fake priced like a Claude model; each call waits for ``release`` so the
    generation holds its locks while the test acts."""

    name = "anthropic"
    model = "claude-sonnet-4-6"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def prepare(self, request):
        from app.llm.providers_real import AnthropicProvider

        return AnthropicProvider("k", self.model, 5).prepare(request)

    def generate(self, request, prompt_name, prompt_version):
        from app.llm.base import LLMResponse, LLMUsage

        self.calls += 1
        self.started.set()
        assert self.release.wait(60), "the test never released the call"
        out = MockLLMProvider().generate(request, prompt_name, prompt_version).output
        return LLMResponse(task=request.task, provider=self.name, model=self.model, output=out,
                           usage=LLMUsage(input_tokens=1000, output_tokens=200),
                           prompt_name=prompt_name, prompt_version=prompt_version)


@pytest.fixture
def slow_priced(monkeypatch) -> _SlowPriced:
    prices = {_SlowPriced.model: [3.0, 15.0]}
    for settings in (get_settings(), service_module.llm_service._settings):
        monkeypatch.setattr(settings, "llm_prices_usd_per_mtok", prices)
    monkeypatch.setattr(get_settings(), "llm_workspace_monthly_budget_usd", 50.0)
    provider = _SlowPriced()
    monkeypatch.setattr(service_module.llm_service, "_provider", provider)
    return provider


def _blocked_by(world, pid: int) -> list:
    with world.witness.connect() as conn:
        return conn.execute(
            text("select pid from pg_stat_activity where datname = current_database()"
                 " and :p = any(pg_blocking_pids(pid))"), {"p": pid},
        ).all()


def _cancel_all(world) -> None:
    with world.witness.connect() as conn:
        conn.execute(text("select pg_cancel_backend(pid) from pg_stat_activity"
                          " where datname = current_database() and pid <> pg_backend_pid()"))


def _await(predicate, what: str, seconds: float = 30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _recording_outcomes(monkeypatch) -> list:
    """Every ``execute_generation`` outcome (the job rows may be erased with their
    organization): a result summary, or the job error's summary."""
    from app.jobs.status import JobExecutionError

    outcomes: list = []
    real = creative_service.execute_generation

    def execute(ctx, **kwargs):
        try:
            result = real(ctx, **kwargs)
        except JobExecutionError as exc:
            outcomes.append((ctx.context.workspace_id, exc.error.summary))
            raise
        outcomes.append((ctx.context.workspace_id, result["outcome"]))
        return result

    monkeypatch.setattr(creative_service, "execute_generation", execute)
    return outcomes


def test_postgres_an_erasure_arriving_during_a_generation_waits_and_completes(
    monkeypatch, slow_priced
):
    """The reverse of r1's P8 order: the generation holds its locks (organization KEY SHARE,
    the global advisory lock, the workspace NO KEY UPDATE) across a priced call whose ledger
    session reserves and settles on its own connection; the organization's erasure arrives
    meanwhile, locks the organization (it waits), and erases once the generation commits.
    Neither waits on the other undetected."""
    from app.organizations.members import lock_organization
    from app.organizations.models import Organization
    from app.privacy import erasure
    from app.tests._creative_support import postgres_creative_world

    outcomes = _recording_outcomes(monkeypatch)
    with postgres_creative_world("w4erase2") as world:
        world.enable()
        job_id = world.generate(world.seed_brief()).json()["job_id"]
        generation = threading.Thread(target=world.drain, daemon=True)
        generation.start()
        assert slow_priced.started.wait(30), "the generation never called the model"
        x = world.factory()
        x_pid = x.execute(text("select pg_backend_pid()")).scalar_one()
        result: dict = {}

        def erase():
            try:
                lock_organization(x, world.org)
                result["locked"] = time.monotonic()
                erasure.erase_organization(x, organization_id=world.org)
                x.commit()
                result["erasure"] = "committed"
            except Exception as exc:  # noqa: BLE001 - reported below
                result["erasure"] = type(exc).__name__
                x.rollback()

        eraser = threading.Thread(target=erase, daemon=True)
        eraser.start()
        _await(lambda: any(r for r in _blocking_pids(world, x_pid)), "the erasure to wait")
        assert "locked" not in result  # it waits for the generation's organization lock
        released = time.monotonic()
        slow_priced.release.set()
        generation.join(60)
        eraser.join(60)
        hung = generation.is_alive() or eraser.is_alive()
        if hung:
            _cancel_all(world)
            generation.join(30)
            eraser.join(30)
        x.close()
        assert not hung, "generation and organization erasure waited on each other"
        assert outcomes == [(world.ws, "draft_created")]
        assert result["erasure"] == "committed" and result["locked"] > released
        assert world.row(Job, job_id) is None
        assert world.row(Organization, world.org) is None


def _blocking_pids(world, pid: int) -> list:
    with world.witness.connect() as conn:
        return list(conn.execute(text("select unnest(pg_blocking_pids(:p))"), {"p": pid})
                    .scalars())


def test_postgres_another_tenants_generation_proceeds_while_one_tenants_erasure_is_open(
    monkeypatch,
):
    """Organization A's erasure holds A's row FOR UPDATE (not yet committed) and A's job is
    claimed: A's generation waits at its FIRST lock (the organization), before the global
    advisory lock, so organization B's generation is not queued behind A's erasure. Then the
    erasure commits and A's job ends without a draft."""
    from app.organizations.members import lock_organization
    from app.privacy import erasure
    from app.tests._creative_support import postgres_creative_world

    for name, value in {**LIMITS, "llm_max_calls_per_workspace_per_day": 100}.items():
        monkeypatch.setattr(get_settings(), name, value)
    outcomes = _recording_outcomes(monkeypatch)
    with postgres_creative_world("w4eraseb") as world:
        world.enable()
        world.enable(world.ws_b)
        job_a = world.generate(world.seed_brief()).json()["job_id"]
        brief_b = world.seed_brief(world.ws_b)
        x = world.factory()
        lock_organization(x, world.org)
        erasure._require_no_active_jobs(x, world.org)  # passes: A's job is still pending
        x_pid = x.execute(text("select pg_backend_pid()")).scalar_one()
        worker_a = threading.Thread(target=world.drain, daemon=True)
        worker_a.start()
        _await(lambda: _blocked_by(world, x_pid), "A's generation to wait on the erasure")
        r = world.send("POST", f"/workspaces/{world.ws_b}/briefs/{brief_b}/drafts",
                       body={"channel": "email"}, token=world.owner_b)
        assert r.status_code == 202, r.text
        worker_b = threading.Thread(target=world.drain, daemon=True)
        worker_b.start()
        worker_b.join(60)
        b_done = not worker_b.is_alive()
        b_job = world.job(r.json()["job_id"])
        monkeypatch.setattr(erasure, "_require_no_active_jobs", lambda db, org: None)
        erasure.erase_organization(x, organization_id=world.org)
        x.commit()
        x.close()
        worker_a.join(60)
        hung = worker_a.is_alive()
        if hung:
            _cancel_all(world)
            worker_a.join(30)
        assert b_done, "organization B's generation queued behind organization A's erasure"
        assert b_job["status"] == "succeeded", (b_job["last_error_code"],
                                                b_job["last_error_summary"])
        assert not hung
        assert world.row(Job, job_a) is None
        assert (world.ws_b, "draft_created") in outcomes
        assert not any(ws == world.ws and o == "draft_created" for ws, o in outcomes)
        assert world.count(CreativeDraft, workspace_id=world.ws_b) == 1


def test_postgres_scout_shaped_ledger_traffic_and_the_sweep_never_wait_on_a_generation(
    monkeypatch, slow_priced
):
    """While a priced generation holds every generation lock across its call, the budget
    ledger's own short transactions (a Scout-shaped reserve and settle on the same workspace
    and on another organization's, and the recovery sweep) complete without waiting on it;
    the generation then completes."""
    from app.tests._creative_support import postgres_creative_world

    with postgres_creative_world("w4ledger") as world:
        world.enable()
        job_id = world.generate(world.seed_brief()).json()["job_id"]
        generation = threading.Thread(target=world.drain, daemon=True)
        generation.start()
        assert slow_priced.started.wait(30)
        timings: dict[str, float] = {}
        errors: dict[str, str] = {}

        def ledger_traffic():
            for label, ws, org in (("same_workspace", world.ws, world.org),
                                   ("other_organization", world.ws_b, world.org_b)):
                key = uuid.uuid4().hex
                now = datetime.now(UTC)
                start = time.monotonic()
                s = world.factory()
                try:
                    ok = ledger.reserve(
                        s, workspace_id=ws, organization_id=org, job_id=None, attempt_key=key,
                        worst_usd=Decimal("0.01"), budget_usd=Decimal("50"),
                        expires_at=now + timedelta(minutes=5), now=now,
                    )
                    s.commit()
                    assert ok
                    ledger.settle(s, attempt_key=key, settled_usd=Decimal("0.005"), now=now)
                    s.commit()
                    timings[label] = time.monotonic() - start
                except Exception as exc:  # noqa: BLE001 - reported below
                    errors[label] = repr(exc)
                    s.rollback()
                finally:
                    s.close()
            start = time.monotonic()
            s = world.factory()
            try:
                ledger.sweep(s, now=datetime.now(UTC))
                s.commit()
                timings["sweep"] = time.monotonic() - start
            except Exception as exc:  # noqa: BLE001
                errors["sweep"] = repr(exc)
                s.rollback()
            finally:
                s.close()

        traffic = threading.Thread(target=ledger_traffic, daemon=True)
        traffic.start()
        traffic.join(20)
        waited = traffic.is_alive()
        slow_priced.release.set()
        generation.join(60)
        if waited or generation.is_alive():
            _cancel_all(world)
            traffic.join(30)
            generation.join(30)
        assert not waited, "ledger traffic waited on the generation's locks"
        assert errors == {}
        assert set(timings) == {"same_workspace", "other_organization", "sweep"}
        assert max(timings.values()) < 10.0, timings
        job = world.job(job_id)
        assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
        assert world.count(CreativeDraft, workspace_id=world.ws) == 1


def test_postgres_the_utc_day_boundary_holds_on_timestamptz(monkeypatch):
    """The boundary on PostgreSQL's ``timestamptz`` (the production type), not only SQLite's
    stored strings: a row 1 µs before midnight and a row at midnight, read from either side,
    and from a clock five hours behind UTC."""
    from app.tests._creative_support import postgres_creative_world

    midnight = MIDNIGHTS["month_end"]
    with postgres_creative_world("w4day") as world:
        _usage(world, world.ws, midnight - US, n=3, cost=0.25)
        _usage(world, world.ws, midnight, n=2, cost=0.5)
        _reservation(world, world.ws, midnight - US, "0.2")
        _reservation(world, world.ws, midnight, "0.1")
        before = _read(world, world.ws, midnight - US)
        after = _read(world, world.ws, midnight)
        assert (before.workspace_calls, before.workspace_cost) == (3, Decimal("0.950000"))
        assert (after.workspace_calls, after.workspace_cost) == (2, Decimal("1.100000"))
        local = midnight.astimezone(timezone(timedelta(hours=-5)))  # 19:00 the day before
        assert _read(world, world.ws, local).workspace_calls == 2
        assert _refused(world, world.ws, midnight - US) == DAILY_CALLS_WORKSPACE
        assert _refused(world, world.ws, midnight) == DAILY_COST_WORKSPACE


# --------------------------------------------------------------------------- #
# The route's typed refusal never names whose ceiling it was
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reason", ["workspace_calls", "workspace_cost", "global_cost"])
def test_every_ceiling_answers_the_same_429_naming_no_tenant(world, tenants, scenario_clock,
                                                              reason):
    a1, a2, b = tenants
    if reason == "workspace_calls":
        _usage(world, a1, SCENARIO, n=3)
    elif reason == "workspace_cost":
        _usage(world, a1, SCENARIO, cost=1.0)
    else:
        _usage(world, b, SCENARIO, cost=10.0, task="classify_signal")  # platform day spent
    r = _post(world, a1, world.seed_brief(a1))
    assert r.status_code == 429
    error = r.json()["error"]
    assert (error["code"], error["message"]) == (
        "llm_quota_exceeded",
        "The daily limit for generated drafts has been reached. Try again tomorrow.",
    )
    for leak in (world.org_b, b, a2, "global", "workspace"):
        assert leak not in r.text


def test_requests_at_the_route_read_the_scenario_day(world, tenants):
    """The route's check takes ``now`` (the request path passes it explicitly here)."""
    ws = tenants[0]
    _usage(world, ws, SCENARIO, n=3)
    with world.session() as s:
        brief = s.get(creative_service.RecommendationBrief, world.seed_brief(ws))
        user = s.get(User, world.users["marketer"])
        with pytest.raises(creative_service.CreativeQuotaExceededError):
            request_generation(s, brief=brief, body=CreativeDraftCreate(channel="email"),
                               requested_by=user, settings=get_settings(), now=SCENARIO)
        job = request_generation(s, brief=brief, body=CreativeDraftCreate(channel="email"),
                                 requested_by=user, settings=get_settings(),
                                 now=SCENARIO + timedelta(days=1))
        assert job.id
        s.rollback()

