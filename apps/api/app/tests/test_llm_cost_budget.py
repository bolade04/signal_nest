"""P6-LLM-4: cost accounting, per-run cap, per-workspace budget, refusal, tenant reference.

Driven through the real Anthropic adapter over recorded replay (no network). Asserts:
costs come from the configured price table (unknown models fail closed to the highest
rate, the mock is free); every attempt is recorded in ``llm_usage_events`` with no
content; the per-run cap and the monthly budget stop calls before they are made and
the affected output is marked ``budget_limited``; the run endpoint refuses an
exhausted workspace with HTTP 429 ``llm_budget_exhausted``; on the durable worker
path every failed attempt's spend is still recorded and the next attempt's budget
check sees it; and only an opaque tenant reference - never a raw id - reaches the
provider.

Every module-level path to a provider is offline (``app.tests._llm_offline``), and
every ``Settings`` here is built by ``offline_settings``, so the module behaves the
same on a machine whose environment configures a real provider.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.enums import ExplanationSource
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.jobs import handlers as job_handlers
from app.jobs import pipeline
from app.jobs.models import Job
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobStatus
from app.jobs.store import DurableJobStore, JobLeaseLostError
from app.jobs.worker import LEASE_LOST, JobRunner
from app.llm.base import LLMMalformedError
from app.llm.metering import (
    LLMBudgetExceededError,
    LLMMeter,
    cost_usd,
    month_to_date_usd,
    tenant_reference,
)
from app.llm.models import LLMUsageEvent
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.main import app
from app.opportunities.models import Opportunity
from app.scouting_requests import routes as scout_routes
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm, offline_settings

API = get_settings().api_prefix
KEY = "fixture-key"
MODEL = "claude-sonnet-4-6"
CLASSIFIED = {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}
EXPLAINED = {
    "why_it_matters": "Commuters report long waits.",
    "who_cares": "Commuters",
    "ai_inference": "Pre-ordering would help.",
    "recommended_action": "Promote pre-order.",
}


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


def _settings(**overrides) -> Settings:
    return offline_settings(llm_provider="anthropic", llm_api_key=KEY, llm_model=MODEL, **overrides)


def _reply(output: dict, *, stop_reason: str = "end_turn", tokens=(1000, 500)) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": json.dumps(output)}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
        },
    )


class Recorder:
    """A replayed provider answering by task; records every request body."""

    def __init__(self, classify=None, explain=None):
        self.bodies: list[dict] = []
        self.classify = classify or (lambda: _reply(CLASSIFIED))
        self.explain = explain or (lambda: _reply(EXPLAINED))

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        content = body["messages"][0]["content"]
        return self.classify() if content.startswith("Classify") else self.explain()

    def service(self, settings: Settings) -> LLMService:
        provider = AnthropicProvider(
            KEY, MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(self.handler))
        )
        return LLMService(provider, sleep=lambda _s: None, settings=settings)


# --------------------------------------------------------------------------- #
# Prices, cost and the tenant reference
# --------------------------------------------------------------------------- #
class TestPricing:
    def test_cost_from_the_price_table(self):
        s = _settings()
        assert cost_usd(s, "anthropic", "claude-sonnet-4-6", 1000, 500) == pytest.approx(0.0105)
        assert cost_usd(s, "anthropic", "claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.0)

    def test_unknown_model_is_charged_the_highest_rates(self, caplog):
        s = _settings()
        highest_in = max(p[0] for p in s.llm_prices_usd_per_mtok.values())
        highest_out = max(p[1] for p in s.llm_prices_usd_per_mtok.values())
        cost = cost_usd(s, "openai", "gpt-unknown", 1_000_000, 1_000_000)
        assert cost == pytest.approx(highest_in + highest_out)
        assert any(r.getMessage() == "llm.price_unknown" for r in caplog.records)

    def test_mock_is_free(self):
        assert cost_usd(_settings(), "mock", "mock-1", 10**6, 10**6) == 0.0

    def test_price_source_is_recorded(self):
        assert "2026-06-24" in offline_settings().llm_price_source

    @pytest.mark.parametrize(
        "table", [{}, {"m": [1.0]}, {"m": [-1.0, 2.0]}, {"m": [1.0, float("nan")]}]
    )
    def test_invalid_price_tables_are_rejected(self, table):
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="llm_prices_usd_per_mtok"):
            offline_settings(llm_prices_usd_per_mtok=table)

    def test_reply_cost_reaches_the_response_and_the_log(self, caplog):
        caplog.set_level("INFO", logger="signalnest.llm")
        _, response = Recorder().service(_settings()).run("classify_signal", {"content": "x"})
        assert response.usage.estimated_cost_usd == pytest.approx(0.0105)
        [call] = [r for r in caplog.records if r.getMessage() == "llm.call"]
        assert call.extra_fields["estimated_cost_usd"] == pytest.approx(0.0105)


class TestTenantReference:
    def test_opaque_stable_and_per_workspace(self):
        s = _settings(secret_key="k" * 40)
        ref = tenant_reference(s, "ws-dallas-0001")
        assert ref == tenant_reference(s, "ws-dallas-0001")
        assert ref != tenant_reference(s, "ws-london-0002")
        assert ref.startswith("t_") and len(ref) == 34 and "dallas" not in ref
        assert ref != tenant_reference(_settings(secret_key="j" * 40), "ws-dallas-0001")


# --------------------------------------------------------------------------- #
# The meter inside the service
# --------------------------------------------------------------------------- #
@pytest.fixture
def ledger_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
    engine.dispose()


def _meter(db, settings: Settings) -> LLMMeter:
    return LLMMeter(settings).bind(
        db, organization_id="org-1", workspace_id="ws-1", scout_request_id=None
    )


class TestMeteredAttempts:
    def test_each_attempt_is_charged_by_what_the_provider_did(self, ledger_db):
        s = _settings(llm_max_retries=2)
        steps = iter([
            httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}}),
            httpx.ReadTimeout("slow"),
            _reply(CLASSIFIED, stop_reason="max_tokens", tokens=(800, 1024)),
        ])

        def handler(request):
            step = next(steps)
            if isinstance(step, Exception):
                raise step
            return step

        provider = AnthropicProvider(
            KEY, MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        meter = _meter(ledger_db, s)
        with pytest.raises(LLMMalformedError):
            LLMService(provider, sleep=lambda _s: None, settings=s).run(
                "classify_signal", {"content": "x"}, meter=meter
            )
        meter.flush(ledger_db)
        ledger_db.commit()
        rows = ledger_db.scalars(select(LLMUsageEvent).order_by(LLMUsageEvent.created_at)).all()
        by_outcome = {r.outcome: r for r in rows}
        assert set(by_outcome) == {"LLMUnavailableError", "LLMTimeoutError", "LLMMalformedError"}
        assert by_outcome["LLMUnavailableError"].cost_usd == 0.0
        assert by_outcome["LLMTimeoutError"].cost_usd > 0  # worst case: it may have run
        truncated = by_outcome["LLMMalformedError"]
        assert (truncated.input_tokens, truncated.output_tokens) == (800, 1024)
        assert truncated.cost_usd == pytest.approx(cost_usd(s, "anthropic", MODEL, 800, 1024))
        assert meter.attempts == 3 and meter.spent == pytest.approx(sum(r.cost_usd for r in rows))

    def test_schema_invalid_reply_is_still_charged(self, ledger_db):
        s = _settings()
        meter = _meter(ledger_db, s)
        recorder = Recorder(classify=lambda: _reply({"sentiment": "negative"}))
        with pytest.raises(LLMMalformedError):
            recorder.service(s).run("classify_signal", {"content": "x"}, meter=meter)
        assert meter.spent == pytest.approx(0.0105)

    def test_run_cap_stops_attempts_before_they_are_made(self, ledger_db):
        s = _settings(llm_max_calls_per_run=1, llm_max_retries=2)
        recorder = Recorder(
            classify=lambda: httpx.Response(500, json={"type": "error", "error": {}})
        )
        meter = _meter(ledger_db, s)
        with pytest.raises(LLMBudgetExceededError) as exc:
            recorder.service(s).run("classify_signal", {"content": "x"}, meter=meter)
        assert exc.value.reason == "run_call_cap"
        assert len(recorder.bodies) == 1 and meter.refusals == {"run_call_cap": 1}

    def test_budget_stops_an_attempt_whose_worst_case_would_cross_it(self, ledger_db):
        s = _settings(llm_workspace_monthly_budget_usd=0.001)
        recorder = Recorder()
        with pytest.raises(LLMBudgetExceededError) as exc:
            recorder.service(s).run("classify_signal", {"content": "x"}, meter=_meter(ledger_db, s))
        assert exc.value.reason == "workspace_budget" and recorder.bodies == []

    def test_budget_refusal_never_falls_back_to_the_mock(self, ledger_db):
        s = _settings(llm_workspace_monthly_budget_usd=0.0, llm_allow_dev_fallback=True)
        with pytest.raises(LLMBudgetExceededError):
            Recorder().service(s).run(
                "classify_signal", {"content": "x"}, meter=_meter(ledger_db, s)
            )

    def test_mock_provider_is_never_limited(self, ledger_db):
        from app.llm.mock import MockLLMProvider

        s = _settings(llm_max_calls_per_run=1, llm_workspace_monthly_budget_usd=0.0)
        meter = _meter(ledger_db, s)
        service = LLMService(MockLLMProvider(), settings=s)
        for _ in range(3):
            service.run("classify_signal", {"content": "slow"}, meter=meter)
        assert meter.attempts == 3 and meter.spent == 0.0 and meter.refusals == {}

    def test_month_to_date_counts_only_this_workspace_and_month(self, ledger_db):
        now = datetime.now(UTC)
        last_month = now.replace(day=1) - (now.replace(day=2) - now.replace(day=1))
        for ws, when, cost in [("ws-1", now, 1.5), ("ws-1", last_month, 9.0), ("ws-2", now, 7.0)]:
            ledger_db.add(LLMUsageEvent(
                organization_id="org-1", workspace_id=ws, task="t", provider="anthropic",
                model=MODEL, cost_usd=cost, outcome="ok", created_at=when,
            ))
        ledger_db.commit()
        assert month_to_date_usd(ledger_db, "ws-1", now) == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# Pipeline and API over a throwaway four-market seed
# --------------------------------------------------------------------------- #
@pytest.fixture
def seeded(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'llm4.db'}", connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(seed_mod, "SessionLocal", factory)
    seed_mod.seed(reset=True)
    yield factory
    engine.dispose()


def _first_request(factory) -> ScoutRequest:
    with factory() as db:
        return db.scalars(select(ScoutRequest).order_by(ScoutRequest.id)).first()


def _run_with(factory, monkeypatch, settings: Settings, recorder: Recorder):
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "llm_service", recorder.service(settings))
    request = _first_request(factory)
    with factory() as db:
        before_opps = set(db.scalars(select(Opportunity.id)))
        before_signals = set(db.scalars(select(NormalizedSignal.id)))
        before_usage = set(db.scalars(select(LLMUsageEvent.id)))
        pipeline._run(db, request.id)
        db.commit()
        opps = [o for o in db.scalars(select(Opportunity)) if o.id not in before_opps]
        signals = [s for s in db.scalars(select(NormalizedSignal)) if s.id not in before_signals]
        usage = [u for u in db.scalars(select(LLMUsageEvent)) if u.id not in before_usage]
    return request, opps, signals, usage


def test_run_records_every_attempt_without_content(seeded, monkeypatch):
    recorder = Recorder()
    request, opps, _, usage = _run_with(seeded, monkeypatch, _settings(), recorder)
    assert len(usage) == len(recorder.bodies) > 0
    assert {u.workspace_id for u in usage} == {request.workspace_id}
    assert {u.scout_request_id for u in usage} == {request.id}
    assert all(u.outcome == "ok" and u.cost_usd == pytest.approx(0.0105) for u in usage)
    columns = set(LLMUsageEvent.__table__.columns.keys())
    assert not columns & {"prompt", "content", "output", "text", "response"}


def test_only_an_opaque_tenant_reference_reaches_the_provider(seeded, monkeypatch):
    recorder = Recorder()
    settings = _settings()
    request, _, _, _ = _run_with(seeded, monkeypatch, settings, recorder)
    expected = tenant_reference(settings, request.workspace_id)
    assert recorder.bodies
    for body in recorder.bodies:
        assert body["metadata"] == {"user_id": expected}
        sent = json.dumps(body)
        for raw in (request.workspace_id, request.organization_id, request.id, request.brand_id):
            assert raw not in sent


def test_run_cap_marks_the_rest_budget_limited(seeded, monkeypatch):
    recorder = Recorder()
    _, opps, signals, usage = _run_with(
        seeded, monkeypatch, _settings(llm_max_calls_per_run=2), recorder
    )
    assert len(recorder.bodies) == 2 and len(usage) == 2
    analysed = [s for s in signals if not s.is_noise and not s.is_duplicate]
    sources = [s.ingest_metadata["classification_source"] for s in analysed]
    assert sources.count("model") == 2
    assert sources.count("budget_limited") == len(analysed) - 2
    assert all(
        o.explanation_source in {"budget_limited", "rule_based"} for o in opps
    )
    member_sources: dict[str, set] = {}
    for s in signals:
        if s.cluster_id:
            member_sources.setdefault(s.cluster_id, set()).add(
                s.ingest_metadata["classification_source"]
            )
    limited = [o for o in opps if o.explanation_source == "budget_limited"]
    assert limited
    for o in limited:
        # A cluster the cap left unclassified says so; a fully classified one whose
        # explain call the cap refused carries the plain budget copy.
        expected = (
            pipeline.BUDGET_UNCLASSIFIED_WHY
            if "budget_limited" in member_sources[o.cluster_id]
            else pipeline.BUDGET_LIMITED_WHY
        )
        assert o.why_it_matters == expected
        assert o.ai_inference is None


def test_exhausted_workspace_budget_makes_no_calls(seeded, monkeypatch):
    request = _first_request(seeded)
    with seeded() as db:
        db.add(LLMUsageEvent(
            organization_id=request.organization_id, workspace_id=request.workspace_id,
            task="classify_signal", provider="anthropic", model=MODEL, cost_usd=5.0,
            outcome="ok",
        ))
        db.commit()
    recorder = Recorder()
    _, opps, signals, usage = _run_with(seeded, monkeypatch, _settings(), recorder)
    assert recorder.bodies == [] and usage == []
    analysed = [s for s in signals if not s.is_noise and not s.is_duplicate]
    assert analysed and all(
        s.ingest_metadata["classification_source"] == ExplanationSource.BUDGET_LIMITED.value
        for s in analysed
    )
    assert all(o.explanation_source in {"budget_limited", "rule_based"} for o in opps)


def test_a_member_left_unclassified_by_the_cap_is_never_rule_based(seeded, monkeypatch):
    """The cap stops classification part-way: a cluster with an unclassified member
    is ``budget_limited`` with copy saying the scores lack it, even below the action
    floor, where an all-classified cluster would be ``rule_based`` (#237's rule)."""
    real = pipeline.score_relevance
    monkeypatch.setattr(pipeline, "score_relevance", lambda sig, ctx: (0, real(sig, ctx)[1]))
    recorder = Recorder()
    _, opps, signals, _ = _run_with(
        seeded, monkeypatch, _settings(llm_max_calls_per_run=2), recorder
    )
    assert len(recorder.bodies) == 2
    by_cluster: dict[str, set] = {}
    for s in signals:
        if s.cluster_id:
            by_cluster.setdefault(s.cluster_id, set()).add(
                s.ingest_metadata["classification_source"]
            )
    limited = [o for o in opps if "budget_limited" in by_cluster[o.cluster_id]]
    complete = [o for o in opps if by_cluster[o.cluster_id] == {"model"}]
    assert limited, "the cap must leave at least one cluster with an unclassified member"
    for o in limited:
        assert o.explanation_source == "budget_limited"
        assert o.why_it_matters == pipeline.BUDGET_UNCLASSIFIED_WHY
        assert "unaffected" not in o.why_it_matters.lower()
        assert o.ai_inference is None
    for o in complete:
        assert o.explanation_source == "rule_based"


# --------------------------------------------------------------------------- #
# Failed attempts on the durable worker path (scope r1 MAJOR-1)
# --------------------------------------------------------------------------- #
class _LeaseLostAtCompletion(DurableJobStore):
    """The lease is reclaimed while the attempt runs: completion matches no row."""

    def complete(self, db, job, **_kwargs):
        raise JobLeaseLostError(job.id)


def _failing_worker_run(monkeypatch, settings: Settings, *, fail: bool = True) -> Recorder:
    """The real handler under the real worker, with a replayed real adapter. With
    ``fail`` the pipeline raises after its classification calls (a non-LLM error)."""
    recorder = Recorder()
    monkeypatch.setattr(job_handlers, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "llm_service", recorder.service(settings))
    if fail:
        def boom(*_a, **_k):
            raise RuntimeError("opportunity build failed")

        monkeypatch.setattr(pipeline, "_build_opportunities", boom)
    return recorder


def _enqueue(factory, request: ScoutRequest) -> str:
    with factory() as db:
        job = enqueue_scout_request(
            db,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            scout_request_id=request.id,
            location_id=request.location_id,
            campaign_id=request.campaign_id,
            request_id=request.id,
        )
        db.commit()
        return job.id


def _attempt(factory, attempt: int, store: DurableJobStore | None = None) -> str | bool:
    """One worker poll, a day after the previous one so any retry backoff has passed."""
    when = datetime.now(UTC) + timedelta(days=attempt)
    runner = JobRunner(
        settings=offline_settings(),
        session_factory=factory,
        store=store or DurableJobStore(),
        clock=lambda: when,
    )
    if store is None:
        return runner.poll_once(worker_id="w-llm4")
    with factory() as db:
        job = store.claim_one(db, worker_id="w-llm4", lease_seconds=60, now=when)
        db.commit()
        return runner.run_claimed(db, job)


def _anthropic_rows(factory, request_id: str) -> list[LLMUsageEvent]:
    with factory() as db:
        return list(db.scalars(select(LLMUsageEvent).where(
            LLMUsageEvent.scout_request_id == request_id,
            LLMUsageEvent.provider == "anthropic",
        )))


def test_every_failed_worker_attempt_records_its_spend(seeded, monkeypatch):
    recorder = _failing_worker_run(monkeypatch, _settings())
    request = _first_request(seeded)
    with seeded() as db:
        opportunities_before = db.scalar(select(func.count()).select_from(Opportunity))
    job_id = _enqueue(seeded, request)
    calls_per_attempt = None
    for attempt in (1, 2):
        assert _attempt(seeded, attempt) is True
        with seeded() as db:
            job = db.get(Job, job_id)
            assert (job.status, job.attempt_count) == (JobStatus.RETRY_WAIT.value, attempt)
            # The attempt's own work was rolled back ...
            assert db.scalar(select(func.count()).select_from(Opportunity)) == (
                opportunities_before
            )
        calls_per_attempt = calls_per_attempt or len(recorder.bodies)
        assert calls_per_attempt > 0
        # ... but every provider call it paid for is recorded, once.
        rows = _anthropic_rows(seeded, request.id)
        assert len(rows) == len(recorder.bodies) == attempt * calls_per_attempt
        assert {r.workspace_id for r in rows} == {request.workspace_id}
        assert sum(r.cost_usd for r in rows) == pytest.approx(0.0105 * len(rows))


def test_the_next_attempt_is_budgeted_with_the_failed_attempts_spend(seeded, monkeypatch):
    budget = 0.04
    recorder = _failing_worker_run(
        monkeypatch, _settings(llm_workspace_monthly_budget_usd=budget)
    )
    request = _first_request(seeded)
    _enqueue(seeded, request)
    assert _attempt(seeded, 1) is True
    first = len(recorder.bodies)
    assert first > 0
    assert _attempt(seeded, 2) is True
    # Unrecorded, the second attempt would start from zero and pay for ``first`` calls
    # again; recorded, the budget it sees is already spent.
    assert len(recorder.bodies) == first
    rows = _anthropic_rows(seeded, request.id)
    assert len(rows) == first
    assert sum(r.cost_usd for r in rows) <= budget


def test_spend_survives_a_lease_lost_at_completion(seeded, monkeypatch):
    recorder = _failing_worker_run(monkeypatch, _settings(), fail=False)
    request = _first_request(seeded)
    with seeded() as db:
        opportunities_before = db.scalar(select(func.count()).select_from(Opportunity))
    _enqueue(seeded, request)
    assert _attempt(seeded, 1, store=_LeaseLostAtCompletion()) == LEASE_LOST
    assert recorder.bodies
    rows = _anthropic_rows(seeded, request.id)
    assert len(rows) == len(recorder.bodies)
    with seeded() as db:
        assert db.scalar(select(func.count()).select_from(Opportunity)) == opportunities_before


def test_a_successful_worker_attempt_records_its_spend_once(seeded, monkeypatch):
    recorder = _failing_worker_run(monkeypatch, _settings(), fail=False)
    request = _first_request(seeded)
    job_id = _enqueue(seeded, request)
    assert _attempt(seeded, 1) is True
    with seeded() as db:
        assert db.get(Job, job_id).status == JobStatus.SUCCEEDED.value
    assert recorder.bodies
    assert len(_anthropic_rows(seeded, request.id)) == len(recorder.bodies)


def test_a_failing_after_rollback_write_never_masks_the_attempt(seeded, monkeypatch):
    _failing_worker_run(monkeypatch, _settings())

    def broken_flush(self, db):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(pipeline.LLMMeter, "flush", broken_flush)
    request = _first_request(seeded)
    job_id = _enqueue(seeded, request)
    assert _attempt(seeded, 1) is True
    with seeded() as db:
        job = db.get(Job, job_id)
        assert (job.status, job.attempt_count) == (JobStatus.RETRY_WAIT.value, 1)


# --------------------------------------------------------------------------- #
# API refusal state
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(seeded):
    def _override_get_db():
        s = seeded()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _spend(factory, request: ScoutRequest, usd: float) -> None:
    with factory() as db:
        db.add(LLMUsageEvent(
            organization_id=request.organization_id, workspace_id=request.workspace_id,
            task="explain_opportunity", provider="anthropic", model=MODEL, cost_usd=usd,
            outcome="ok",
        ))
        db.commit()


def _post_run(client, request: ScoutRequest):
    token = live_bearer(seed_mod.sid("user"))
    return client.post(
        f"{API}/workspaces/{request.workspace_id}/scout-requests/{request.id}/run",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_run_endpoint_refuses_an_exhausted_workspace(seeded, client, monkeypatch):
    monkeypatch.setattr(scout_routes, "get_settings", lambda: _settings())
    request = _first_request(seeded)
    _spend(seeded, request, 5.0)
    response = _post_run(client, request)
    assert response.status_code == 429, response.text
    body = response.json()
    assert "llm_budget_exhausted" in json.dumps(body)
    assert "USD 5.00 of 5.00" in json.dumps(body)
    with seeded() as db:
        assert db.get(ScoutRequest, request.id).status == request.status  # nothing claimed
        assert db.scalar(select(func.count()).select_from(LLMUsageEvent)) >= 1


def test_run_endpoint_accepts_a_workspace_under_budget(seeded, client, monkeypatch):
    monkeypatch.setattr(scout_routes, "get_settings", lambda: _settings())
    request = _first_request(seeded)
    _spend(seeded, request, 4.99)
    response = _post_run(client, request)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"


def test_mock_provider_is_never_refused(seeded, client, monkeypatch):
    monkeypatch.setattr(scout_routes, "get_settings", lambda: offline_settings())
    request = _first_request(seeded)
    _spend(seeded, request, 50.0)
    assert scout_routes.get_settings().llm_provider == "mock"
    assert _post_run(client, request).status_code == 200


def test_a_foreign_caller_is_refused_before_any_budget_lookup(seeded, client, monkeypatch):
    """E9 on the budget branch (scope r1 INFO-7): under a real provider, the owner of
    another organization naming an exhausted workspace gets the existing membership
    refusal (403 ``permission_denied``), never the 429 that would reveal its spend."""
    monkeypatch.setattr(scout_routes, "get_settings", lambda: _settings())
    target = _first_request(seeded)
    _spend(seeded, target, 50.0)
    registered = client.post(
        f"{API}/auth/register",
        json={
            "email": "llm4-outsider@example.com",
            "full_name": "LLM4 outsider",
            "password": "Outsider-passphrase-2026!",
            "organization_name": "LLM4 outsider organization",
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["memberships"][0]["organization_id"] != target.organization_id
    path = f"{API}/workspaces/{target.workspace_id}/scout-requests/{target.id}/run"
    foreign = client.post(
        path, headers={"Authorization": f"Bearer {registered.json()['access_token']}"}
    )
    assert foreign.status_code == 403, foreign.text
    assert foreign.json()["error"]["code"] == "permission_denied"
    assert "budget" not in foreign.text.lower() and "USD" not in foreign.text
    # Positive control: the exhausted workspace's own editor does get the refusal.
    assert _post_run(client, target).status_code == 429
    with seeded() as db:
        assert db.get(ScoutRequest, target.id).status == target.status
