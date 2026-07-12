"""Provider-neutral LLM contracts.

Every provider (mock/openai/anthropic) implements the same ``LLMProvider`` protocol and
returns an ``LLMResponse`` with normalized metadata. Tasks are addressed by name and
each has a Pydantic output schema validated by the service layer.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class LLMError(Exception):
    """Base class for normalized provider errors."""


class LLMTimeoutError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMRefusalError(LLMError):
    pass


class LLMMalformedError(LLMError):
    pass


class LLMProviderError(LLMError):
    pass


class LLMRequest(BaseModel):
    task: str
    inputs: dict[str, Any]
    seed: str | None = None
    temperature: float = 0.0
    max_tokens: int = 800


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
