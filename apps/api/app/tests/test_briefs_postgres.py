"""Project Phase 5A on PostgreSQL: brief capture under real concurrency and the shared ledger.

* **Versioning under concurrency.** Captures for one opportunity lock its row
  (``SELECT ... FOR UPDATE``, READ COMMITTED) before reading the highest version, so N
  concurrent captures, each in its own session and transaction, all succeed with versions
  1..N and a linear ``parent_brief_id`` chain — no unique-key clash, no lost version.
* **Shared ledger.** With a priced provider on PostgreSQL the brief's meter reserves before
  the call and settles after it, each in a short transaction of its own; the brief links to
  that committed usage row, and the row survives a rollback of the request's transaction
  (spend is never lost with an error).

Each test has a database of its own (created and dropped here). Everything is offline: the
mock provider, or the real Anthropic adapter over ``httpx.MockTransport``. Gate:
``require_postgres`` (fails, never skips, under ``CI=true``).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.briefs.models import RecommendationBrief
from app.briefs.service import create_brief
from app.db.models import Base
from app.intelligence.records import SignalIntelligenceRecord
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.tests._auth2_support import pg_database, require_postgres
from app.tests._llm_offline import offline_llm, offline_settings

KEY, MODEL = "fixture-key", "claude-sonnet-4-6"
EXPLAINED = {
    "why_it_matters": "Commuters report long morning waits.",
    "who_cares": "commuters",
    "ai_inference": "Inference: speed decides.",
    "recommended_action": "Publish a brand-safe post about faster service.",
    "risk_note": "",
    "suggested_angles": ["Speed you can count on"],
}


@pytest.fixture(scope="module", autouse=True)
def _offline() -> Iterator[None]:
    with offline_llm():
        yield


@pytest.fixture
def factory() -> Iterator[sessionmaker]:  # pragma: no cover - gated on PostgreSQL
    require_postgres()
    with pg_database("sn_p5a") as url:
        engine = create_engine(url, future=True, pool_size=20, max_overflow=10)
        Base.metadata.create_all(engine)
        make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
        _seed(make)
        try:
            yield make
        finally:
            engine.dispose()


def _seed(make) -> None:  # pragma: no cover - gated on PostgreSQL
    from app.brands.models import Brand
    from app.organizations.models import Organization, Workspace
    from app.scouting_requests.models import ScoutRequest
    from app.signals.models import NormalizedSignal, RawSignal

    with make() as s:
        s.add(Organization(id="org-p", name="P", slug="org-p"))
        s.add(User(id="author-p", email="author@example.com", full_name="Author P",
                   hashed_password="x", is_active=True))
        s.flush()
        s.add(Workspace(id="ws-p", organization_id="org-p", name="P", slug="ws-p"))
        s.flush()
        s.add(Brand(id="br-p", organization_id="org-p", workspace_id="ws-p", name="B"))
        s.flush()
        s.add(ScoutRequest(id="sr-p", organization_id="org-p", workspace_id="ws-p",
                           brand_id="br-p", name="scout"))
        s.flush()
        s.add(RawSignal(id="raw-p", organization_id="org-p", workspace_id="ws-p",
                        scout_request_id="sr-p", source_type="rss_news", content="c"))
        s.flush()
        s.add(NormalizedSignal(id="ns-p", organization_id="org-p", workspace_id="ws-p",
                               scout_request_id="sr-p", raw_signal_id="raw-p",
                               source_type="rss_news", excerpt="e", content_hash="h",
                               pain_point_dna="speed_complaint"))
        s.flush()
        s.add(Opportunity(id="opp-p", organization_id="org-p", workspace_id="ws-p",
                          brand_id="br-p", scout_request_id="sr-p", title="T",
                          classification="emerging", decision="monitor",
                          resolved_market="Dallas", audience_fit="commuters"))
        s.flush()
        s.add(SignalIntelligenceRecord(
            id="sir-p", organization_id="org-p", workspace_id="ws-p", scout_request_id="sr-p",
            normalized_signal_id="ns-p", opportunity_id="opp-p", analysis_version="3c",
            scoring_version="3c.1", fingerprint="fp-p", enricher="deterministic",
            accepted=True, classification="emerging"))
        s.commit()


def _capture(make, llm: LLMService, settings, *, commit: bool = True) -> str:
    with make() as s:
        brief = create_brief(
            s,
            opportunity=s.get(Opportunity, "opp-p"),
            intelligence_record=s.get(SignalIntelligenceRecord, "sir-p"),
            campaign_id=None,
            author=s.get(User, "author-p"),
            llm=llm,
            settings=settings,
        )
        brief_id = brief.id
        if commit:
            s.commit()
        else:
            s.rollback()
        return brief_id


def _priced() -> tuple[LLMService, object, list[dict]]:
    """The real Anthropic adapter over a mock transport, priced: the shared-ledger path.

    On this path the request's own transaction holds no ledger lock (the meter reserves and
    settles in short transactions of its own), so the only thing serializing two captures is
    the opportunity row lock under proof.
    """
    settings = offline_settings(llm_provider="anthropic", llm_api_key=KEY, llm_model=MODEL)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": json.dumps(EXPLAINED)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        })

    provider = AnthropicProvider(KEY, MODEL, 5,
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    return LLMService(provider, sleep=lambda _s: None, settings=settings), settings, bodies


def test_concurrent_captures_serialize_into_a_linear_version_chain(factory):
    llm, settings, _bodies = _priced()
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            _capture(factory, llm, settings)
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    with factory() as s:
        rows = s.scalars(
            select(RecommendationBrief).order_by(RecommendationBrief.brief_version)
        ).all()
        assert [r.brief_version for r in rows] == list(range(1, n + 1))
        assert rows[0].parent_brief_id is None
        for previous, current in zip(rows, rows[1:], strict=False):
            assert current.parent_brief_id == previous.id
        # One usage row per capture, each brief linked to its own.
        assert len({r.llm_usage_event_id for r in rows}) == n
        assert s.scalar(select(func.count()).select_from(LLMUsageEvent)) == n


def test_the_mock_path_also_versions_and_links(factory):
    settings = offline_settings()
    llm = LLMService(MockLLMProvider(seed="p5a-pg"), settings=settings)
    first, second = _capture(factory, llm, settings), _capture(factory, llm, settings)
    with factory() as s:
        rows = {r.id: r for r in s.scalars(select(RecommendationBrief)).all()}
        assert rows[second].parent_brief_id == first and rows[second].brief_version == 2
        assert s.get(LLMUsageEvent, rows[second].llm_usage_event_id).provider == "mock"


def test_a_priced_capture_uses_the_shared_ledger_and_keeps_spend_on_rollback(factory):
    llm, settings, bodies = _priced()

    kept = _capture(factory, llm, settings)
    _capture(factory, llm, settings, commit=False)  # the request transaction rolls back

    with factory() as s:
        (brief,) = s.scalars(select(RecommendationBrief)).all()
        assert brief.id == kept
        usage = s.get(LLMUsageEvent, brief.llm_usage_event_id)
        assert usage is not None and (usage.provider, usage.workspace_id) == ("anthropic", "ws-p")
        assert usage.cost_usd > 0
        # Both calls were reserved and settled in transactions of their own: the rolled-back
        # capture's spend is still on the ledger.
        assert s.scalar(select(func.count()).select_from(LLMUsageEvent)) == 2
        states = s.scalars(select(LLMBudgetReservation.state)).all()
        assert sorted(states) == ["settled", "settled"]
    assert len(bodies) == 2
