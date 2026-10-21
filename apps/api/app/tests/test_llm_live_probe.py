"""The E6 live-provider probe, exercised offline: it refuses without explicit confirmation
and, given a replayed provider, runs each step, bounds the billable calls, reports the
uninducible branches as such, and never emits the key or model text."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.llm import live_probe

KEY = "fixture-key"
CLASSIFIED = {"signal_type": "complaint", "pain_point_dna": "speed_complaint"}
EXPLAINED = {
    "why_it_matters": "w",
    "who_cares": "c",
    "ai_inference": "MODEL-TEXT-MUST-NOT-BE-EMITTED",
    "recommended_action": "r",
}


def _message(output: dict, stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "type": "message",
            "content": [{"type": "text", "text": json.dumps(output)}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 120, "output_tokens": 30},
        },
    )


class FakeProvider:
    """Answers like the Messages API would for each probe step."""

    def __init__(self, *, truncate: bool = True):
        self.truncate = truncate
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        assert request.headers["x-api-key"] == KEY
        if body["model"] == live_probe.REJECTED_MODEL:
            return httpx.Response(
                404, json={"type": "error", "error": {"type": "not_found_error", "message": "m"}}
            )
        if body["max_tokens"] == 4 and self.truncate:
            return _message(CLASSIFIED, stop_reason="max_tokens")
        if "explanation engine" in body["messages"][0]["content"]:
            return _message(EXPLAINED)
        return _message(CLASSIFIED)

    def client_factory(self, timeout: float) -> httpx.Client:
        if timeout == 0.001:
            def slow(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectTimeout("timed out", request=request)

            return httpx.Client(transport=httpx.MockTransport(slow))
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _settings(**overrides) -> Settings:
    return Settings(
        llm_provider="anthropic", llm_api_key=KEY, llm_model="claude-sonnet-4-6", **overrides
    )


def test_refuses_without_explicit_confirmation(capsys):
    assert live_probe.main([]) == 2
    assert live_probe.CONFIRM_FLAG in capsys.readouterr().err


def test_refuses_without_a_real_provider():
    records: list[dict] = []
    assert live_probe.run_probe(Settings(), emit=records.append) == 2
    assert records == [
        {"step": "preflight", "outcome": "refused", "reason": "no real provider configured"}
    ]


def test_runs_every_step_with_bounded_billable_calls():
    fake = FakeProvider()
    records: list[dict] = []
    assert live_probe.run_probe(_settings(), client_factory=fake.client_factory,
                                emit=records.append) == 0
    steps = {r["step"]: r for r in records}
    assert list(steps) == [
        "success_classify",
        "success_explain",
        "truncated",
        "timeout",
        "rejected_model",
        "rate_limit_429",
        "server_error_5xx",
        "summary",
    ]
    assert steps["success_classify"]["outcome"] == "ok"
    assert steps["success_classify"]["is_simulated"] is False
    assert steps["success_classify"]["provider"] == "anthropic"
    assert steps["success_explain"]["validated_keys"] == sorted(
        ["why_it_matters", "who_cares", "ai_inference", "recommended_action", "risk_note",
         "suggested_angles"]
    )
    assert steps["truncated"]["outcome"] == "LLMMalformedError"
    assert steps["timeout"]["outcome"] == "LLMTimeoutError"
    assert steps["rejected_model"]["outcome"] == "LLMProviderError"
    assert all(steps[s]["as_expected"] for s in list(steps)[:5])
    for uninduced in ("rate_limit_429", "server_error_5xx"):
        assert steps[uninduced]["outcome"] == "not_induced"
        assert steps[uninduced]["covered_by"] == live_probe.REPLAY_COVERAGE
    assert steps["summary"] == {"step": "summary", "unexpected": 0}
    # Billable requests (anything that reached a real model id): at most four, no retries.
    billable = [r for r in fake.requests if r["model"] != live_probe.REJECTED_MODEL]
    assert len(billable) == 3 and all(r["max_tokens"] <= 1024 for r in billable)
    emitted = json.dumps(records)
    assert KEY not in emitted and "MODEL-TEXT-MUST-NOT-BE-EMITTED" not in emitted
    assert live_probe.SYNTHETIC_CLASSIFY["content"] not in emitted


def test_an_unexpected_outcome_fails_the_probe():
    fake = FakeProvider(truncate=False)  # the provider ignores max_tokens=4
    records: list[dict] = []
    assert live_probe.run_probe(_settings(), client_factory=fake.client_factory,
                                emit=records.append) == 1
    truncated = next(r for r in records if r["step"] == "truncated")
    assert truncated["outcome"] == "ok" and truncated["as_expected"] is False
    assert records[-1] == {"step": "summary", "unexpected": 1}


@pytest.mark.parametrize("flag", ["--confirm", "--yes", "-y"])
def test_near_miss_flags_do_not_confirm(flag):
    with pytest.raises(SystemExit):
        live_probe.main([flag])
