"""Project Phase 5C (W21 lane P5C): generation ceilings and usage accounting.

The 5C-W1 cross-authored slice (``test_llm_quota_behaviour.py``, Phase 5C plan §5) for the
seam's quota code (:mod:`app.llm.quota`), on the binding coordinator decisions:

* **Fail closed while unset.** Every ceiling defaults to ``None``; while any is unset the
  generate route answers 503 ``creative_generation_unconfigured`` and a job already queued
  refuses (``POLICY ceilings_unset``) before any model call.
* **Refuse at or before breach.** A ceiling already reached, or one the next attempt's worst
  case could cross, refuses with the typed error (route 429 ``llm_quota_exceeded``; job
  ``POLICY ceiling:<reason>``) and no call is made — per workspace (calls, cost) and globally
  (cost), per UTC day, counted from the ledger (``llm_usage_events`` plus the charge of
  reservations with no usage row), never from a draft's snapshot (R5).
* **Under a lock.** On PostgreSQL concurrent jobs cannot both pass a check only one fits.
* **Usage rows for every call, legacy tasks included** (exit criteria 2(a) and 7): already
  delivered by Phase 6 (P6-LLM-4, coordinator decision I-4: no ``llm_usage_records``); proven
  here on the existing writes.
* The output-token bound is ``min(llm_max_output_tokens, llm_max_output_tokens_per_call)``
  (``test_creative_generation``).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql, sqlite

from app.core.config import CREATIVE_GENERATION_CEILINGS, get_settings
from app.creative.models import CreativeDraft
from app.creative.service import GENERATE_TASK
from app.jobs import pipeline
from app.llm import budget as ledger
from app.llm import service as service_module
from app.llm.metering import LLMMeter
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.llm.quota import (
    CEILINGS_UNSET,
    DAILY_CALLS_WORKSPACE,
    DAILY_COST_GLOBAL,
    DAILY_COST_WORKSPACE,
    LLMCeilingExceededError,
    check_ceilings,
    day_window,
    global_lock_statement,
    lock_for_generation,
    workspace_lock_select,
)
from app.llm.service import LLMService
from app.tests._creative_support import CEILINGS, CreativeWorld, creative_world

SETTINGS_CEILINGS = {
    "llm_daily_cost_ceiling_usd_global": 10.0,
    "llm_daily_cost_ceiling_usd_per_workspace": 1.0,
    "llm_max_calls_per_workspace_per_day": 3,
    "llm_max_output_tokens_per_call": 400,
}


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("p5c-quota") / "quota.db", "p5cq") as w:
        yield w


@pytest.fixture(autouse=True)
def ceilings(monkeypatch):
    for name, value in SETTINGS_CEILINGS.items():
        monkeypatch.setattr(get_settings(), name, value)


@pytest.fixture(autouse=True)
def clean_ledger(world: CreativeWorld) -> None:
    """Each test reads only its own day: the global ceiling would otherwise count every
    earlier test's spend in this module's shared database."""
    with world.witness.begin() as conn:
        conn.execute(LLMUsageEvent.__table__.delete())
        conn.execute(LLMBudgetReservation.__table__.delete())


@pytest.fixture
def ws(world: CreativeWorld) -> str:
    return world.workspace("quota")


class _Counter:
    def __init__(self, real, delay: float = 0.0):
        self.real, self.delay, self.calls = real, delay, 0
        self.name, self.model = real.name, real.model

    def generate(self, request, prompt_name, prompt_version):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.real.generate(request, prompt_name, prompt_version)


@pytest.fixture
def provider(monkeypatch) -> _Counter:
    counter = _Counter(service_module.llm_service._provider)
    monkeypatch.setattr(service_module.llm_service, "_provider", counter)
    return counter


def _usage(world: CreativeWorld, ws: str, *, task: str = GENERATE_TASK, cost: float = 0.0,
           at: datetime | None = None, n: int = 1, org: str | None = None) -> None:
    with world.session() as s:
        for _ in range(n):
            s.add(LLMUsageEvent(
                organization_id=org or world.org_of(ws), workspace_id=ws, task=task,
                provider="anthropic", model="claude-sonnet-4-6", input_tokens=10,
                output_tokens=10, cost_usd=cost, outcome="ok",
                created_at=at or datetime.now(UTC),
            ))


def _reservation(world: CreativeWorld, ws: str, *, reserved: str, settled: str | None,
                 recorded: bool, at: datetime | None = None) -> None:
    now = at or datetime.now(UTC)
    with world.session() as s:
        s.add(LLMBudgetReservation(
            organization_id=world.org_of(ws), workspace_id=ws,
            period_start=ledger.period_start_for(now), attempt_key=uuid.uuid4().hex,
            reserved_usd=Decimal(reserved),
            settled_usd=None if settled is None else Decimal(settled),
            state="reserved" if settled is None else "released",
            expires_at=now + timedelta(minutes=5), created_at=now,
            usage_recorded_at=now if recorded else None,
        ))


def _check(world: CreativeWorld, ws: str, **kwargs):
    with world.session() as s:
        return check_ceilings(s, get_settings(), workspace_id=ws, task=GENERATE_TASK,
                              now=kwargs.pop("now", datetime.now(UTC)), **kwargs)


def _refused(world: CreativeWorld, ws: str, **kwargs) -> str:
    with pytest.raises(LLMCeilingExceededError) as refused:
        _check(world, ws, **kwargs)
    return refused.value.reason


# --------------------------------------------------------------------------- #
# Fail closed while any ceiling is unset ("ceilings unset still generates" mutant)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("unset", CREATIVE_GENERATION_CEILINGS)
def test_the_route_refuses_while_any_ceiling_is_unset(world, ws, monkeypatch, provider, unset):
    monkeypatch.setattr(get_settings(), unset, None)
    brief_id = world.seed_brief(ws)
    r = world.generate(brief_id, ws=ws)
    err = r.json()["error"]
    assert (r.status_code, err["code"]) == (503, "creative_generation_unconfigured")
    assert world.count(CreativeDraft, workspace_id=ws) == 0
    from app.jobs.models import Job

    assert world.count(Job, workspace_id=ws) == 0
    assert provider.calls == 0
    assert _refused(world, ws) == CEILINGS_UNSET


@pytest.mark.parametrize("unset", CREATIVE_GENERATION_CEILINGS)
def test_a_queued_job_refuses_when_a_ceiling_was_unset_after_enqueue(
    world, ws, monkeypatch, provider, unset
):
    brief_id = world.seed_brief(ws)
    job_id = world.generate(brief_id, ws=ws).json()["job_id"]
    monkeypatch.setattr(get_settings(), unset, None)
    world.drain()
    job = world.job(job_id)
    assert (job["status"], job["last_error_code"], job["last_error_summary"]) == (
        "failed", "policy", "ceilings_unset"
    )
    assert provider.calls == 0
    assert world.count(CreativeDraft, workspace_id=ws) == 0
    assert world.count(LLMUsageEvent, workspace_id=ws) == 0


def test_the_meter_itself_refuses_while_a_ceiling_is_unset(world, ws, monkeypatch, provider):
    """Defence in depth: the per-attempt check refuses even if a caller skipped the
    up-front one."""
    from app.llm.quota import CeilingMeter

    monkeypatch.setattr(get_settings(), "llm_max_calls_per_workspace_per_day", None)
    with world.session() as s:
        meter = CeilingMeter(get_settings(), db=s, task=GENERATE_TASK).bind(
            s, organization_id=world.org, workspace_id=ws, scout_request_id=None
        )
        with pytest.raises(LLMCeilingExceededError) as refused:
            service_module.llm_service.run(GENERATE_TASK, {"channel": "sms"}, meter=meter)
    assert refused.value.reason == CEILINGS_UNSET
    assert provider.calls == 0


# --------------------------------------------------------------------------- #
# Each ceiling, read from the ledger
# --------------------------------------------------------------------------- #
def test_the_workspace_call_ceiling_counts_generation_calls_today(world, ws):
    _usage(world, ws, n=2)
    _usage(world, ws, task="classify_signal", n=5)  # other tasks: not generation calls
    _usage(world, ws, n=4, at=datetime.now(UTC) - timedelta(days=1))  # yesterday
    _usage(world, world.workspace("quota other"), n=5)  # another workspace
    assert _check(world, ws).workspace_calls == 2
    assert _refused(world, ws, pending_calls=1) == DAILY_CALLS_WORKSPACE
    _usage(world, ws)
    assert _refused(world, ws) == DAILY_CALLS_WORKSPACE


def test_the_workspace_cost_ceiling_counts_every_task_and_unrecorded_reservations(world, ws):
    _usage(world, ws, task="classify_signal", cost=0.4)
    _usage(world, ws, cost=0.3, at=datetime.now(UTC) - timedelta(days=1))  # yesterday
    _reservation(world, ws, reserved="0.2", settled=None, recorded=False)  # in flight
    _reservation(world, ws, reserved="0.25", settled="0.25", recorded=False)  # released
    _reservation(world, ws, reserved="9.0", settled="0.1", recorded=True)  # has a usage row
    usage = _check(world, ws)
    assert usage.workspace_cost == Decimal("0.850000")
    assert _refused(world, ws, worst=Decimal("0.2")) == DAILY_COST_WORKSPACE  # would cross
    _check(world, ws, worst=Decimal("0.15"))  # fits exactly: 1.0
    assert _refused(world, ws, pending_cost=Decimal("0.15")) == DAILY_COST_WORKSPACE  # reached


def test_the_global_cost_ceiling_counts_every_workspace(world, ws):
    for n in range(3):
        _usage(world, world.workspace(f"global {n}"), cost=3.0)
    _usage(world, world.ws_b, cost=0.5, org=world.org_b)  # another organization counts too
    reason = _refused(world, ws, worst=Decimal("0.6"))
    assert reason == DAILY_COST_GLOBAL
    _check(world, ws, worst=Decimal("0.4"))  # this workspace's own spend is 0


def test_the_day_is_the_utc_calendar_day(world, ws):
    start, end = day_window(datetime(2026, 11, 1, 23, 59, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC))
    late = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    _usage(world, ws, n=3, at=late - timedelta(microseconds=1))  # the previous day
    assert _check(world, ws).workspace_calls == 0


def test_quotas_read_the_ledger_never_a_drafts_snapshot(world, ws):
    """R5: a draft's usage snapshot is provenance only; with no ledger rows nothing counts."""
    brief_id = world.seed_brief(ws)
    assert world.generated(brief_id, ws=ws)["status"] == "succeeded"
    with world.witness.begin() as conn:
        conn.execute(LLMUsageEvent.__table__.delete().where(
            LLMUsageEvent.__table__.c.workspace_id == ws
        ))
    with world.session() as s:  # a huge snapshot, through Core (the table is append-only)
        draft = s.query(CreativeDraft).filter_by(workspace_id=ws).one()
        s.execute(
            CreativeDraft.__table__.insert().values(
                **{c.name: getattr(draft, c.name) for c in CreativeDraft.__table__.columns
                   if c.name not in ("id", "draft_version", "estimated_cost_usd",
                                     "input_tokens", "output_tokens", "llm_usage_event_id")},
                id=uuid.uuid4().hex, draft_version=2, estimated_cost_usd=Decimal("999"),
                input_tokens=10**6, output_tokens=10**6,
            )
        )
    usage = _check(world, ws)
    assert (usage.workspace_calls, usage.workspace_cost) == (0, Decimal("0"))


