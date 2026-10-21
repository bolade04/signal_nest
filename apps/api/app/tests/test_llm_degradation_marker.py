"""P6-LLM-5: degraded explanations are persisted WITH a marker and surfaced by the API.

The scout pipeline runs end to end against a throwaway four-market seed with its LLM
service swapped for the real Anthropic adapter over a recorded-response replay (no
network). Three provider behaviours are replayed - success, explanation failure,
classification failure - and the persisted opportunities and the opportunity detail
API are asserted for each:

* ``explanation_source == "model"`` only when the model's validated output was stored;
* ``"degraded"`` when the provider failed - the stored copy says no AI explanation
  exists (no fabricated inference, no generic "relevant to your market" filler);
* ``"rule_based"`` below the relevance action floor, where no model is consulted;
* a failed classification is recorded on the signal as ``classification_source``.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.enums import ExplanationSource
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.jobs import pipeline
from app.llm.providers_real import AnthropicProvider
from app.llm.service import LLMService
from app.main import app
from app.opportunities.models import Opportunity
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal
from app.tests._auth2_support import live_bearer

API = get_settings().api_prefix
EXPLANATION = {
    "why_it_matters": "Commuters in this market keep reporting long waits at peak hours.",
    "who_cares": "Commuters buying coffee before 8am",
    "ai_inference": "Inference: a faster pre-order option would meet visible demand.",
    "recommended_action": "Promote mobile pre-order for the morning rush.",
    "risk_note": "Avoid naming competitors.",
    "suggested_angles": ["Skip the queue", "Ready when you arrive"],
}
CLASSIFIED = {
    "signal_type": "complaint",
    "pain_point_dna": "speed_complaint",
    "sentiment": "negative",
    "audience_fit_label": "Commuters buying coffee before 8am",
    "is_actionable": True,
    "rationale": "Repeated complaints about wait times.",
}
OLD_CANNED = ("Signal is relevant to your market.", "Demand appears to be forming.")


def _ok(output: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": json.dumps(output)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    )


def _overloaded() -> httpx.Response:
    return httpx.Response(
        529,
        json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )


def _task(request: httpx.Request) -> str:
    content = json.loads(request.content)["messages"][0]["content"]
    if content.startswith("You are SignalNest's signal classifier"):
        return "classify_signal"
    if content.startswith("You are SignalNest's explanation engine"):
        return "explain_opportunity"
    raise AssertionError(f"unexpected prompt: {content[:60]!r}")


def _replay_service(*, classify_ok: bool, explain_ok: bool) -> tuple[LLMService, list[str]]:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        task = _task(request)
        calls.append(task)
        if task == "classify_signal":
            return _ok(CLASSIFIED) if classify_ok else _overloaded()
        return _ok(EXPLANATION) if explain_ok else _overloaded()

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider("fixture-key", "claude-sonnet-4-6", 5, client=client)
    return LLMService(provider, sleep=lambda _s: None), calls


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("llm5")
    engine = create_engine(
        f"sqlite:///{tmp / 'llm5.db'}", connect_args={"check_same_thread": False}, future=True
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = factory
    try:
        seed_mod.seed(reset=True)
        yield factory
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


def _rerun(factory, monkeypatch, service: LLMService) -> tuple[list[Opportunity], list]:
    """Re-run the first seeded scout request with ``service``; return what this run created."""
    monkeypatch.setattr(pipeline, "llm_service", service)
    with factory() as db:
        request_id = db.scalars(select(ScoutRequest.id).order_by(ScoutRequest.id)).first()
        before_opps = set(db.scalars(select(Opportunity.id)))
        before_signals = set(db.scalars(select(NormalizedSignal.id)))
        pipeline._run(db, request_id)
        db.commit()
        opps = [
            o for o in db.scalars(select(Opportunity)) if o.id not in before_opps
        ]
        signals = [
            s for s in db.scalars(select(NormalizedSignal)) if s.id not in before_signals
        ]
    assert opps, "the re-run must create opportunities to assert on"
    return opps, signals


def test_seeded_mock_opportunities_record_their_source(seeded):
    with seeded() as db:
        sources = {o.explanation_source for o in db.scalars(select(Opportunity))}
    assert sources <= {ExplanationSource.MODEL.value, ExplanationSource.RULE_BASED.value}
    assert ExplanationSource.MODEL.value in sources
    assert None not in sources


def test_successful_model_output_is_marked_model(seeded, monkeypatch):
    service, calls = _replay_service(classify_ok=True, explain_ok=True)
    opps, signals = _rerun(seeded, monkeypatch, service)
    assert "classify_signal" in calls and "explain_opportunity" in calls
    modelled = [o for o in opps if o.explanation_source == ExplanationSource.MODEL.value]
    assert modelled
    for o in modelled:
        assert o.ai_inference == EXPLANATION["ai_inference"]
        assert o.why_it_matters == EXPLANATION["why_it_matters"]
        assert o.suggested_angles == EXPLANATION["suggested_angles"]
    for o in opps:
        assert o.explanation_source in {"model", "rule_based"}
    analysed = [s for s in signals if not s.is_noise and not s.is_duplicate]
    assert analysed and all(
        s.ingest_metadata["classification_source"] == "model" for s in analysed
    )


def test_provider_failure_persists_a_marked_honest_placeholder(seeded, monkeypatch):
    service, calls = _replay_service(classify_ok=True, explain_ok=False)
    opps, _ = _rerun(seeded, monkeypatch, service)
    degraded = [o for o in opps if o.explanation_source == ExplanationSource.DEGRADED.value]
    assert degraded, "an explanation failure must be stored as degraded"
    # Bounded retries: each failed explanation costs exactly llm_max_retries + 1 calls.
    attempts = get_settings().llm_max_retries + 1
    assert calls.count("explain_opportunity") == attempts * len(degraded)
    for o in degraded:
        assert o.why_it_matters == pipeline.DEGRADED_EXPLANATION["why_it_matters"]
        assert o.why_it_matters.startswith("AI explanation unavailable")
        assert o.ai_inference is None
        assert o.suggested_angles == []
        assert o.why_it_matters not in OLD_CANNED and o.ai_inference not in OLD_CANNED
    # Nothing in this run is labelled as model output.
    assert all(o.explanation_source in {"degraded", "rule_based"} for o in opps)


def test_classification_failure_is_recorded_and_the_run_completes(seeded, monkeypatch):
    service, calls = _replay_service(classify_ok=False, explain_ok=True)
    opps, signals = _rerun(seeded, monkeypatch, service)
    analysed = [s for s in signals if not s.is_noise and not s.is_duplicate]
    assert analysed
    for s in analysed:
        assert s.ingest_metadata["classification_source"] == "degraded"
        assert s.signal_type is None and s.pain_point_dna is None
    skipped = [s for s in signals if s.is_noise or s.is_duplicate]
    assert all(s.ingest_metadata["classification_source"] is None for s in skipped)
    assert opps


# --------------------------------------------------------------------------- #
# API: the marker is surfaced on the opportunity detail
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
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


def _detail(client, factory, opportunity: Opportunity) -> dict:
    token = live_bearer(seed_mod.sid("user"))
    response = client.get(
        f"{API}/workspaces/{opportunity.workspace_id}/opportunities/{opportunity.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_detail_api_surfaces_degraded_marker(seeded, client, monkeypatch):
    service, _ = _replay_service(classify_ok=True, explain_ok=False)
    opps, _ = _rerun(seeded, monkeypatch, service)
    degraded = next(o for o in opps if o.explanation_source == "degraded")
    body = _detail(client, seeded, degraded)
    assert body["explanation_source"] == "degraded"
    assert body["ai_inference"] is None
    assert body["why_it_matters"].startswith("AI explanation unavailable")


def test_detail_api_reports_unrecorded_rows_as_null(seeded, client):
    with seeded() as db:
        opp = db.scalars(select(Opportunity)).first()
        opp.explanation_source = None  # a row written before the marker existed
        db.commit()
    assert _detail(client, seeded, opp)["explanation_source"] is None


def test_card_contract_is_unchanged(seeded, client):
    with seeded() as db:
        opp = db.scalars(select(Opportunity)).first()
    token = live_bearer(seed_mod.sid("user"))
    response = client.get(
        f"{API}/workspaces/{opp.workspace_id}/opportunities",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    items = response.json()
    items = items["items"] if isinstance(items, dict) else items
    assert items and all("explanation_source" not in item for item in items)
