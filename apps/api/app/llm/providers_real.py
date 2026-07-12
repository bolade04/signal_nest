"""Real OpenAI/Anthropic adapters.

Wired behind env vars and sharing the exact ``LLMResponse`` contract as the mock. They
request strict JSON and normalize provider-specific errors. Not exercised in local mode
(mock is default); provided so a real provider can be enabled without touching callers.
"""

from __future__ import annotations

import json
import time

import httpx

from app.llm.base import (
    LLMMalformedError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUsage,
)
from app.llm.prompts import get_prompt


def _render(request: LLMRequest) -> str:
    prompt = get_prompt(request.task)
    try:
        body = prompt.template.format(
            **{k: request.inputs.get(k, "") for k in _fields(prompt.template)}
        )
    except Exception:
        body = prompt.template
    return body + "\nRespond with strict minified JSON only."


def _fields(template: str) -> list[str]:
    import string

    return [f[1] for f in string.Formatter().parse(template) if f[1]]


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: int):
        self.api_key = api_key
        self.model = model or "gpt-4o-mini"
        self.timeout = timeout

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        started = time.perf_counter()
        try:
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": request.temperature,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "You are SignalNest. Return strict JSON."},
                        {"role": "user", "content": _render(request)},
                    ],
                },
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:  # pragma: no cover - network
            raise LLMTimeoutError(str(exc)) from exc
        if resp.status_code == 429:  # pragma: no cover - network
            raise LLMRateLimitError("openai rate limit")
        if resp.status_code >= 400:  # pragma: no cover - network
            raise LLMProviderError(f"openai error {resp.status_code}")
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
            output = json.loads(content)
        except (KeyError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise LLMMalformedError("openai returned non-JSON") from exc
        usage = data.get("usage", {})
        return LLMResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            output=output,
            usage=LLMUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            status="ok",
            is_simulated=False,
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: int):
        self.api_key = api_key
        self.model = model or "claude-sonnet-4-6"
        self.timeout = timeout

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        started = time.perf_counter()
        try:
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.model,
                    "max_tokens": request.max_tokens,
                    "temperature": request.temperature,
                    "messages": [{"role": "user", "content": _render(request)}],
                },
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:  # pragma: no cover - network
            raise LLMTimeoutError(str(exc)) from exc
        if resp.status_code == 429:  # pragma: no cover - network
            raise LLMRateLimitError("anthropic rate limit")
        if resp.status_code >= 400:  # pragma: no cover - network
            raise LLMProviderError(f"anthropic error {resp.status_code}")
        data = resp.json()
        try:
            text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
            output = json.loads(text)
        except (KeyError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise LLMMalformedError("anthropic returned non-JSON") from exc
        usage = data.get("usage", {})
        return LLMResponse(
            task=request.task,
            provider=self.name,
            model=self.model,
            output=output,
            usage=LLMUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            status="ok",
            is_simulated=False,
        )