# --------------------------------------------------------------------------- #
# Through the route and the job
# --------------------------------------------------------------------------- #
def test_the_route_answers_429_once_a_ceiling_is_reached(world, ws, provider):
    brief_id = world.seed_brief(ws)
    _usage(world, ws, n=3)
    r = world.generate(brief_id, ws=ws)
    err = r.json()["error"]
    assert (r.status_code, err["code"]) == (429, "llm_quota_exceeded")
    assert "global" not in err["message"].lower()  # never discloses platform-wide spend
    assert provider.calls == 0


def test_the_job_refuses_a_ceiling_reached_after_enqueue(world, ws, provider):
    brief_id = world.seed_brief(ws)
    job_id = world.generate(brief_id, ws=ws).json()["job_id"]
    _usage(world, ws, cost=1.0)  # the workspace's day is spent meanwhile
    world.drain()
    job = world.job(job_id)
    assert (job["status"], job["last_error_code"], job["last_error_summary"]) == (
        "failed", "policy", f"ceiling:{DAILY_COST_WORKSPACE}"
    )
    assert provider.calls == 0
    assert world.count(CreativeDraft, workspace_id=ws) == 0


def test_generation_stops_at_the_call_ceiling(world, ws, provider):
    brief_id = world.seed_brief(ws)
    for _ in range(3):
        assert world.generated(brief_id, ws=ws)["status"] == "succeeded"
    assert world.count(LLMUsageEvent, workspace_id=ws, task=GENERATE_TASK) == 3
    assert world.generate(brief_id, ws=ws).status_code == 429
    assert provider.calls == 3


