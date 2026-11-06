"""Offline harness for the LLM test modules (P6-LLM-1/5). Not itself a test module.

``Settings`` reads ``LLM_*`` from the environment and from ``.env``, and the module-level
``app.llm.service.llm_service`` (what ``get_llm_service()`` returns to every call site) is
built from them at import. On a machine with a real
provider configured, a test that relied on either would send real, keyed requests.

* :func:`offline_settings` pins every ``llm_*`` field to its code default (overrides
  aside) and ignores ``.env``, so a test's LLM settings never come from the machine.
* :func:`offline_llm` swaps the process's service for the deterministic mock under
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
from app.llm import service as service_module
from app.llm.mock import MockLLMProvider
from app.llm.service import LLMService

#: The tenant an unmetered test call names (Phase 5A: every ``LLMService.run`` names one).
#: A metered call names its meter's tenant instead.
TEST_TENANT = {"organization_id": "org-llm-test", "workspace_id": "ws-llm-test"}


def tenant_of(meter: Any) -> dict[str, str]:
    """The tenant a metered test call names: its bound meter's."""
    return {"organization_id": meter.organization_id, "workspace_id": meter.workspace_id}


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
    from app.llm.prompts import get_prompt, template_fields
    from app.llm.providers_real import AnthropicProvider

    # Every template field present (rendering is fail-closed, Phase 5A); None renders the
    # same empty block an absent field used to.
    inputs = {name: None for name in template_fields(get_prompt(task).template)}
    inputs["content"] = content
    request = LLMRequest(task=task, inputs=inputs, max_tokens=max_tokens, tenant_ref=tenant_ref)
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
        mp.setattr(service_module, "llm_service", LLMService(provider, settings=settings))
        yield settings
