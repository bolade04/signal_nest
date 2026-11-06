"""P6-LLM-4 hardening: an :class:`~app.llm.metering.LLMMeter` belongs to one run, for good.

A meter is bound once, to the organization, workspace and scout request the run
loaded. Binding it again - to another tenant or to the same one - is refused with
:class:`~app.llm.metering.LLMMeterBindingError`, before anything changes, and the
owner fields cannot be assigned. A meter that already holds records cannot be bound
at all. So a failed rebind cannot move recorded usage to another workspace, reset the
budget counters or the attempt count, or change what any later row or request is
attributed to. The production path builds a fresh meter per attempt
(``app.jobs.handlers.execute_scout_request``) and is unaffected.

Fake providers only: the real Anthropic adapter over ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.models import Base
from app.llm.metering import (
    LLMBudgetExceededError,
    LLMMeter,
    month_to_date_usd,
    tenant_reference,
)
from app.llm.models import LLMUsageEvent
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.organizations.models import Organization, Workspace
from app.tests._llm_offline import offline_llm, offline_settings, tenant_of, wire_payload

KEY = "fixture-key"
MODEL = "claude-sonnet-4-6"
CLASSIFIED = {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}
A = {"organization_id": "org-A", "workspace_id": "ws-A", "scout_request_id": "req-A"}
B = {"organization_id": "org-B", "workspace_id": "ws-B", "scout_request_id": "req-B"}
JOB = "job-A"
OWNER_FIELDS = ("organization_id", "workspace_id", "scout_request_id", "tenant_ref", "job_id")


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


def _settings(**overrides) -> Settings:
    return offline_settings(llm_provider="anthropic", llm_api_key=KEY, llm_model=MODEL, **overrides)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session
    engine.dispose()


def _row(owner: dict, *, job_id: str | None, cost: float) -> LLMUsageEvent:
    return LLMUsageEvent(
        **owner,
        job_id=job_id,
        task="classify_signal",
        provider="anthropic",
        model=MODEL,
        cost_usd=cost,
        outcome="ok",
        created_at=datetime.now(UTC),
    )


class Provider:
    """The real adapter over a replay; records the tenant reference of each request."""

    def __init__(self):
        self.refs: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.refs.append(json.loads(request.content).get("metadata", {}).get("user_id"))
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(CLASSIFIED)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "output_tokens": 500},
            },
        )

    def service(self, settings: Settings) -> LLMService:
        provider = AnthropicProvider(
            KEY, MODEL, 5, client=httpx.Client(transport=httpx.MockTransport(self.handler))
        )
        return LLMService(provider, sleep=lambda _s: None, settings=settings)


def _state(meter: LLMMeter) -> dict:
    return {
        **{f: getattr(meter, f) for f in OWNER_FIELDS},
        "month_to_date": meter.month_to_date,
        "prior_attempts": meter.prior_attempts,
        "attempts": meter.attempts,
        "spent": meter.spent,
        "refusals": dict(meter.refusals),
        "summary": meter.summary(),
        "records": [dict(r) for r in meter._records],
    }


def _seed(db) -> None:
    # Job A already has two recorded attempts; workspace B has its own spend. The
    # tenants exist: the ledger never writes for a workspace that is not there.
    db.add_all(
        [
            Organization(id="org-A", name="A", slug="org-a"),
            Organization(id="org-B", name="B", slug="org-b"),
            Workspace(id="ws-A", organization_id="org-A", name="A", slug="ws-a"),
            Workspace(id="ws-B", organization_id="org-B", name="B", slug="ws-b"),
            _row(A, job_id=JOB, cost=0.25),
            _row(A, job_id=JOB, cost=0.25),
            _row(B, job_id="job-B", cost=3.0),
        ]
    )
    db.commit()


@pytest.mark.parametrize("target", [B, A], ids=["other-tenant", "same-tenant"])
def test_a_failed_rebind_changes_nothing_before_or_after(db, target):
    s = _settings(llm_max_calls_per_run=4)
    _seed(db)
    provider = Provider()
    service = provider.service(s)
    meter = LLMMeter(s, job_id=JOB).bind(db, **A)
    assert meter.tenant_ref == tenant_reference(s, "ws-A")
    assert (meter.organization_id, meter.workspace_id, meter.scout_request_id) == (
        "org-A",
        "ws-A",
        "req-A",
    )
    assert meter.prior_attempts == 2 and meter.attempts == 2
    assert meter.month_to_date == pytest.approx(0.5)

    service.run(
        "classify_signal", {"content": "x", "audiences": None}, meter=meter, **tenant_of(meter)
    )
    before = _state(meter)

    with pytest.raises(RuntimeError, match="already bound") as exc:
        meter.bind(db, **target)
    assert type(exc.value).__name__ == "LLMMeterBindingError"
    # Nothing moved or reset: owner, tenant reference, counters, records.
    assert _state(meter) == before

    # The run goes on as A's, and the call cap still counts every attempt (2 prior +
    # 1 + 1 = 4): a rebind did not reset ``attempts`` back to ``prior_attempts``.
    service.run(
        "classify_signal", {"content": "x", "audiences": None}, meter=meter, **tenant_of(meter)
    )
    with pytest.raises(LLMBudgetExceededError) as cap:
        service.run(
            "classify_signal", {"content": "x", "audiences": None}, meter=meter, **tenant_of(meter)
        )
    assert cap.value.reason == "run_call_cap"
    assert meter.attempts == 4 and meter.refusals == {"run_call_cap": 1}

    # Every request, before and after the failed rebind, carried A's reference.
    ref_a = tenant_reference(s, "ws-A")
    assert provider.refs == [ref_a, ref_a]

    meter.flush(db)
    db.commit()
    written = db.scalars(select(LLMUsageEvent).where(LLMUsageEvent.job_id == JOB)).all()
    assert len(written) == 4  # 2 seeded + the 2 this meter made
    assert {(r.organization_id, r.workspace_id, r.scout_request_id) for r in written} == {
        ("org-A", "ws-A", "req-A")
    }
    # Workspace B's ledger is exactly what it was.
    b_rows = db.scalars(select(LLMUsageEvent).where(LLMUsageEvent.workspace_id == "ws-B")).all()
    assert [(r.job_id, r.cost_usd) for r in b_rows] == [("job-B", 3.0)]
    assert month_to_date_usd(db, "ws-B") == pytest.approx(3.0)
    assert month_to_date_usd(db, "ws-A") == pytest.approx(0.5 + 2 * 0.0105)


def test_a_meter_holding_records_cannot_be_bound(db):
    """Usage recorded before any bind is never attributed by a later bind."""
    s = _settings()
    meter = LLMMeter(s, job_id=JOB)
    meter.before_attempt("anthropic", MODEL, wire_payload(max_tokens=100))
    meter.after_attempt(
        task="classify_signal", provider="anthropic", model=MODEL
    )
    before = _state(meter)

    with pytest.raises(RuntimeError, match="already holds"):
        meter.bind(db, **B)
    assert _state(meter) == before and meter.workspace_id is None

    meter.flush(db)
    db.commit()
    assert db.scalars(select(LLMUsageEvent)).all() == []


def _attempt_only() -> LLMMeter:
    meter = LLMMeter(_settings(), job_id=JOB)
    # counted, nothing recorded yet
    meter.before_attempt("anthropic", MODEL, wire_payload(max_tokens=100))
    assert meter.attempts == 1
    return meter


def _spend_only() -> LLMMeter:
    meter = LLMMeter(_settings(), job_id=JOB)
    meter.spent = 0.5
    return meter


def _refusal_only() -> LLMMeter:
    meter = LLMMeter(_settings(llm_workspace_monthly_budget_usd=0.0), job_id=JOB)
    with pytest.raises(LLMBudgetExceededError):
        meter.before_attempt("anthropic", MODEL, wire_payload(max_tokens=100))  # budget 0: refused
    assert meter.attempts == 0 and meter.refusals == {"workspace_budget": 1}
    return meter


@pytest.mark.parametrize(
    "prime", [_attempt_only, _spend_only, _refusal_only], ids=["attempts", "spent", "refusals"]
)
def test_a_meter_with_attempts_spend_or_refusals_but_no_records_cannot_be_bound(db, prime):
    """Not only records: any accounting state from before a bind refuses the bind."""
    _seed(db)
    meter = prime()
    assert meter._records == []
    before = _state(meter)

    with pytest.raises(RuntimeError, match="already holds") as exc:
        meter.bind(db, **A)
    assert type(exc.value).__name__ == "LLMMeterBindingError"
    assert _state(meter) == before
    assert meter.workspace_id is None and meter.tenant_ref is None

    meter.flush(db)
    db.commit()
    assert len(db.scalars(select(LLMUsageEvent)).all()) == 3  # only the seeded rows


@pytest.mark.parametrize("field", OWNER_FIELDS)
def test_owner_fields_cannot_be_assigned(db, field):
    meter = LLMMeter(_settings(), job_id=JOB).bind(db, **A)
    before = getattr(meter, field)
    with pytest.raises(AttributeError):
        setattr(meter, field, "ws-B")
    assert getattr(meter, field) == before


def test_a_fresh_meter_per_attempt_still_binds_and_counts_prior_attempts(db):
    """The production shape: each worker attempt builds and binds its own meter."""
    s = _settings()
    _seed(db)
    first = LLMMeter(s, job_id=JOB).bind(db, **A)
    first.before_attempt("anthropic", MODEL, wire_payload(max_tokens=100))
    first.after_attempt(
        task="classify_signal", provider="anthropic", model=MODEL
    )
    first.flush(db)
    db.commit()

    second = LLMMeter(s, job_id=JOB).bind(db, **A)
    assert second.prior_attempts == 3 and second.attempts == 3
    assert second.tenant_ref == first.tenant_ref == tenant_reference(s, "ws-A")

    # Each bind derives the reference from the workspace it is given, never from an
    # earlier meter: a meter for workspace B gets B's reference and B's own counters.
    other = LLMMeter(s, job_id="job-B").bind(db, **B)
    assert other.tenant_ref == tenant_reference(s, "ws-B") != first.tenant_ref
    assert (other.organization_id, other.workspace_id, other.scout_request_id) == (
        "org-B",
        "ws-B",
        "req-B",
    )
    assert other.prior_attempts == 1 and other.month_to_date == pytest.approx(3.0)
