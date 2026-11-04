"""Provider-neutral LLM contracts.

Every provider (mock/openai/anthropic) implements the same ``LLMProvider`` protocol and
returns an ``LLMResponse`` with normalized metadata. Tasks are addressed by name and
each has a Pydantic output schema validated by the service layer.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class LLMError(Exception):
    """Base class for normalized provider errors.

    ``usage`` is set when the provider replied (and billed) but the reply was unusable
    - truncated, refused or not JSON - so cost accounting can charge it (P6-LLM-4).
    """

    usage: LLMUsage | None = None
    #: True when the request may have reached the provider but no reply was seen (a
    #: connection failure after dispatch): the outcome is unknown, so cost accounting
    #: charges the worst case rather than treating it as known-free.
    outcome_unknown: bool = False


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    """HTTP 429. ``retry_after`` is the provider's ``retry-after`` in seconds, if sent."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMRefusalError(LLMError):
    pass


class LLMMalformedError(LLMError):
    pass


class LLMProviderError(LLMError):
    """The provider rejected the request (bad key, unknown model, invalid request)."""


class LLMUnavailableError(LLMProviderError):
    """Transient provider-side failure (5xx, 529 overloaded, connection error)."""


class LLMPromptRenderError(RuntimeError):
    """The prompt could not be rendered from its template and inputs (Phase 5A).

    Raised while the request is built, before anything is sent, reserved or charged: a
    template field with no input key, or a template the formatter cannot fill. Rendering is
    fail-closed - there is no fallback that sends an unrendered or partly rendered
    template.

    A programming error (a call site and its template disagree), not an LLM outcome: like
    :class:`LLMTenantError` it is not an ``LLMError``, so it is never retried, never falls
    back to the mock and is never absorbed as a degraded result. Nor is it a
    ``ValueError``, which the metered path's wire-size probe would read as "cost cannot be
    bounded" and misreport as a budget refusal.
    """


class LLMTenantError(RuntimeError):
    """A model call named no tenant, or a tenant other than its meter's (Phase 5A).

    A programming error, not an LLM outcome: like ``LLMMeterBindingError`` it is not an
    ``LLMError``, so no LLM failure handling (degraded marking, dev fallback) absorbs it.
    """


class LLMRequest(BaseModel):
    task: str
    inputs: dict[str, Any]
    seed: str | None = None
    temperature: float = 0.0
    max_tokens: int = 800
    #: Opaque per-tenant reference sent to the provider (never a raw id; P6-LLM-4).
    tenant_ref: str | None = None
    #: Cap per untrusted prompt value, in characters (P6-LLM-2).
    max_untrusted_chars: int = 4000


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class LLMResponse(BaseModel):
    task: str
    provider: str
    model: str
    output: dict[str, Any]
    usage: LLMUsage = Field(default_factory=LLMUsage)
    latency_ms: float = 0.0
    prompt_name: str = ""
    prompt_version: str = ""
    input_hash: str = ""
    status: str = "ok"  # ok | low_confidence | refused | error
    is_simulated: bool = False
    trace_id: str | None = None
    #: Set by the service, never by a provider: the usage-ledger row
    #: (``llm_usage_events.id``) of the metered attempt that produced this response, or
    #: ``None`` when no meter recorded it (an unmetered call, the dev fallback).
    usage_event_id: str | None = None


class LLMProvider(Protocol):
    name: str

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        ...
