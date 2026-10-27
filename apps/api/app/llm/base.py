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


class LLMProvider(Protocol):
    name: str

    def generate(self, request: LLMRequest, prompt_name: str, prompt_version: str) -> LLMResponse:
        ...
