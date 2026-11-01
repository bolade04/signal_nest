"""LLM service: provider selection, retries/backoff, timeout, schema validation, metadata.

Callers use ``get_llm_service().run(task, inputs, organization_id=..., workspace_id=...)``
and receive a validated Pydantic model and the response metadata.
The service never silently falls back from a real provider to mock unless
``LLM_ALLOW_DEV_FALLBACK`` is explicitly enabled (and never in staging/production).

Retries are bounded: at most ``llm_max_retries + 1`` attempts, and only for transient
failures (timeout, 429, 5xx/overloaded, connection). Between attempts the service
waits ``min(0.1 * 2**attempt, 2.0)`` seconds, or the provider's ``retry-after``
clamped to ``llm_retry_after_cap_seconds``; it never waits after the last attempt.
Permanent failures (rejected request, refusal, malformed or schema-invalid output)
are not retried, so a bad reply is never paid for twice.

With a :class:`~app.llm.metering.LLMMeter` (P6-LLM-4) every attempt is first checked
against the run cap and the workspace budget - a disallowed attempt is not made and
:class:`~app.llm.metering.LLMBudgetExceededError` propagates (never to the dev
fallback) - and then charged: a reply by its reported usage (even one that later
fails schema validation), a timeout by its worst case, a refused HTTP call nothing.
The meter's opaque tenant reference is sent to the provider.

Phase 5A seam (always live, un-flagged; disclosed in the Phase 5 plan section 9):

* **Tenant on every call.** ``run`` requires ``organization_id`` and ``workspace_id``.
  A missing tenant, or one that differs from the bound meter's, raises
  :class:`~app.llm.base.LLMTenantError` before anything is sent. An unmetered call
  derives the opaque tenant reference from the workspace, so every request carries one.
* **Sanitized at the boundary.** Every string input, at any depth, is defanged
  (:func:`app.llm.untrusted.sanitize_inputs`) before any provider - the mock included -
  sees it. The real adapters' prompt blocks remain the stronger layer. What this changes,
  including in mock mode, is stated exactly in :mod:`app.llm.untrusted`.
* **``input_hash`` computed here**, never by a provider: SHA-256 of the canonical JSON of
  the task, the prompt name and version and the sanitized inputs
  (:func:`compute_input_hash`). Every response, the dev fallback's included, carries it.
* **Fail-closed rendering.** A prompt that cannot be rendered raises
  :class:`~app.llm.base.LLMPromptRenderError` (a programming error, not an ``LLMError``)
  while the request is built: on the metered path before the attempt is reserved, and in
  every case before anything is sent. It is never retried and never falls back to the
  mock.
* **Usage link.** A metered response carries ``usage_event_id``, the usage-ledger row of
  the attempt that produced it (Phase 5A brief provenance).
* **Injectable clock.** ``sleep`` (the retry wait) is injectable, so retry tests wait for
  nothing and can assert every back-off.
* :func:`get_llm_service` is the accessor call sites use; the module-level
  ``llm_service`` stays the default instance it returns.
* **Metrics** (names and closed label values frozen at the W21 CF-1 amendment,
  :mod:`app.core.metrics`): every provider attempt counts ``llm_calls_total`` (``outcome``
  success|failure, ``error_class`` none or its class in the ``app.llm`` taxonomy) and
  observes ``llm_call_duration_ms``; a reply's usage adds to ``llm_input_tokens_total`` /
  ``llm_output_tokens_total``; an attempt the meter refuses counts as a failure
  ``budget_exceeded`` with no duration. Labels are the provider (``dependency``) and the task
  (``operation``) only - never a tenant, request or free-text value; a provider or task
  outside the closed vocabularies (a test double) emits nothing.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, get_args

from pydantic import BaseModel, ValidationError

from app.core.config import LLMProvider as ConfiguredProvider
from app.core.config import Settings, get_settings
from app.core.logging import get_logger, trace_id_ctx
from app.core.metrics import (
    LLM_CALL_DURATION_MS,
    LLM_CALLS_TOTAL,
    LLM_INPUT_TOKENS_TOTAL,
    LLM_OUTPUT_TOKENS_TOTAL,
    get_metrics,
)
from app.llm.base import (
    LLMError,
    LLMMalformedError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequest,
    LLMResponse,
    LLMTenantError,
    LLMTimeoutError,
    LLMUnavailableError,
    LLMUsage,
)
from app.llm.metering import LLMBudgetExceededError, cost_usd, tenant_reference
from app.llm.mock import MockLLMProvider
from app.llm.prompts import Prompt, get_prompt
from app.llm.schemas import TASK_SCHEMAS
from app.llm.untrusted import sanitize_inputs

if TYPE_CHECKING:
    from app.llm.metering import LLMMeter

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


def compute_input_hash(task: str, prompt: Prompt, inputs: dict[str, Any]) -> str:
    """SHA-256 hex of what the model is asked: task, prompt identity and inputs.

    Canonical JSON (sorted keys, no whitespace, UTF-8); a value JSON cannot represent is
    hashed by its ``str()``. Computed by the service from the sanitized inputs, so it does
    not depend on a provider remembering to set it.
    """
    payload = {
        "task": task,
        "prompt": f"{prompt.name}@{prompt.version}",
        "inputs": inputs,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: ``dependency`` label values: the configured providers (CF-1 closed vocabulary).
_METRIC_DEPENDENCIES = frozenset(get_args(ConfiguredProvider))
#: ``error_class`` label of each ``app.llm`` error class, most specific first (CF-1 closed
#: vocabulary, ``app.core.metrics.LLM_METRIC_ERROR_CLASSES``); anything else is "other".
_METRIC_ERROR_CLASSES: tuple[tuple[type[Exception], str], ...] = (
    (LLMBudgetExceededError, "budget_exceeded"),
    (LLMTimeoutError, "timeout"),
    (LLMRateLimitError, "rate_limit"),
    (LLMRefusalError, "refusal"),
    (LLMMalformedError, "malformed"),
    (LLMUnavailableError, "unavailable"),
    (LLMProviderError, "provider_error"),
)


def _metric_error_class(error: Exception | None) -> str:
    if error is None:
        return "none"
    for cls, code in _METRIC_ERROR_CLASSES:
        if isinstance(error, cls):
            return code
    return "other"


def _record_call(
    provider: str,
    task: str,
    *,
    error: Exception | None = None,
    duration_ms: float | None = None,
    usage: LLMUsage | None = None,
) -> None:
    """Count one model attempt (closed labels only; see the module docstring)."""
    if provider not in _METRIC_DEPENDENCIES or task not in TASK_SCHEMAS:
        return  # never emit a label value outside the frozen vocabularies
    metrics = get_metrics()
    outcome = "success" if error is None else "failure"
    metrics.increment(
        LLM_CALLS_TOTAL,
        dependency=provider,
        operation=task,
        outcome=outcome,
        error_class=_metric_error_class(error),
    )
    if duration_ms is not None:
        metrics.observe(
            LLM_CALL_DURATION_MS, duration_ms, dependency=provider, operation=task,
            outcome=outcome,
        )
    if usage is not None:
        if usage.input_tokens > 0:
            metrics.increment(LLM_INPUT_TOKENS_TOTAL, amount=usage.input_tokens,
                              dependency=provider, operation=task)
        if usage.output_tokens > 0:
            metrics.increment(LLM_OUTPUT_TOKENS_TOTAL, amount=usage.output_tokens,
                              dependency=provider, operation=task)


def _require_tenant(
    organization_id: str, workspace_id: str, meter: LLMMeter | None
) -> None:
    """Refuse a call that names no tenant, or a tenant other than its meter's."""
    for name, value in (("organization_id", organization_id), ("workspace_id", workspace_id)):
        if not isinstance(value, str) or not value:
            raise LLMTenantError(f"an LLM call needs a {name}")
    if meter is None:
        return
    if meter.workspace_id is None or meter.organization_id is None:
        raise LLMTenantError("an LLM call's meter must be bound to its tenant first")
    if (meter.organization_id, meter.workspace_id) != (organization_id, workspace_id):
        raise LLMTenantError("an LLM call's tenant differs from its meter's tenant")


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
        clock: Callable[[], float] = time.perf_counter,
    ):
        self._provider = provider or _build_provider()
        #: The injectable clock: ``sleep`` waits between attempts, ``clock`` (seconds,
        #: monotonic) times each attempt for ``llm_call_duration_ms``.
        self._sleep = sleep
        self._clock = clock
        self._settings = settings

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def run(
        self,
        task: str,
        inputs: dict,
        *,
        organization_id: str,
        workspace_id: str,
        meter: LLMMeter | None = None,
    ) -> tuple[BaseModel, LLMResponse]:
        """Run ``task`` for the tenant ``(organization_id, workspace_id)``.

        Raises :class:`LLMTenantError` (nothing sent) for a missing tenant or one that
        differs from the bound ``meter``'s; :class:`LLMPromptRenderError` (nothing sent,
        nothing charged) when the prompt cannot be rendered; otherwise the provider's
        normalized :class:`LLMError` after the bounded retries.
        """
        _require_tenant(organization_id, workspace_id, meter)
        settings = self._settings or get_settings()
        prompt = get_prompt(task)
        inputs = sanitize_inputs(inputs)
        input_hash = compute_input_hash(task, prompt, inputs)
        request = LLMRequest(
            task=task,
            inputs=inputs,
            seed=settings.llm_mock_seed,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_output_tokens,
            tenant_ref=(
                meter.tenant_ref
                if meter is not None
                else tenant_reference(settings, workspace_id)
            ),
            max_untrusted_chars=settings.llm_max_untrusted_chars,
        )
        provider_name = self._provider.name
        model = getattr(self._provider, "model", "") or ""

        def charge(response: LLMResponse | None = None, error: Exception | None = None):
            if meter is not None:
                meter.after_attempt(
                    task=task,
                    provider=provider_name,
                    model=model,
                    response=response,
                    error=error,
                )

        attempts = settings.llm_max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            if meter is not None:
                try:
                    meter.before_attempt(provider_name, model, self._wire_bytes(request))
                except LLMBudgetExceededError as exc:
                    _record_call(provider_name, task, error=exc)  # not made: no duration
                    raise
            started = self._clock()
            try:
                response = self._provider.generate(request, prompt.name, prompt.version)
            except LLMError as exc:
                _record_call(
                    provider_name, task, error=exc,
                    duration_ms=(self._clock() - started) * 1000,
                    usage=getattr(exc, "usage", None),
                )
                charge(error=exc)
                last_error = exc
                # A permanent failure (rejected, refused, malformed) is never retried.
                if not isinstance(exc, _TRANSIENT) or attempt + 1 >= attempts:
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
                continue
            duration_ms = (self._clock() - started) * 1000
            response.usage.estimated_cost_usd = cost_usd(
                settings,
                provider_name,
                model,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )
            charge(response=response)
            response.trace_id = trace_id_ctx.get()
            response.input_hash = input_hash
            response.usage_event_id = meter.last_usage_event_id if meter is not None else None
            try:
                validated = self._validate(task, response)
            except LLMMalformedError as exc:
                _record_call(provider_name, task, error=exc, duration_ms=duration_ms,
                             usage=response.usage)
                last_error = exc
                break
            _record_call(provider_name, task, duration_ms=duration_ms, usage=response.usage)
            self._log(response, attempt)
            return validated, response

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
            response.trace_id = trace_id_ctx.get()
            response.input_hash = input_hash
            return self._validate(task, response), response

        raise last_error or LLMError("LLM call failed")

    def _wire_bytes(self, request: LLMRequest) -> bytes | None:
        """The request body exactly as the provider's transport will send it, for the
        budget bound; ``None`` when the provider cannot show it (unbounded, so refused)."""
        prepare = getattr(self._provider, "prepare", None)
        if prepare is None:
            return None
        try:
            return prepare(request).content
        except (TypeError, ValueError):
            return None  # not encodable as the body the transport would send

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


def get_llm_service() -> LLMService:
    """The process's LLM service: the module-level ``llm_service`` at call time.

    Call sites use this rather than binding the singleton at import, so the instance
    the process (or a test) installs as ``app.llm.service.llm_service`` is the one every
    call site uses. It is also a FastAPI dependency (``Depends(get_llm_service)``).
    """
    return llm_service
