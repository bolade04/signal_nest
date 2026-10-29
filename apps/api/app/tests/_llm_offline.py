"""Offline harness for the LLM test modules (P6-LLM-1/5). Not itself a test module.

``Settings`` reads ``LLM_*`` from the environment and from ``.env``, and the pipeline's
module-level ``llm_service`` is built from them at import. On a machine with a real
provider configured, a test that relied on either would send real, keyed requests.

* :func:`offline_settings` pins every ``llm_*`` field to its code default (overrides
  aside) and ignores ``.env``, so a test's LLM settings never come from the machine.
* :func:`offline_llm` swaps the pipeline's service for the deterministic mock under
  those settings, and makes httpx's real network transport raise: a request that no
  ``httpx.MockTransport`` (or Starlette test transport) serves fails the test loudly.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.jobs import pipeline
from app.llm.mock import MockLLMProvider
from app.llm.service import LLMService


def wire_payload(
    model: str = "claude-sonnet-4-6",
    *,
    content: str = "x",
    task: str = "classify_signal",
    max_tokens: int = 1024,
    tenant_ref: str | None = None,
) -> bytes:
    """The body bytes the Anthropic adapter would send for ``content`` (nothing is sent)."""
    from app.llm.base import LLMRequest
    from app.llm.providers_real import AnthropicProvider

    request = LLMRequest(
        task=task, inputs={"content": content}, max_tokens=max_tokens, tenant_ref=tenant_ref
    )
    return AnthropicProvider("fixture-key", model, 5).prepare(request).content


def offline_settings(**overrides: Any) -> Settings:
    llm_defaults = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
        if name.startswith("llm_")
    }
    return Settings(_env_file=None, **{**llm_defaults, **overrides})


def _refuse_real_http(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"real HTTP request in an offline LLM test: {request.url.host}")


@contextmanager
def offline_llm() -> Iterator[Settings]:
    settings = offline_settings()
    provider = MockLLMProvider(model=settings.llm_model or "mock-1", seed=settings.llm_mock_seed)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx.HTTPTransport, "handle_request", _refuse_real_http)
        mp.setattr(pipeline, "llm_service", LLMService(provider, settings=settings))
        yield settings