def test_retries_inside_one_job_count_against_the_call_ceiling(world, ws, monkeypatch):
    """The meter's unwritten attempts count: with two calls left, a provider that keeps timing
    out gets two attempts, and the third is refused before it is made."""
    from app.llm.base import LLMTimeoutError

    settings = service_module.llm_service._settings
    monkeypatch.setattr(settings, "llm_max_retries", 5)
    monkeypatch.setattr(service_module.llm_service, "_sleep", lambda _s: None)
    calls = []

    class _Timeout:
        name, model = "mock", "mock-1"

        def generate(self, request, prompt_name, prompt_version):
            calls.append(1)
            raise LLMTimeoutError("simulated")

    monkeypatch.setattr(service_module.llm_service, "_provider", _Timeout())
    _usage(world, ws)  # one of three used
    job = world.generated(world.seed_brief(ws), ws=ws)
    assert (job["last_error_code"], job["last_error_summary"]) == (
        "policy", f"ceiling:{DAILY_CALLS_WORKSPACE}"
    )
    assert len(calls) == 2
    assert world.count(LLMUsageEvent, job_id=job["id"]) == 2  # both attempts recorded


# --------------------------------------------------------------------------- #
# Exit criteria 2(a) / 7: usage rows for every call, legacy tasks included (Phase 6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "task, inputs",
    [
        ("classify_signal", {"content": "where can I buy coffee?", "audiences": None}),
        ("explain_opportunity", {"resolved_market": "Dallas", "audience_fit": "parents",
                                 "pain_point_dna": "price_frustration",
                                 "safe_positioning": "Fair pricing."}),
        (GENERATE_TASK, {"channel": "sms", "summary": "Hi"}),
    ],
)
def test_every_metered_call_writes_one_usage_row_per_attempt(world, ws, task, inputs):
    """Already delivered by P6-LLM-4 (coordinator decision I-4): ``LLMService.run`` charges
    every attempt through the meter (``app.llm.service`` ``charge``/``after_attempt``), and
    :meth:`LLMMeter.flush` (same-session path) or settlement (shared ledger) writes one
    ``llm_usage_events`` row per attempt — for the legacy tasks exactly as for generation."""
    service = LLMService(MockLLMProvider(), settings=service_module.llm_service._settings)
    with world.session() as s:
        meter = LLMMeter(get_settings()).bind(
            s, organization_id=world.org, workspace_id=ws, scout_request_id=None
        )
        service.run(task, inputs, meter=meter)
        meter.flush(s)
    [row] = world.rows(LLMUsageEvent, workspace_id=ws)
    assert (row["task"], row["provider"], row["outcome"]) == (task, "mock", "ok")
    assert row["organization_id"] == world.org


