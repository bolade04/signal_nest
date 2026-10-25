"""Real OpenAI/Anthropic adapters.

Hand-rolled ``httpx`` clients sharing the exact ``LLMResponse`` contract as the mock.
Every HTTP outcome is normalized onto the ``app.llm.base`` taxonomy so the service
can decide what is retryable:

* transport timeout -> ``LLMTimeoutError`` (retryable)
* 429 -> ``LLMRateLimitError`` carrying the provider's ``retry-after`` (retryable)
* 408, 409, 5xx (incl. Anthropic 529 overloaded), connection failure ->
  ``LLMUnavailableError`` (retryable)
* any other non-2xx -> ``LLMProviderError`` (permanent: bad key, model or request)
* a 2xx whose body is not the documented envelope, whose text is not one JSON
  object, or that stopped early -> ``LLMMalformedError``; a refusal ->
  ``LLMRefusalError``

Exception messages carry only the provider name, the HTTP status and the
provider's error *type* and request id - never a response body, prompt text or key.

The HTTP client is injectable so tests replay recorded responses through
``httpx.MockTransport``; no test reaches a live provider.

Wire shapes (Anthropic): ``POST /v1/messages`` with ``x-api-key`` and
``anthropic-version: 2023-06-01``; reply ``content`` is a list of typed blocks,
``stop_reason`` is one of ``end_turn | max_tokens | stop_sequence | tool_use |
pause_turn | refusal``; errors are ``{"type": "error", "error": {"type",
"message"}, "request_id"}``. Source: the Anthropic ``claude-api`` reference bundled
with Claude Code, model table cached 2026-06-24, read 2026-10-20 - not re-checked
against the live API. OpenAI shapes are from the Chat Completions reference as
previously documented and are likewise unverified against the live API.
"""

from __future__ import annotations

import json
import re
import string
import time
from typing import Any

import httpx

from app.llm.base import (
    LLMMalformedError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUsage,
)
from app.llm.prompts import get_prompt

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = (
    "You are SignalNest's analysis engine. Reply with exactly one JSON object and "
    "nothing else: no prose, no markdown."
)

#: Anthropic models that still accept ``temperature``. Opus 4.7 and later, the
#: Fable family and Sonnet 5 reject sampling parameters with HTTP 400, so for any
#: model not listed here the field is omitted rather than risk a hard failure.
_ANTHROPIC_SAMPLING_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-opus-4-6",
    "claude-opus-4-5",
)

_RETRYABLE_STATUS = frozenset({408, 409})
_FENCED_JSON = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.DOTALL)
_ERROR_TYPE = re.compile(r"[a-z_]{1,64}")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _render(request: LLMRequest) -> str:
    prompt = get_prompt(request.task)
    fields = [f[1] for f in string.Formatter().parse(prompt.template) if f[1]]
    body = prompt.template.format(**{k: _as_text(request.inputs.get(k)) for k in fields})
    return body + "\nRespond with strict minified JSON only."


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    return str(value)


def anthropic_accepts_temperature(model: str) -> bool:
    return model.startswith(_ANTHROPIC_SAMPLING_MODELS)


def _error_detail(resp: httpx.Response) -> str:
    """``" type=<error type> request_id=<id>"`` from a provider error, both optional.

    Only values matching a strict shape are echoed, so a hostile or garbled body
    cannot inject text into logs through an exception message.
    """
    error_type = None
    request_id = resp.headers.get("request-id")
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("type"), str):
            error_type = err["type"]
        if isinstance(data.get("request_id"), str):
            request_id = data["request_id"]
    parts = []
    if error_type and _ERROR_TYPE.fullmatch(error_type):
        parts.append(f"type={error_type}")
    if request_id and _REQUEST_ID.fullmatch(request_id):
        parts.append(f"request_id={request_id}")
    return "".join(f" {p}" for p in parts)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None  # HTTP-date form: fall back to the service's own backoff
    if seconds != seconds or seconds < 0:  # NaN or negative
        return None
    return seconds


def _raise_for_status(resp: httpx.Response, provider: str) -> None:
    status = resp.status_code
    if 200 <= status < 300:
        return
    detail = _error_detail(resp)
    if status == 429:
        raise LLMRateLimitError(
            f"{provider} rate limited (HTTP 429{detail})", retry_after=_retry_after(resp)
        )
    if status in _RETRYABLE_STATUS or status >= 500:
        raise LLMUnavailableError(f"{provider} unavailable (HTTP {status}{detail})")
    raise LLMProviderError(f"{provider} rejected the request (HTTP {status}{detail})")


