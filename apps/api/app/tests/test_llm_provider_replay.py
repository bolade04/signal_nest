"""P6-LLM-1: every real-adapter path executed under test by recorded-response replay.

The Anthropic and OpenAI adapters are driven through ``httpx.MockTransport``: each
test serves a fixed provider response (success, timeout, connection failure, 429 with
and without ``retry-after``, 5xx/529, 4xx, non-JSON body, non-JSON or truncated or
schema-invalid model text, refusal, unexpected stop reasons) and asserts both the
normalized outcome and the exact request the adapter put on the wire. No test opens a
network connection or uses a real key, and no test's settings come from the machine
(``app.tests._llm_offline``).

The fixtures are hand-written to the providers' documented response shapes (see the
``app.llm.providers_real`` module docstring for the source and its date); they are not
captures of live traffic. Recording real responses is the separate E6 demonstration.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.llm import service as service_mod
from app.llm.base import (
    LLMMalformedError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequest,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.providers_real import (
    ANTHROPIC_MESSAGES_URL,
    ANTHROPIC_VERSION,
    OPENAI_CHAT_URL,
    SYSTEM_PROMPT,
    AnthropicProvider,
    OpenAIProvider,
    anthropic_accepts_temperature,
)
from app.llm.schemas import ClassifySignalOut
from app.llm.service import LLMService
from app.tests._llm_offline import offline_llm, offline_settings

KEY = "fixture-key"
CLASSIFIED = {
    "signal_type": "complaint",
    "pain_point_dna": "speed_complaint",
    "sentiment": "negative",
    "audience_fit_label": "commuters buying coffee before 8am",
    "is_actionable": True,
    "rationale": "Repeated complaints about wait times.",
}
#: A provider error message that must never be echoed into exception text or logs.
LEAKY_MESSAGE = "prompt text: my-private-scraped-content"
#: Stands in for tenant context a model may echo in schema-invalid output.
MODEL_TEXT = "TENANT-MARKER-7f3a"


@pytest.fixture(scope="module", autouse=True)
def _offline():
    with offline_llm():
        yield


# --------------------------------------------------------------------------- #
# Replay harness
# --------------------------------------------------------------------------- #
Step = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


class Replay:
    """Serves the given steps in order and records every request the adapter sent."""

    def __init__(self, *steps: Step):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("adapter made more requests than the replay recorded")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step) and not isinstance(step, httpx.Response):
            return step(request)
        return step

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def _json_response(status: int, payload: Any, headers: dict[str, str] | None = None):
    return httpx.Response(status, json=payload, headers=headers or {})


def anthropic_ok(
    output: Any = None,
    *,
    text: str | None = None,
    stop_reason: str | None = "end_turn",
    extra_blocks: list[dict] | None = None,
) -> httpx.Response:
    content = list(extra_blocks or [])
    content.append({"type": "text", "text": text if text is not None else json.dumps(output)})
    return _json_response(
        200,
        {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 412, "output_tokens": 96},
        },
    )


def anthropic_error(
    status: int, error_type: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    return _json_response(
        status,
        {
            "type": "error",
            "error": {"type": error_type, "message": LEAKY_MESSAGE},
            "request_id": "req_fixture_1",
        },
        headers,
    )


def openai_ok(
    output: Any = None,
    *,
    content: str | None = None,
    finish_reason: str = "stop",
    refusal: str | None = None,
) -> httpx.Response:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content if content is not None else json.dumps(output),
    }
    if refusal is not None:
        message = {"role": "assistant", "content": None, "refusal": refusal}
    return _json_response(
        200,
        {
            "id": "chatcmpl-fixture",
            "object": "chat.completion",
            "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 80, "total_tokens": 380},
        },
    )


def openai_error(status: int, error_type: str, headers: dict[str, str] | None = None):
    return _json_response(
        status,
        {"error": {"message": LEAKY_MESSAGE, "type": error_type, "code": None}},
        headers,
    )


def _request(task: str = "classify_signal", **inputs: Any) -> LLMRequest:
    if not inputs:
        inputs = {"content": "the queue at the cafe is so slow", "audiences": ["commuters"]}
    return LLMRequest(task=task, inputs=inputs, temperature=0.0, max_tokens=640)


def _anthropic(replay: Replay, model: str = "claude-sonnet-4-6") -> AnthropicProvider:
    return AnthropicProvider(KEY, model, 5, client=replay.client())


def _openai(replay: Replay) -> OpenAIProvider:
    return OpenAIProvider(KEY, "gpt-4o-mini", 5, client=replay.client())


def _generate(provider) -> Any:
    return provider.generate(_request(), "classify_signal", "1.1.0")


# --------------------------------------------------------------------------- #
# Anthropic: success and request shape
# --------------------------------------------------------------------------- #
class TestAnthropicSuccess:
    def test_parses_text_output_usage_and_metadata(self):
        replay = Replay(anthropic_ok(CLASSIFIED))
        response = _generate(_anthropic(replay))
        assert response.output == CLASSIFIED
        assert response.provider == "anthropic" and response.model == "claude-sonnet-4-6"
        assert response.usage.input_tokens == 412 and response.usage.output_tokens == 96
        assert response.is_simulated is False and response.status == "ok"
        assert (response.prompt_name, response.prompt_version) == ("classify_signal", "1.1.0")

    def test_request_matches_the_messages_api(self):
        replay = Replay(anthropic_ok(CLASSIFIED))
        _generate(_anthropic(replay))
        [sent] = replay.requests
        assert sent.method == "POST" and str(sent.url) == ANTHROPIC_MESSAGES_URL
        assert sent.headers["x-api-key"] == KEY
        assert sent.headers["anthropic-version"] == ANTHROPIC_VERSION == "2023-06-01"
        assert sent.headers["content-type"] == "application/json"
        assert "authorization" not in sent.headers
        body = replay.body()
        assert set(body) == {"model", "max_tokens", "system", "messages", "temperature"}
        assert body["model"] == "claude-sonnet-4-6"
        assert body["max_tokens"] == 640
        assert body["system"] == SYSTEM_PROMPT
        assert body["temperature"] == 0.0
        [message] = body["messages"]
        assert message["role"] == "user"
        # The rendered prompt carries the inputs the pipeline passes (1.0.0 dropped them).
        assert "the queue at the cafe is so slow" in message["content"]
        assert "Target audiences: commuters" in message["content"]
        assert message["content"].endswith("Respond with strict minified JSON only.")

    @pytest.mark.parametrize(
        "model, sends",
        [
            ("claude-sonnet-4-6", True),
            ("claude-haiku-4-5", True),
            ("claude-haiku-4-5-20251001", True),
            ("claude-opus-4-6", True),
            ("claude-sonnet-5", False),
            ("claude-opus-5", False),
            ("claude-opus-4-8", False),
            ("claude-fable-5-1", False),
            ("some-future-model", False),
        ],
    )
    def test_temperature_only_for_models_that_accept_it(self, model, sends):
        assert anthropic_accepts_temperature(model) is sends
        replay = Replay(anthropic_ok(CLASSIFIED))
        _generate(_anthropic(replay, model))
        assert ("temperature" in replay.body()) is sends

    def test_default_model_when_unset(self):
        assert AnthropicProvider(KEY, "", 5, client=Replay().client()).model == "claude-sonnet-4-6"

    def test_thinking_blocks_are_ignored_and_text_blocks_parsed(self):
        replay = Replay(
            anthropic_ok(CLASSIFIED, extra_blocks=[{"type": "thinking", "thinking": ""}])
        )
        assert _generate(_anthropic(replay)).output == CLASSIFIED

    def test_single_markdown_fence_is_tolerated(self):
        replay = Replay(anthropic_ok(text="```json\n" + json.dumps(CLASSIFIED) + "\n```"))
        assert _generate(_anthropic(replay)).output == CLASSIFIED

    def test_stop_sequence_is_a_normal_stop(self):
        replay = Replay(anthropic_ok(CLASSIFIED, stop_reason="stop_sequence"))
        assert _generate(_anthropic(replay)).output == CLASSIFIED

    def test_missing_or_odd_usage_counts_as_zero(self):
        resp = anthropic_ok(CLASSIFIED)
        payload = json.loads(resp.content)
        payload["usage"] = {"input_tokens": "12", "output_tokens": True}
        replay = Replay(_json_response(200, payload))
        usage = _generate(_anthropic(replay)).usage
        assert (usage.input_tokens, usage.output_tokens) == (0, 0)


# --------------------------------------------------------------------------- #
# Anthropic: transport and HTTP failures
# --------------------------------------------------------------------------- #
class TestAnthropicFailures:
    def test_timeout(self):
        replay = Replay(httpx.ReadTimeout("read timed out"))
        with pytest.raises(LLMTimeoutError, match="anthropic request timed out"):
            _generate(_anthropic(replay))

    def test_connection_failure_is_retryable_unavailable(self):
        replay = Replay(httpx.ConnectError("connection refused"))
        with pytest.raises(LLMUnavailableError, match="ConnectError"):
            _generate(_anthropic(replay))

    def test_429_carries_numeric_retry_after(self):
        replay = Replay(anthropic_error(429, "rate_limit_error", {"retry-after": "7"}))
        with pytest.raises(LLMRateLimitError) as exc:
            _generate(_anthropic(replay))
        assert exc.value.retry_after == 7.0
        assert "HTTP 429" in str(exc.value)
        assert "type=rate_limit_error" in str(exc.value)
        assert "request_id=req_fixture_1" in str(exc.value)

    @pytest.mark.parametrize(
        "headers", [{}, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, {"retry-after": "-3"}]
    )
    def test_429_without_usable_retry_after(self, headers):
        replay = Replay(anthropic_error(429, "rate_limit_error", headers))
        with pytest.raises(LLMRateLimitError) as exc:
            _generate(_anthropic(replay))
        assert exc.value.retry_after is None

    @pytest.mark.parametrize(
        "status, error_type",
        [(500, "api_error"), (529, "overloaded_error"), (503, "api_error"), (408, "api_error")],
    )
    def test_server_side_failures_are_unavailable(self, status, error_type):
        replay = Replay(anthropic_error(status, error_type))
        with pytest.raises(LLMUnavailableError) as exc:
            _generate(_anthropic(replay))
        assert f"HTTP {status}" in str(exc.value) and f"type={error_type}" in str(exc.value)

    def test_html_gateway_error_is_unavailable(self):
        replay = Replay(httpx.Response(502, text="<html>Bad Gateway</html>"))
        with pytest.raises(LLMUnavailableError, match="HTTP 502"):
            _generate(_anthropic(replay))

    @pytest.mark.parametrize(
        "status, error_type",
        [
            (400, "invalid_request_error"),
            (401, "authentication_error"),
            (403, "permission_error"),
            (404, "not_found_error"),
            (413, "request_too_large"),
        ],
    )
    def test_client_errors_are_permanent(self, status, error_type):
        replay = Replay(anthropic_error(status, error_type))
        with pytest.raises(LLMProviderError) as exc:
            _generate(_anthropic(replay))
        assert not isinstance(exc.value, LLMUnavailableError)
        assert f"HTTP {status}" in str(exc.value)

    def test_redirect_is_not_followed_or_parsed(self):
        replay = Replay(httpx.Response(307, headers={"location": "https://example.invalid/"}))
        with pytest.raises(LLMProviderError, match="HTTP 307"):
            _generate(_anthropic(replay))
        assert len(replay.requests) == 1

    def test_error_text_never_echoes_the_provider_message_or_key(self):
        replay = Replay(anthropic_error(400, "invalid_request_error"))
        with pytest.raises(LLMProviderError) as exc:
            _generate(_anthropic(replay))
        assert LEAKY_MESSAGE not in str(exc.value) and KEY not in str(exc.value)

    def test_hostile_error_fields_are_not_echoed(self):
        replay = Replay(
            _json_response(
                400,
                {
                    "type": "error",
                    "error": {"type": "bad\nINJECTED log line", "message": "x"},
                    "request_id": "req with spaces",
                },
            )
        )
        with pytest.raises(LLMProviderError) as exc:
            _generate(_anthropic(replay))
        assert "INJECTED" not in str(exc.value) and "spaces" not in str(exc.value)


# --------------------------------------------------------------------------- #
# Anthropic: malformed 2xx replies
# --------------------------------------------------------------------------- #
class TestAnthropicMalformed:
    def test_non_json_body(self):
        replay = Replay(httpx.Response(200, text="<html>maintenance</html>"))
        with pytest.raises(LLMMalformedError, match="non-JSON body"):
            _generate(_anthropic(replay))

    def test_body_that_is_not_an_object(self):
        replay = Replay(_json_response(200, ["not", "a", "message"]))
        with pytest.raises(LLMMalformedError, match="unexpected body shape"):
            _generate(_anthropic(replay))

    @pytest.mark.parametrize(
        "text",
        [
            "Sure! Here is the classification you asked for.",
            '{"signal_type": "complaint", "pain_point_dna": ',
            "",
        ],
    )
    def test_model_text_that_is_not_json(self, text):
        replay = Replay(anthropic_ok(text=text))
        with pytest.raises(LLMMalformedError, match="non-JSON text"):
            _generate(_anthropic(replay))

    def test_json_that_is_not_an_object(self):
        replay = Replay(anthropic_ok(text='["complaint"]'))
        with pytest.raises(LLMMalformedError, match="not an object"):
            _generate(_anthropic(replay))

    def test_truncated_at_max_tokens(self):
        replay = Replay(anthropic_ok(CLASSIFIED, stop_reason="max_tokens"))
        with pytest.raises(LLMMalformedError, match="truncated"):
            _generate(_anthropic(replay))

    def test_refusal(self):
        replay = Replay(anthropic_ok(text="", stop_reason="refusal"))
        with pytest.raises(LLMRefusalError):
            _generate(_anthropic(replay))

    @pytest.mark.parametrize("stop_reason", ["tool_use", "pause_turn", None, "something_new"])
    def test_unexpected_stop_reason(self, stop_reason):
        replay = Replay(anthropic_ok(CLASSIFIED, stop_reason=stop_reason))
        with pytest.raises(LLMMalformedError, match="stop_reason"):
            _generate(_anthropic(replay))

    @pytest.mark.parametrize(
        "content", [None, "text", [], [{"type": "thinking", "thinking": "x"}], [{"type": "text"}]]
    )
    def test_missing_text_content(self, content):
        payload = json.loads(anthropic_ok(CLASSIFIED).content)
        payload["content"] = content
        replay = Replay(_json_response(200, payload))
        with pytest.raises(LLMMalformedError):
            _generate(_anthropic(replay))


# --------------------------------------------------------------------------- #
# OpenAI adapter (kept in the codebase: same replay coverage)
# --------------------------------------------------------------------------- #
class TestOpenAI:
    def test_success_and_request_shape(self):
        replay = Replay(openai_ok(CLASSIFIED))
        response = _generate(_openai(replay))
        assert response.output == CLASSIFIED and response.provider == "openai"
        assert (response.usage.input_tokens, response.usage.output_tokens) == (300, 80)
        [sent] = replay.requests
        assert str(sent.url) == OPENAI_CHAT_URL
        assert sent.headers["authorization"] == f"Bearer {KEY}"
        body = replay.body()
        assert body["model"] == "gpt-4o-mini"
        assert body["max_completion_tokens"] == 640
        assert body["response_format"] == {"type": "json_object"}
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["messages"][0]["content"] == SYSTEM_PROMPT

    def test_default_model_when_unset(self):
        assert OpenAIProvider(KEY, "", 5, client=Replay().client()).model == "gpt-4o-mini"

    def test_timeout(self):
        with pytest.raises(LLMTimeoutError):
            _generate(_openai(Replay(httpx.ConnectTimeout("connect timed out"))))

    def test_429(self):
        replay = Replay(openai_error(429, "rate_limit_exceeded", {"retry-after": "2"}))
        with pytest.raises(LLMRateLimitError) as exc:
            _generate(_openai(replay))
        assert exc.value.retry_after == 2.0 and LEAKY_MESSAGE not in str(exc.value)

    def test_5xx(self):
        with pytest.raises(LLMUnavailableError):
            _generate(_openai(Replay(openai_error(503, "server_error"))))

    def test_4xx(self):
        with pytest.raises(LLMProviderError) as exc:
            _generate(_openai(Replay(openai_error(401, "invalid_request_error"))))
        assert not isinstance(exc.value, LLMUnavailableError)

    def test_non_json_body(self):
        with pytest.raises(LLMMalformedError):
            _generate(_openai(Replay(httpx.Response(200, text="upstream reset"))))

    def test_non_json_content(self):
        with pytest.raises(LLMMalformedError):
            _generate(_openai(Replay(openai_ok(content="I cannot produce JSON."))))

    def test_truncated(self):
        with pytest.raises(LLMMalformedError, match="truncated"):
            _generate(_openai(Replay(openai_ok(CLASSIFIED, finish_reason="length"))))

    @pytest.mark.parametrize(
        "response",
        [openai_ok(CLASSIFIED, finish_reason="content_filter"), openai_ok(refusal="I can't")],
    )
    def test_refusal(self, response):
        with pytest.raises(LLMRefusalError):
            _generate(_openai(Replay(response)))

    @pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": "x"}]}])
    def test_missing_message(self, payload):
        with pytest.raises(LLMMalformedError):
            _generate(_openai(Replay(_json_response(200, payload))))

    def test_missing_content(self):
        payload = json.loads(openai_ok(CLASSIFIED).content)
        payload["choices"][0]["message"]["content"] = None
        with pytest.raises(LLMMalformedError):
            _generate(_openai(Replay(_json_response(200, payload))))


# --------------------------------------------------------------------------- #
# Service: bounded retries and backoff over the real adapter
# --------------------------------------------------------------------------- #
def _settings(**overrides: Any) -> Settings:
    return offline_settings(llm_provider="anthropic", llm_api_key=KEY, **overrides)


@pytest.fixture
def settings(monkeypatch):
    def install(**overrides: Any) -> Settings:
        s = _settings(**overrides)
        monkeypatch.setattr(service_mod, "get_settings", lambda: s)
        return s

    install()
    return install


def _service(replay: Replay, sleeps: list[float]) -> LLMService:
    return LLMService(_anthropic(replay), sleep=sleeps.append)


INPUTS = {"content": "the queue at the cafe is so slow", "audiences": ["commuters"]}


class TestServiceRetries:
    def test_transient_failures_are_retried_with_backoff_then_succeed(self, settings):
        replay = Replay(
            anthropic_error(429, "rate_limit_error", {"retry-after": "3"}),
            anthropic_error(529, "overloaded_error"),
            anthropic_ok(CLASSIFIED),
        )
        sleeps: list[float] = []
        validated, response = _service(replay, sleeps).run("classify_signal", INPUTS)
        assert isinstance(validated, ClassifySignalOut)
        assert validated.signal_type == "complaint" and response.provider == "anthropic"
        assert len(replay.requests) == 3
        # attempt 0: the provider's retry-after; attempt 1: 0.1 * 2**1.
        assert sleeps == [3.0, 0.2]

    def test_retry_after_is_clamped_to_the_cap(self, settings):
        settings(llm_retry_after_cap_seconds=4.0)
        replay = Replay(
            anthropic_error(429, "rate_limit_error", {"retry-after": "120"}),
            anthropic_ok(CLASSIFIED),
        )
        sleeps: list[float] = []
        _service(replay, sleeps).run("classify_signal", INPUTS)
        assert sleeps == [4.0]

    def test_attempts_are_bounded_and_no_sleep_follows_the_last(self, settings):
        replay = Replay(
            anthropic_error(500, "api_error"),
            httpx.ReadTimeout("slow"),
            anthropic_error(529, "overloaded_error"),
        )
        sleeps: list[float] = []
        with pytest.raises(LLMUnavailableError):
            _service(replay, sleeps).run("classify_signal", INPUTS)
        assert len(replay.requests) == 3  # llm_max_retries=2 -> 3 attempts
        assert sleeps == [0.1, 0.2]

    def test_zero_retries_means_one_attempt(self, settings):
        settings(llm_max_retries=0)
        replay = Replay(anthropic_error(529, "overloaded_error"))
        sleeps: list[float] = []
        with pytest.raises(LLMUnavailableError):
            _service(replay, sleeps).run("classify_signal", INPUTS)
        assert len(replay.requests) == 1 and sleeps == []

    @pytest.mark.parametrize(
        "step, error",
        [
            (anthropic_error(400, "invalid_request_error"), LLMProviderError),
            (anthropic_error(401, "authentication_error"), LLMProviderError),
            (httpx.Response(200, text="not json"), LLMMalformedError),
            (anthropic_ok(text="plain prose"), LLMMalformedError),
            (anthropic_ok(CLASSIFIED, stop_reason="max_tokens"), LLMMalformedError),
            (anthropic_ok(text="", stop_reason="refusal"), LLMRefusalError),
        ],
    )
    def test_permanent_failures_are_not_retried(self, settings, step, error):
        replay = Replay(step)
        sleeps: list[float] = []
        with pytest.raises(error):
            _service(replay, sleeps).run("classify_signal", INPUTS)
        assert len(replay.requests) == 1 and sleeps == []

    def test_schema_invalid_json_is_malformed_and_not_retried(self, settings):
        replay = Replay(anthropic_ok({"sentiment": "negative"}))  # no signal_type
        sleeps: list[float] = []
        with pytest.raises(LLMMalformedError, match="schema validation"):
            _service(replay, sleeps).run("classify_signal", INPUTS)
        assert len(replay.requests) == 1

    def test_schema_invalid_types_are_malformed(self, settings):
        replay = Replay(
            anthropic_ok(
                {
                    "why_it_matters": "x",
                    "who_cares": "y",
                    "ai_inference": "z",
                    "recommended_action": "a",
                    "suggested_angles": "not-a-list",
                }
            )
        )
        with pytest.raises(LLMMalformedError):
            _service(replay, []).run("explain_opportunity", {"resolved_market": "Dallas"})

    def test_max_output_tokens_setting_reaches_the_wire(self, settings):
        settings(llm_max_output_tokens=333)
        replay = Replay(anthropic_ok(CLASSIFIED))
        _service(replay, []).run("classify_signal", INPUTS)
        assert replay.body()["max_tokens"] == 333

    def test_failures_are_logged_without_body_text(self, settings, caplog):
        caplog.set_level(logging.DEBUG, logger="signalnest.llm")
        replay = Replay(
            anthropic_error(429, "rate_limit_error"), anthropic_error(400, "invalid_request_error")
        )
        with pytest.raises(LLMProviderError):
            _service(replay, []).run("classify_signal", INPUTS)
        events = {
            r.getMessage(): getattr(r, "extra_fields", {})
            for r in caplog.records
            if r.name == "signalnest.llm"
        }
        assert set(events) == {"llm.retry", "llm.failed"}
        assert events["llm.failed"]["error_class"] == "LLMProviderError"
        assert "HTTP 400" in events["llm.failed"]["error"]
        logged = json.dumps(list(events.values()))
        assert LEAKY_MESSAGE not in logged and KEY not in logged

    @pytest.mark.parametrize(
        "task, output, error",
        [
            (
                "classify_signal",
                {**CLASSIFIED, "is_actionable": MODEL_TEXT},
                "is_actionable: bool_parsing",
            ),
            (
                "classify_signal",
                {"sentiment": "negative", "rationale": MODEL_TEXT},
                "signal_type: missing",
            ),
            (
                "explain_opportunity",
                {
                    "why_it_matters": "w",
                    "who_cares": "c",
                    "ai_inference": "i",
                    "recommended_action": "r",
                    "suggested_angles": MODEL_TEXT,
                },
                "suggested_angles: list_type",
            ),
        ],
    )
    def test_schema_errors_never_carry_model_output(self, settings, caplog, task, output, error):
        caplog.set_level(logging.DEBUG, logger="signalnest.llm")
        with pytest.raises(LLMMalformedError) as exc:
            _service(Replay(anthropic_ok(output)), []).run(task, INPUTS)
        assert str(exc.value) == f"Response failed schema validation: {error}"
        # No chained ValidationError either: its text quotes the rejected input.
        assert exc.value.__cause__ is None and exc.value.__suppress_context__
        formatter = JsonFormatter(environment="staging")
        failed = [formatter.format(r) for r in caplog.records if r.getMessage() == "llm.failed"]
        assert len(failed) == 1 and error in failed[0]
        assert MODEL_TEXT not in failed[0] and "input_value" not in failed[0]

    def test_schema_error_locations_outside_the_schema_are_masked(self):
        class Keyed(BaseModel):
            scores: dict[str, int]

        with pytest.raises(ValidationError) as exc:
            Keyed.model_validate({"scores": {MODEL_TEXT: "high"}})
        assert service_mod._schema_errors(Keyed, exc.value) == "scores.?: int_parsing"

    def test_dev_fallback_still_serves_mock_when_explicitly_enabled(self, settings):
        settings(llm_allow_dev_fallback=True)
        replay = Replay(anthropic_error(400, "invalid_request_error"))
        validated, response = _service(replay, []).run("classify_signal", INPUTS)
        assert response.provider == "mock" and response.is_simulated is True
        assert isinstance(validated, ClassifySignalOut)


# --------------------------------------------------------------------------- #
# The offline harness itself
# --------------------------------------------------------------------------- #
def test_the_offline_harness_refuses_a_real_transport():
    # Loopback discard port: without the refusal this would fail to connect locally
    # (ConnectError), never reach another host, and still fail the test.
    with httpx.Client(transport=httpx.HTTPTransport()) as client:
        with pytest.raises(AssertionError, match="real HTTP request in an offline LLM test"):
            client.get("http://127.0.0.1:9/")


# --------------------------------------------------------------------------- #
# Configuration bounds
# --------------------------------------------------------------------------- #
class TestConfigBounds:
    def test_defaults_are_within_bounds(self):
        s = offline_settings()
        assert (s.llm_max_retries, s.llm_timeout_seconds) == (2, 30)
        assert (s.llm_max_output_tokens, s.llm_retry_after_cap_seconds) == (1024, 10.0)

    @pytest.mark.parametrize(
        "field, value",
        [
            ("llm_max_retries", -1),
            ("llm_max_retries", 6),
            ("llm_timeout_seconds", 0),
            ("llm_timeout_seconds", 121),
            ("llm_max_output_tokens", 0),
            ("llm_max_output_tokens", 16001),
            ("llm_retry_after_cap_seconds", -0.5),
            ("llm_retry_after_cap_seconds", 61),
            ("llm_retry_after_cap_seconds", float("nan")),
        ],
    )
    def test_out_of_bounds_values_are_rejected(self, field, value):
        with pytest.raises(ValidationError) as exc:
            offline_settings(**{field: value})
        assert field in str(exc.value)
