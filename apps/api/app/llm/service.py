"""LLM service: provider selection, retries/backoff, timeout, schema validation, metadata.

Callers use ``llm_service.run(task, inputs)`` and receive a validated Pydantic model.
The service never silently falls back from a real provider to mock unless
``LLM_ALLOW_DEV_FALLBACK`` is explicitly enabled (and never in staging/production).
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger, trace_id_ctx
from app.llm.base import (
    LLMError,
    LLMMalformedError,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
)
from app.llm.mock import MockLLMProvider
from app.llm.prompts import get_prompt
from app.llm.schemas import TASK_SCHEMAS

logger = get_logger("signalnest.llm")

_TRANSIENT = (LLMTimeoutError, LLMRateLimitError)


def _build_provider() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "mock":
        return MockLLMProvider(model=s.llm_model or "mock-1", seed=s.llm_mock_seed)
    if s.llm_provider == "openai":
        from app.llm.providers_real import OpenAIProvider

        return OpenAIProvider(s.llm_api_key, s.llm_model or "", s.llm_timeout_seconds)
    from app.llm.providers_real import AnthropicProvider

    return AnthropicProvider(s.llm_api_key, s.llm_model or "", s.llm_timeout_seconds)


class LLMService:
    def __init__(self, provider: LLMProvider | None = None):
        self._provider = provider or _build_provider()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def run(self, task: str, inputs: dict) -> tuple[BaseModel, LLMResponse]:
        settings = get_settings()
        prompt = get_prompt(task)
        request = LLMRequest(
            task=task,
            inputs=inputs,
            seed=settings.llm_mock_seed,
            temperature=settings.llm_temperature,
        )

        last_error: Exception | None = None
        for attempt in range(settings.llm_max_retries + 1):
            try:
                response = self._provider.generate(request, prompt.name, prompt.version)
                response.trace_id = trace_id_ctx.get()
                validated = self._validate(task, response)
                self._log(response, attempt)
                return validated, response
            except _TRANSIENT as exc:
                last_error = exc
                backoff = min(2**attempt * 0.1, 2.0)
                logger.warning(
                    "llm.retry",
                    extra={"extra_fields": {"task": task, "attempt": attempt, "error": str(exc)}},
                )
                time.sleep(backoff)
            except LLMError as exc:
                last_error = exc
                break

        # Explicit dev-only fallback (never enabled in staging/production by config validation).
        if settings.llm_allow_dev_fallback and not isinstance(self._provider, MockLLMProvider):
            logger.warning("llm.dev_fallback_to_mock", extra={"extra_fields": {"task": task}})
            mock = MockLLMProvider(seed=settings.llm_mock_seed)
            response = mock.generate(request, prompt.name, prompt.version)
            return self._validate(task, response), response

        raise last_error or LLMError("LLM call failed")

    def _validate(self, task: str, response: LLMResponse) -> BaseModel:
        schema = TASK_SCHEMAS.get(task)
        if schema is None:
            raise LLMMalformedError(f"No schema for task '{task}'")
        try:
            return schema.model_validate(response.output)
        except ValidationError as exc:
            raise LLMMalformedError(f"Response failed schema validation: {exc}") from exc

    def _log(self, response: LLMResponse, attempt: int) -> None:
        logger.info(
            "llm.call",
            extra={
                "extra_fields": {
                    "task": response.task,
                    "provider": response.provider,
                    "model": response.model,
                    "prompt": f"{response.prompt_name}@{response.prompt_version}",
                    "latency_ms": response.latency_ms,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "estimated_cost_usd": response.usage.estimated_cost_usd,
                    "status": response.status,
                    "input_hash": response.input_hash,
                    "is_simulated": response.is_simulated,
                    "attempt": attempt,
                }
            },
        )


llm_service = LLMService()
