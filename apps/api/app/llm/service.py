"""LLM service: provider selection, retries/backoff, timeout, schema validation, metadata.

Callers use ``llm_service.run(task, inputs)`` and receive a validated Pydantic model.
The service never silently falls back from a real provider to mock unless
``LLM_ALLOW_DEV_FALLBACK`` is explicitly enabled (and never in staging/production).

Retries are bounded: at most ``llm_max_retries + 1`` attempts, and only for transient
failures (timeout, 429, 5xx/overloaded, connection). Between attempts the service
waits ``min(0.1 * 2**attempt, 2.0)`` seconds, or the provider's ``retry-after``
clamped to ``llm_retry_after_cap_seconds``; it never waits after the last attempt.
Permanent failures (rejected request, refusal, malformed or schema-invalid output)
are not retried, so a bad reply is never paid for twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, trace_id_ctx
from app.llm.base import (
    LLMError,
    LLMMalformedError,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.mock import MockLLMProvider
from app.llm.prompts import get_prompt
from app.llm.schemas import TASK_SCHEMAS

logger = get_logger("signalnest.llm")

_TRANSIENT = (LLMTimeoutError, LLMRateLimitError, LLMUnavailableError)
_BACKOFF_BASE_SECONDS = 0.1
_BACKOFF_CAP_SECONDS = 2.0


def _build_provider() -> LLMProvider:
    s = get_settings()
    if s.llm_provider == "mock":
        return MockLLMProvider(model=s.llm_model or "mock-1", seed=s.llm_mock_seed)
    if s.llm_provider == "openai":
        from app.llm.providers_real import OpenAIProvider

        return OpenAIProvider(s.llm_api_key, s.llm_model or "", s.llm_timeout_seconds)
    from app.llm.providers_real import AnthropicProvider

    return AnthropicProvider(s.llm_api_key, s.llm_model or "", s.llm_timeout_seconds)


def _backoff_seconds(attempt: int, exc: Exception, retry_after_cap: float) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        return min(retry_after, retry_after_cap)
    return min(_BACKOFF_BASE_SECONDS * 2**attempt, _BACKOFF_CAP_SECONDS)


def _schema_errors(schema: type[BaseModel], exc: ValidationError) -> str:
    """``field: error-type`` per error, and nothing from the rejected value.

    pydantic's message quotes the input, which is model output and can echo tenant
    context from the prompt; it would reach ``llm.failed`` and the E6 probe's output.
    A location part that is not one of the schema's own field names is masked.
    """
    fields = schema.model_fields
    parts = []
    for err in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(p) if isinstance(p, int) or p in fields else "?" for p in err["loc"])
        parts.append(f"{loc or '<root>'}: {err['type']}")
    return "; ".join(parts)


class LLMService:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        settings: Settings | None = None,
    ):
        self._provider = provider or _build_provider()
        self._sleep = sleep
        self._settings = settings

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def run(self, task: str, inputs: dict) -> tuple[BaseModel, LLMResponse]:
        settings = self._settings or get_settings()
        prompt = get_prompt(task)
        request = LLMRequest(
            task=task,
            inputs=inputs,
            seed=settings.llm_mock_seed,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_output_tokens,
        )

        attempts = settings.llm_max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._provider.generate(request, prompt.name, prompt.version)
                response.trace_id = trace_id_ctx.get()
                validated = self._validate(task, response)
                self._log(response, attempt)
                return validated, response
            except _TRANSIENT as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                backoff = _backoff_seconds(attempt, exc, settings.llm_retry_after_cap_seconds)
                logger.warning(
                    "llm.retry",
                    extra={
                        "extra_fields": {
                            "task": task,
                            "attempt": attempt,
                            "error_class": type(exc).__name__,
                            "error": str(exc),
                            "backoff_s": backoff,
                        }
                    },
                )
                self._sleep(backoff)
            except LLMError as exc:
                last_error = exc
                break

        logger.warning(
            "llm.failed",
            extra={
                "extra_fields": {
                    "task": task,
                    "provider": self._provider.name,
                    "error_class": type(last_error).__name__,
                    "error": str(last_error),
                }
            },
        )

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
            # ``from None``: the ValidationError's own text quotes the rejected input.
            raise LLMMalformedError(
                f"Response failed schema validation: {_schema_errors(schema, exc)}"
            ) from None

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
