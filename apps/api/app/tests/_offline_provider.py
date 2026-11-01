"""Offline LLM provider pin for the API test session (FU-237-F1, part 2). Not a test module.

``Settings`` reads ``LLM_*`` from the environment (and ``.env``), and ``app.llm.service``
builds ``llm_service`` from them at *import*; every call site reaches that object through
``get_llm_service()`` (Phase 5A; ``pipeline`` used to bind it by name). On a
machine with a real provider configured, any test that reaches the pipeline would send real,
keyed requests. This module (1) removes provider selection and credentials from
``os.environ`` for the test process, before the application is imported, (2) forces the
mock provider and verifies the resolved settings, and (3) swaps the already-built service
objects for mock-backed ones. The socket guard (``_offline_guard``) remains the backstop.

Opt-out: ``@pytest.mark.allow_real_provider``, honoured only when
``SIGNALNEST_TESTS_ALLOW_REAL_PROVIDER=1``; no test in the repository may carry it
(governance test in ``test_offline_guard.py``). The pin itself is always applied; a marked
test gets the original environment and a freshly built service for its duration.
"""

from __future__ import annotations

import os
from typing import Any

ALLOW_ENV = "SIGNALNEST_TESTS_ALLOW_REAL_PROVIDER"
MARKER = "allow_real_provider"
#: Environment names removed from the test process (prefix match, upper-case).
_SCRUB_PREFIXES = ("LLM_", "ANTHROPIC_", "OPENAI_")
_removed: dict[str, str] = {}
_pinned: dict[str, Any] = {}


def scrub_environment() -> None:
    """Idempotent. Call before importing anything that reads settings."""
    for name in list(os.environ):
        if name.upper().startswith(_SCRUB_PREFIXES):
            _removed[name] = os.environ.pop(name)
    os.environ["LLM_PROVIDER"] = "mock"


def verify_and_swap() -> None:
    """Rebuild cached settings, verify they are offline, and swap import-time services."""
    from app.core.config import get_settings
    from app.llm import service as service_module
    from app.llm.mock import MockLLMProvider
    from app.llm.service import LLMService
    from app.tests._llm_offline import offline_settings

    get_settings.cache_clear()
    live = get_settings()
    if live.llm_provider != "mock" or live.llm_api_key:
        raise RuntimeError(
            "offline provider pin failed: resolved settings still select "
            f"llm_provider={live.llm_provider!r} (a .env file in the working directory?)"
        )
    if "service" not in _pinned:  # one mock object for the whole session (idempotent)
        settings = offline_settings()
        _pinned["service"] = LLMService(
            MockLLMProvider(model=settings.llm_model or "mock-1", seed=settings.llm_mock_seed),
            settings=settings,
        )
    service_module.llm_service = _pinned["service"]


class RealProviderWindow:
    """For a marked test only: original env + fresh service; restored on exit."""

    def __init__(self) -> None:
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> RealProviderWindow:
        from app.core.config import get_settings
        from app.llm import service as service_module

        self._saved = {
            "env": {k: os.environ.get(k) for k in [*_removed, "LLM_PROVIDER"]},
            "module": service_module.llm_service,
        }
        os.environ.update(_removed)
        get_settings.cache_clear()
        service_module.llm_service = service_module.LLMService()
        return self

    def __exit__(self, *exc: object) -> None:
        from app.core.config import get_settings
        from app.llm import service as service_module

        for k, v in self._saved["env"].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()
        service_module.llm_service = self._saved["module"]
