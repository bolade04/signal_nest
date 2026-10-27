"""Child-process bodies for the PostgreSQL LLM budget proofs. Not a test module.

``multiprocessing`` with the ``spawn`` start method imports this module afresh in every
child, so each child builds its own engine from the URL it is handed (no connection,
session or lock is inherited). Everything here is offline: the provider is a fixed-cost
fake, never a network client, and the settings are built explicitly from code defaults.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

# A spawned child holds only the models it imports; the unit of work needs every table
# the ledger's foreign keys point at (the same trap ``app.jobs.worker`` documents).
import app.db.models  # noqa: F401 - completes Base.metadata for this process
from app.core.config import Settings
from app.llm import budget as ledger
from app.llm.base import LLMRequest, LLMResponse, LLMUsage
from app.llm.metering import LLMBudgetExceededError, LLMMeter
from app.llm.service import LLMService

MODEL = "claude-sonnet-4-6"
KEY = "fixture-key"
CLASSIFIED = {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}
#: 1000 input + 500 output tokens at the sonnet-4-6 table price: USD 0.0105 per call.
FIXED_USAGE = (1000, 500)
FIXED_COST = Decimal("0.010500")
DB_TIMEOUT_MS = 10_000
#: The inputs the P1/P3 run bodies send through the service.
RUN_INPUTS = {"content": "x" * 100}


def wire_payload(content: str = "x", tenant_ref: str | None = None) -> bytes:
    """The body bytes the adapter would send for ``content`` (what the meter bounds)."""
    from app.tests._llm_offline import wire_payload as wire

    return wire(MODEL, content=content, max_tokens=1024, tenant_ref=tenant_ref)


#: A tenant reference has a fixed length ("t_" + 32 hex), so any value bounds the same.
TENANT_REF = "t_" + "0" * 32
#: The content the crash body reserves for (it never renders a prompt through the service).
CRASH_CONTENT = "c" * 100


def worst_case(content: str = RUN_INPUTS["content"], *, tenant: bool = True) -> Decimal:
    """The worst case the meter reserves for one call with ``content`` (budget-free)."""
    settings = settings_for(1.0)
    payload = wire_payload(content, TENANT_REF if tenant else None)
    bound = LLMMeter(settings).worst_case("anthropic", MODEL, payload)
    return ledger.usd(bound)


def settings_for(budget: float, **overrides: Any) -> Settings:
    llm_defaults = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
        if name.startswith("llm_")
    }
    return Settings(
        _env_file=None,
        **{
            **llm_defaults,
            "llm_provider": "anthropic",
            "llm_api_key": KEY,
            "llm_model": MODEL,
            "llm_workspace_monthly_budget_usd": budget,
            **overrides,
        },
    )


class FixedCostProvider:
    """A fake Anthropic-priced provider: every call reports the same usage, offline."""

    name = "anthropic"
    model = MODEL

    def __init__(self) -> None:
        self.calls = 0

    def prepare(self, request: LLMRequest):
        """The exact bytes an Anthropic adapter would send (what the meter bounds)."""
        from app.llm.providers_real import AnthropicProvider

        return AnthropicProvider(KEY, MODEL, 5).prepare(request)

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            output=dict(CLASSIFIED),
            usage=LLMUsage(input_tokens=FIXED_USAGE[0], output_tokens=FIXED_USAGE[1]),
            prompt_name=prompt_name,
            prompt_version=prompt_version,
        )


def make_engine(url: str):
    engine = create_engine(url, future=True, pool_size=4, max_overflow=4)

    @event.listens_for(engine, "connect")
    def _timeouts(dbapi_conn, _record):  # pragma: no cover - trivial hook
        cur = dbapi_conn.cursor()
        cur.execute(f"SET statement_timeout = {DB_TIMEOUT_MS}")
        cur.execute(f"SET lock_timeout = {DB_TIMEOUT_MS}")
        cur.close()

    return engine


def make_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def run_until_refused(
    url: str,
    barrier,
    *,
    budget: float,
    organization_id: str,
    workspace_id: str,
    job_id: str | None,
    attempt: int,
    results,
) -> None:
    """P1 body: a run of its own process, making metered calls until one is refused.

    Binds first, waits at the barrier with every other process, then calls until the
    budget refuses. Reports (pid, calls made, worst case reserved per call, spent, reason).
    """
    engine = make_engine(url)
    try:
        factory = make_factory(engine)
        settings = settings_for(budget)
        provider = FixedCostProvider()
        service = LLMService(provider, sleep=lambda _s: None, settings=settings)
        meter = LLMMeter(settings, job_id=job_id, attempt=attempt, session_factory=factory)
        with factory() as run_session:
            meter.bind(
                run_session,
                organization_id=organization_id,
                workspace_id=workspace_id,
                scout_request_id=None,
            )
            assert meter.shared_ledger, "the proof needs the shared-ledger path"
        barrier.wait(timeout=60)
        reason = None
        for _ in range(1000):
            try:
                service.run("classify_signal", RUN_INPUTS, meter=meter)
            except LLMBudgetExceededError as exc:
                reason = exc.reason
                break
        results.put(
            {
                "pid": os.getpid(),
                "calls": provider.calls,
                "spent": meter.spent,
                "reason": reason,
                "attempts": meter.attempts,
            }
        )
    finally:
        engine.dispose()


def reserve_then_die(
    url: str,
    *,
    budget: float,
    organization_id: str,
    workspace_id: str,
    job_id: str | None,
    attempt: int,
    reserved_event,
    make_call: bool,
) -> None:
    """P2/P3 body: reserve one call (and optionally make it), then die before settling.

    ``os._exit(1)`` skips every ``finally`` and atexit handler, like a SIGKILL would:
    the reservation committed before the call is the only trace this process leaves.
    """
    engine = make_engine(url)
    factory = make_factory(engine)
    settings = settings_for(budget)
    provider = FixedCostProvider()
    meter = LLMMeter(settings, job_id=job_id, attempt=attempt, session_factory=factory)
    with factory() as run_session:
        meter.bind(
            run_session,
            organization_id=organization_id,
            workspace_id=workspace_id,
            scout_request_id=None,
        )
    meter.before_attempt("anthropic", MODEL, wire_payload(CRASH_CONTENT))
    if make_call:
        provider.generate(LLMRequest(task="classify_signal", inputs={"content": "x"}), "p", "v")
    reserved_event.set()
    time.sleep(0.2)
    os._exit(1)


def settle_once(url: str, *, attempt_key: str, settled_usd: str, barrier, results) -> None:
    """P4 body: settle one key from an independent process (duplicate settlement race)."""
    engine = make_engine(url)
    try:
        factory = make_factory(engine)
        barrier.wait(timeout=60)
        session: Session = factory()
        try:
            done = ledger.settle(
                session,
                attempt_key=attempt_key,
                settled_usd=Decimal(settled_usd),
                now=datetime.now(UTC),
                usage={
                    "scout_request_id": None,
                    "task": "classify_signal",
                    "provider": "anthropic",
                    "model": MODEL,
                    "input_tokens": FIXED_USAGE[0],
                    "output_tokens": FIXED_USAGE[1],
                    "cost_usd": float(settled_usd),
                    "outcome": "ok",
                },
            )
            session.commit()
        finally:
            session.close()
        results.put({"pid": os.getpid(), "settled": done})
    finally:
        engine.dispose()