def _json_body(resp: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMMalformedError(f"{provider} returned a non-JSON body") from exc
    if not isinstance(data, dict):
        raise LLMMalformedError(f"{provider} returned an unexpected body shape")
    return data


def _json_object(text: str, provider: str) -> dict[str, Any]:
    """Parse the model's text as exactly one JSON object.

    A single surrounding markdown code fence is tolerated; anything else that is
    not a JSON object (prose, a list, a truncated document) is malformed.
    """
    candidate = text.strip()
    fenced = _FENCED_JSON.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LLMMalformedError(f"{provider} returned non-JSON text") from exc
    if not isinstance(value, dict):
        raise LLMMalformedError(f"{provider} returned JSON that is not an object")
    return value


def _token_count(usage: Any, key: str) -> int:
    value = usage.get(key) if isinstance(usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


class _HTTPProvider:
    name = ""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: int,
        *,
        client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def _post(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
        try:
            resp = self._client.post(url, headers=headers, json=body, timeout=self.timeout)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"{self.name} request timed out") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(
                f"{self.name} connection failed ({type(exc).__name__})"
            ) from exc
        _raise_for_status(resp, self.name)
        return resp

    def _response(
        self,
        request: LLMRequest,
        output: dict[str, Any],
        usage: LLMUsage,
        started: float,
        prompt_name: str,
        prompt_version: str,
    ) -> LLMResponse:
        return LLMResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            output=output,
            usage=usage,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            status="ok",
            is_simulated=False,
        )


class OpenAIProvider(_HTTPProvider):
    name = "openai"

    def __init__(
        self, api_key: str, model: str, timeout: int, *, client: httpx.Client | None = None
    ):
        super().__init__(api_key, model or "gpt-4o-mini", timeout, client=client)

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        started = time.perf_counter()
        resp = self._post(
            OPENAI_CHAT_URL,
            {"Authorization": f"Bearer {self.api_key}"},
            {
                "model": self.model,
                "temperature": request.temperature,
                "max_completion_tokens": request.max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _render(request)},
                ],
            },
        )
        data = _json_body(resp, self.name)
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise LLMMalformedError("openai returned no message")
        finish = choice.get("finish_reason")
        if isinstance(message.get("refusal"), str) or finish == "content_filter":
            raise LLMRefusalError("openai refused the request")
        if finish == "length":
            raise LLMMalformedError("openai output truncated at the token limit")
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMMalformedError("openai returned no text content")
        usage = data.get("usage")
        return self._response(
            request,
            _json_object(content, self.name),
            LLMUsage(
                input_tokens=_token_count(usage, "prompt_tokens"),
                output_tokens=_token_count(usage, "completion_tokens"),
            ),
            started,
            prompt_name,
            prompt_version,
        )


class AnthropicProvider(_HTTPProvider):
    name = "anthropic"

    def __init__(
        self, api_key: str, model: str, timeout: int, *, client: httpx.Client | None = None
    ):
        super().__init__(api_key, model or "claude-sonnet-4-6", timeout, client=client)

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        started = time.perf_counter()
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _render(request)}],
        }
        if anthropic_accepts_temperature(self.model):
            body["temperature"] = request.temperature
        resp = self._post(
            ANTHROPIC_MESSAGES_URL,
            {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION},
            body,
        )
        data = _json_body(resp, self.name)
        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal":
            raise LLMRefusalError("anthropic refused the request")
        if stop_reason == "max_tokens":
            raise LLMMalformedError("anthropic output truncated at max_tokens")
        if stop_reason not in ("end_turn", "stop_sequence"):
            raise LLMMalformedError("anthropic returned an unexpected stop_reason")
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise LLMMalformedError("anthropic returned no content blocks")
        texts = [
            b["text"]
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if not texts:
            raise LLMMalformedError("anthropic returned no text block")
        usage = data.get("usage")
        return self._response(
            request,
            _json_object("".join(texts), self.name),
            LLMUsage(
                input_tokens=_token_count(usage, "input_tokens"),
                output_tokens=_token_count(usage, "output_tokens"),
            ),
            started,
            prompt_name,
            prompt_version,
        )
