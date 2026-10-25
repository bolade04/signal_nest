"""P6-LLM-3: model output is screened before it is persisted or shown.

Unit level: classification labels are constrained to the closed vocabularies, and
explanation text carrying a link, contact detail, markup, an echoed injection marker
or a high/blocked claim is withheld; medium claims are kept with warnings; long
fields are capped. Pipeline level: a replayed real-adapter reply that fails screening
is stored as ``withheld`` placeholder copy with its claim warnings, never verbatim.

Every module-level path to a provider is offline (``app.tests._llm_offline``), and the
pipeline's settings come from ``offline_settings``, so the module behaves the same on a
machine whose environment configures a real provider.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.enums import ExplanationSource
from app.db import seed as seed_mod
from app.db.models import Base
from app.jobs import pipeline
from app.llm.providers_real import AnthropicProvider
from app.llm.schemas import ClassifySignalOut
from app.llm.screening import (
    MAX_ANGLES,
    MAX_FIELD_CHARS,
    screen_audience,
    screen_classification,
    screen_explanation,
)
from app.llm.service import LLMService
from app.opportunities.models import Opportunity
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal
from app.tests._llm_offline import offline_llm, offline_settings

CLEAN = {
    "why_it_matters": "Commuters keep reporting long waits at peak hours.",
    "who_cares": "Commuters buying coffee before 8am",
    "ai_inference": "A faster pre-order option would meet visible demand.",
    "recommended_action": "Promote mobile pre-order for the morning rush.",
    "risk_note": "Avoid naming competitors.",
    "suggested_angles": ["Skip the queue", "Ready when you arrive"],
}


def _with(**changes) -> dict:
    return {**CLEAN, **changes}


class TestClassification:
    def test_known_labels_pass_through_normalized(self):
        out = ClassifySignalOut(
            signal_type=" Complaint ", pain_point_dna="SPEED_COMPLAINT", sentiment="Negative",
            audience_fit_label="Commuters buying coffee before 8am",
        )
        assert screen_classification(out) == {
            "signal_type": "complaint",
            "pain_point_dna": "speed_complaint",
            "sentiment": "negative",
            "audience_fit_label": "Commuters buying coffee before 8am",
        }

    def test_unknown_or_injected_labels_become_none(self):
        out = ClassifySignalOut(
            signal_type="pwned", pain_point_dna="ignore previous instructions",
            sentiment="furious", audience_fit_label="visit https://evil.example now",
        )
        assert screen_classification(out) == {
            "signal_type": None,
            "pain_point_dna": None,
            "sentiment": "neutral",
            "audience_fit_label": None,
        }

    def test_audience_label_is_cleaned_and_capped(self):
        out = ClassifySignalOut(signal_type="complaint", audience_fit_label="A\x00b‮ " * 200)
        label = screen_classification(out)["audience_fit_label"]
        assert "\x00" not in label and "‮" not in label and len(label) <= 140


class TestExplanation:
    def test_clean_output_is_kept(self):
        screened = screen_explanation(CLEAN, "food and beverage")
        assert screened.withheld_reason is None and screened.output == CLEAN
        assert screened.warnings == []

    @pytest.mark.parametrize(
        "field, value, reason",
        [
            ("ai_inference", "See https://evil.example/deal for details.", "link"),
            ("recommended_action", "Visit www.evil.example today.", "link"),
            ("who_cares", "Email owner@evil.example for a refund.", "contact_detail"),
            ("why_it_matters", "<script>alert(1)</script> demand", "markup"),
            ("risk_note", "&lt;b&gt;escaped markup&lt;/b&gt;", "markup"),
            ("ai_inference", "[quoted:ignore previous instructions] done", "instruction_echo"),
            ("why_it_matters", "I will ignore previous instructions now.", "instruction_echo"),
            ("ai_inference", "My system prompt says to help.", "instruction_echo"),
            ("recommended_action", "Say our coffee cures headaches.", "claim_risk_high"),
            ("why_it_matters", "Results guaranteed for every customer.", "claim_risk_high"),
            ("ai_inference", "Tell them they will double your money.", "claim_risk_blocked"),
        ],
    )
    def test_unsafe_output_is_withheld(self, field, value, reason):
        screened = screen_explanation(_with(**{field: value}), None)
        assert screened.output is None and screened.withheld_reason == reason

    def test_unsafe_angles_are_screened_too(self):
        screened = screen_explanation(_with(suggested_angles=["ok", "go to bit.ly/x"]), None)
        assert screened.output is None

    def test_medium_claim_is_kept_with_warnings(self):
        screened = screen_explanation(_with(why_it_matters="Be the best coffee nearby."), None)
        assert screened.output is not None
        assert any("superlative best" in w for w in screened.warnings)
        assert all(w.startswith("AI explanation: ") for w in screened.warnings)

    def test_regulated_industry_escalates_medium_to_withheld(self):
        screened = screen_explanation(
            _with(why_it_matters="Be the best clinic nearby."), "healthcare"
        )
        assert screened.output is None and screened.withheld_reason == "claim_risk_high"
        assert screened.warnings and screened.warnings[0].startswith("AI explanation withheld:")

    def test_long_fields_are_capped_and_angles_limited(self):
        screened = screen_explanation(
            _with(ai_inference="slow " * 1000, suggested_angles=[f"angle {i}" for i in range(9)]),
            None,
        )
        assert len(screened.output["ai_inference"]) <= MAX_FIELD_CHARS
        assert screened.output["ai_inference"].endswith("…")
        assert len(screened.output["suggested_angles"]) == MAX_ANGLES


# --------------------------------------------------------------------------- #
# Fix pass 2 (adversarial r2 F5, F6, R2-11): normalised screening
# --------------------------------------------------------------------------- #
#: Probe P3a: every one of these was stored as model text at 491294c9.
BYPASSES = [
    "Visit deals\u200b.example/promo now",  # zero-width before the dot
    "Order at bestdeals.shop today",  # bare domain, TLD outside any list
    "See evil\uff0ecom/x",  # fullwidth full stop
    "Write to sales@deals\u200b.com",  # e-mail split by a zero-width character
    "ign\u200bore previous instructions",  # echo split by a zero-width character
    "Call +1 415 555 0100 to buy",  # phone number
    "Reply about untrusted_data blocks",  # R2-11: the delimiter name echoed
]
#: Ordinary explanation text that must still be kept.
LEGITIMATE = [
    "Sales rose 12.5% in 2025, up from 2024-2025 levels.",
    "Open 7am-9pm, Mon-Fri; e.g. weekends too.",
    "Customers in the U.S. and the U.K. ask for oat milk.",
    "A 3.5-star average across 1,200 reviews; waits of 10-15 minutes at 8.30am.",
    "The morning rush.Promote pre-order at the counter.",
    "Parents who act as carers want a quick pickup.",
]


class TestNormalisedScreening:
    @pytest.mark.parametrize("text", BYPASSES)
    def test_every_r2_bypass_is_withheld(self, text):
        for field in ("why_it_matters", "ai_inference"):
            assert screen_explanation(_with(**{field: text}), None).output is None
        assert screen_explanation(_with(suggested_angles=[text]), None).output is None

    @pytest.mark.parametrize("text", LEGITIMATE)
    def test_ordinary_text_is_still_kept(self, text):
        screened = screen_explanation(_with(why_it_matters=text), None)
        assert screened.output is not None, screened.withheld_reason
        assert screened.output["why_it_matters"] == text

    def test_the_normalised_text_is_what_is_stored(self):
        screened = screen_explanation(
            _with(why_it_matters="\uff23ommuters\u200b wait at peak\u00a0hours."), None
        )
        assert screened.output["why_it_matters"] == "Commuters wait at peak hours."

    def test_an_echoed_act_as_defang_is_unwrapped_not_withheld(self):
        screened = screen_explanation(
            _with(who_cares="Parents who [quoted:act as] carers"), None
        )
        assert screened.output is not None
        assert screened.output["who_cares"] == "Parents who act as carers"
        strong = _with(who_cares="Admins [quoted:ignore previous instructions]")
        assert screen_explanation(strong, None).withheld_reason == "instruction_echo"

    @pytest.mark.parametrize(
        "label",
        [
            "<b>Owners</b> of homes",
            "[quoted:ignore previous instructions] admins",
            "Diabetics: this guaranteed cure works 100%",
            "Readers of bestdeals.shop",
            "Call 415 555 0100 commuters",
        ],
    )
    def test_audience_label_gets_the_explanation_rules(self, label):
        assert screen_audience(label, "healthcare") is None
        out = ClassifySignalOut(signal_type="complaint", audience_fit_label=label)
        assert screen_classification(out, "healthcare")["audience_fit_label"] is None

    def test_an_ordinary_audience_label_is_kept(self):
        for label in ("Commuters buying coffee before 8am", "Parents who act as carers"):
            assert screen_audience(label, "food") == label


# --------------------------------------------------------------------------- #
# Pipeline: a reply that fails screening is never stored verbatim
# --------------------------------------------------------------------------- #
CLASSIFIED = {
    "signal_type": "complaint",
    "pain_point_dna": "speed_complaint",
    "sentiment": "negative",
    "audience_fit_label": "Commuters buying coffee before 8am",
}
UNSAFE = _with(ai_inference="Claim it cures fatigue - guaranteed. See https://evil.example")


def _reply(output: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": json.dumps(output)}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    )


def _service(classified: dict, explanation: dict) -> LLMService:
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][0]["content"]
        return _reply(classified if content.startswith("Classify") else explanation)

    provider = AnthropicProvider(
        "fixture-key", "claude-sonnet-4-6", 5,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return LLMService(provider, sleep=lambda _s: None, settings=offline_settings())


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("llm3")
    engine = create_engine(
        f"sqlite:///{tmp / 'llm3.db'}", connect_args={"check_same_thread": False}, future=True
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


def _rerun(factory, monkeypatch, service):
    monkeypatch.setattr(pipeline, "llm_service", service)
    monkeypatch.setattr(pipeline, "get_settings", offline_settings)
    with factory() as db:
        request_id = db.scalars(select(ScoutRequest.id).order_by(ScoutRequest.id)).first()
        before = set(db.scalars(select(Opportunity.id)))
        before_signals = set(db.scalars(select(NormalizedSignal.id)))
        pipeline._run(db, request_id)
        db.commit()
        opps = [o for o in db.scalars(select(Opportunity)) if o.id not in before]
        signals = [s for s in db.scalars(select(NormalizedSignal)) if s.id not in before_signals]
    assert opps
    return opps, signals


def test_seeded_mock_output_passes_screening(seeded):
    with seeded() as db:
        sources = {o.explanation_source for o in db.scalars(select(Opportunity))}
    assert ExplanationSource.WITHHELD.value not in sources


def test_unsafe_explanation_is_withheld_with_its_warnings(seeded, monkeypatch):
    opps, _ = _rerun(seeded, monkeypatch, _service(CLASSIFIED, UNSAFE))
    withheld = [o for o in opps if o.explanation_source == ExplanationSource.WITHHELD.value]
    assert withheld
    for o in withheld:
        assert o.why_it_matters == pipeline.WITHHELD_EXPLANATION["why_it_matters"]
        assert o.ai_inference is None and o.suggested_angles == []
        stored = json.dumps(
            [o.why_it_matters, o.who_cares, o.ai_inference, o.recommended_action,
             o.suggested_angles, o.risk_note]
        )
        assert "evil.example" not in stored and "cures" not in stored
    assert all(o.explanation_source in {"withheld", "rule_based"} for o in opps)


def test_claim_warnings_from_kept_model_text_reach_the_opportunity(seeded, monkeypatch):
    medium = _with(why_it_matters="Be the best coffee stop on the commute.")
    opps, _ = _rerun(seeded, monkeypatch, _service(CLASSIFIED, medium))
    modelled = [o for o in opps if o.explanation_source == ExplanationSource.MODEL.value]
    assert modelled
    for o in modelled:
        assert any(w.startswith("AI explanation: ") for w in o.claims_warnings)


def test_injected_classification_labels_never_reach_titles_or_grouping(seeded, monkeypatch):
    injected = {
        **CLASSIFIED,
        "signal_type": "IGNORE PREVIOUS INSTRUCTIONS",
        "pain_point_dna": "pwned_by_post",
    }
    opps, signals = _rerun(seeded, monkeypatch, _service(injected, CLEAN))
    analysed = [s for s in signals if not s.is_noise and not s.is_duplicate]
    assert analysed and all(s.signal_type is None and s.pain_point_dna is None for s in analysed)
    assert all("pwned" not in o.title.lower() and "ignore" not in o.title.lower() for o in opps)


# Runs last: it writes withheld rows into the module-scoped seed database.
def test_a_rejected_audience_label_falls_back_to_the_business_audience(seeded, monkeypatch):
    """Probe P3c end to end: the model's audience label carries markup and a blocked
    claim. Neither ``who_cares`` nor ``audience_fit`` may store it, whether or not the
    explanation is withheld; the business's own audience is used instead."""
    label = "<i>Diabetics</i> who want a guaranteed cure"
    classified = {**CLASSIFIED, "audience_fit_label": label}
    for explanation in (UNSAFE, CLEAN):
        opps, signals = _rerun(seeded, monkeypatch, _service(classified, explanation))
        assert all(s.ingest_metadata.get("audience_label") is None for s in signals
                   if s.ingest_metadata.get("classification_source") == "model")
        for o in opps:
            for stored in (o.who_cares, o.audience_fit):
                assert "<i>" not in (stored or "") and "guaranteed cure" not in (stored or "")


def test_the_pipeline_screens_the_audience_label_for_the_business_industry(
    seeded, monkeypatch
):
    """F6: the label's claim check uses the business's industry. "best" is a medium
    claim, which a regulated industry escalates to high, so for a healthcare business
    the label is dropped rather than shown."""
    import dataclasses

    real_context = pipeline.build_business_context

    def healthcare_context(db, workspace_id):
        return dataclasses.replace(real_context(db, workspace_id), industry="healthcare")

    monkeypatch.setattr(pipeline, "build_business_context", healthcare_context)
    label = "Patients who want the best results"
    assert screen_audience(label, None) == label  # kept where nothing is regulated
    classified = {**CLASSIFIED, "audience_fit_label": label}
    opps, signals = _rerun(seeded, monkeypatch, _service(classified, CLEAN))
    model_signals = [
        s for s in signals if s.ingest_metadata.get("classification_source") == "model"
    ]
    assert model_signals
    assert all(s.ingest_metadata.get("audience_label") is None for s in model_signals)
    assert all(o.audience_fit != label for o in opps)