def test_both_scouting_call_sites_pass_the_runs_meter():
    """The legacy calls are metered at the pipeline's two call sites (``jobs/pipeline.py``):
    each ``llm_service.run`` passes ``meter=meter``."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pipeline))
    runs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "run"
        and getattr(getattr(node.func, "value", None), "id", None) == "llm_service"
    ]
    assert len(runs) == 2
    for call in runs:
        assert any(k.arg == "meter" for k in call.keywords)


def test_a_generation_jobs_rows_carry_its_job_and_tenant(world, ws):
    job = world.generated(world.seed_brief(ws), ws=ws)
    [row] = world.rows(LLMUsageEvent, job_id=job["id"])
    assert (row["task"], row["workspace_id"], row["organization_id"]) == (
        GENERATE_TASK, ws, world.org
    )


# --------------------------------------------------------------------------- #
# The lock
# --------------------------------------------------------------------------- #
def test_the_locks_compile_as_documented():
    pg = postgresql.dialect()
    workspace = str(workspace_lock_select("w").compile(dialect=pg))
    assert workspace.rstrip().endswith("FOR NO KEY UPDATE")
    assert "pg_advisory_xact_lock" in str(global_lock_statement().compile(dialect=pg))
    assert "FOR" not in str(workspace_lock_select("w").compile(dialect=sqlite.dialect()))


def test_sqlite_takes_no_advisory_lock(world, ws):
    seen: list[str] = []
    with world.session() as s:
        from sqlalchemy import event

        @event.listens_for(s.get_bind(), "before_cursor_execute")
        def _record(conn, cursor, statement, *args):  # pragma: no cover - hook
            seen.append(statement)

        try:
            assert lock_for_generation(s, ws) is True
            assert lock_for_generation(s, "missing-workspace") is False
        finally:
            event.remove(s.get_bind(), "before_cursor_execute", _record)
    assert not any("advisory" in st for st in seen)


# --------------------------------------------------------------------------- #
# PostgreSQL: the ceiling holds under concurrency; the ledger path runs under the lock
# --------------------------------------------------------------------------- #
def test_postgres_concurrent_jobs_cannot_both_pass_the_last_call(monkeypatch):
    from app.tests._creative_support import postgres_creative_world

    monkeypatch.setattr(get_settings(), "llm_max_calls_per_workspace_per_day", 1)
    slow = _Counter(service_module.llm_service._provider, delay=1.0)
    monkeypatch.setattr(service_module.llm_service, "_provider", slow)
    with postgres_creative_world("p5crace") as world:
        world.enable()
        brief_id = world.seed_brief()
        jobs = [world.generate(brief_id, channel=c).json()["job_id"] for c in ("email", "sms")]
        threads = [threading.Thread(target=world.drain) for _ in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads)
        outcomes = sorted(
            (world.job(j)["status"], world.job(j)["last_error_summary"]) for j in jobs
        )
        assert outcomes == [
            ("failed", f"ceiling:{DAILY_CALLS_WORKSPACE}"), ("succeeded", None)
        ]
        assert world.count(CreativeDraft, workspace_id=world.ws) == 1
        assert slow.calls == 1


def test_postgres_a_priced_call_reserves_through_the_ledger_under_the_lock(monkeypatch):
    """The shared-ledger path (a priced provider on PostgreSQL) reserves and settles on a
    second session while the workspace row is locked: ``FOR NO KEY UPDATE`` does not block
    the ledger's foreign-key checks, so the job completes (no self-deadlock)."""
    from app.llm.base import LLMResponse, LLMUsage
    from app.tests._creative_support import postgres_creative_world

    model = "claude-sonnet-4-6"
    prices = {model: [3.0, 15.0]}
    for settings in (get_settings(), service_module.llm_service._settings):
        monkeypatch.setattr(settings, "llm_prices_usd_per_mtok", prices)
    monkeypatch.setattr(get_settings(), "llm_workspace_monthly_budget_usd", 50.0)

    class _Priced:
        name = "anthropic"
        model = "claude-sonnet-4-6"

        def prepare(self, request):
            from app.llm.providers_real import AnthropicProvider

            return AnthropicProvider("k", model, 5).prepare(request)

        def generate(self, request, prompt_name, prompt_version):
            out = MockLLMProvider().generate(request, prompt_name, prompt_version).output
            return LLMResponse(
                task=request.task, provider=self.name, model=model, output=out,
                usage=LLMUsage(input_tokens=1000, output_tokens=200),
                prompt_name=prompt_name, prompt_version=prompt_version,
            )

    monkeypatch.setattr(service_module.llm_service, "_provider", _Priced())
    with postgres_creative_world("p5cledger") as world:
        world.enable()
        job_id = world.generate(world.seed_brief()).json()["job_id"]
        worker = threading.Thread(target=world.drain)
        worker.start()
        worker.join(60)
        assert not worker.is_alive(), "the job deadlocked against its own ledger session"
        job = world.job(job_id)
        assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
        [reservation] = world.rows(LLMBudgetReservation, workspace_id=world.ws)
        assert reservation["state"] == "settled" and reservation["job_id"] == job_id
        [draft] = world.rows(CreativeDraft, workspace_id=world.ws)
        assert draft["estimated_cost_usd"] == Decimal("0.006000")


def test_the_settings_fixture_matches_the_ceiling_names():
    assert set(SETTINGS_CEILINGS) == set(CREATIVE_GENERATION_CEILINGS) == set(CEILINGS)
