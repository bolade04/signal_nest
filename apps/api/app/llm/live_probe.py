"""E6 live-provider probe: run the real adapter's branches against the real provider once.

Phase 6 demonstration E6 needs a recorded execution in which a real LLM provider
served SignalNest's adapter, including its failure branches. This module is the
capture tool for that run. It is **never** run by tests or CI (tests drive it through
``httpx.MockTransport``), and it refuses to start without ``--confirm-live-provider-call``:
running it spends money on the configured provider account and needs an operator-
approved packet.

It is shipped in the image so it can run where the provider key is injected (the
staging task) without anyone reading the key::

    python -m app.llm.live_probe --confirm-live-provider-call

Each step prints one JSON line (``{"event": "e6_probe", ...}``) to stdout. Inputs are
fixed synthetic text, never tenant data; output carries no key, no prompt and no
model text - only classes, statuses, token counts, latency and the validated keys.

Steps (at most four billable requests, retries disabled, ``max_tokens`` <= 1024):

* ``success_classify`` / ``success_explain`` - real calls through ``LLMService``.
* ``truncated`` - ``max_tokens=4`` forces ``stop_reason: max_tokens`` -> malformed.
* ``timeout`` - a 1 ms client timeout -> ``LLMTimeoutError`` (no reply is generated
  or, if the request reached the provider, it is abandoned).
* ``rejected_model`` - an unknown model id -> 404 -> ``LLMProviderError`` (not billed).
* ``rate_limit_429`` / ``server_error_5xx`` - **not induced**: provoking them would
  mean flooding or waiting for an outage. They are reported ``not_induced`` and stay
  covered by recorded replay (``app/tests/test_llm_provider_replay.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.llm.base import LLMError, LLMRequest
from app.llm.prompts import get_prompt
from app.llm.providers_real import AnthropicProvider, OpenAIProvider
from app.llm.service import LLMService

CONFIRM_FLAG = "--confirm-live-provider-call"
REJECTED_MODEL = "signalnest-e6-nonexistent-model"
SYNTHETIC_CLASSIFY = {
    "content": "Synthetic E6 probe text: the morning queue at the coffee shop is far too slow.",
    "audiences": ["commuters buying coffee before work"],
}
SYNTHETIC_EXPLAIN = {
    "resolved_market": "Synthetic Market",
    "audience_fit": "commuters buying coffee before work",
    "pain_point_dna": "speed_complaint",
    "safe_positioning": "Lead with proven convenience.",
}
REPLAY_COVERAGE = "app/tests/test_llm_provider_replay.py"

Emit = Callable[[dict[str, Any]], None]
ClientFactory = Callable[[float], httpx.Client]


def _emit_stdout(record: dict[str, Any]) -> None:
    print(json.dumps({"event": "e6_probe", **record}, sort_keys=True), flush=True)


def _provider(settings: Settings, model: str, timeout: float, client: httpx.Client):
    cls = AnthropicProvider if settings.llm_provider == "anthropic" else OpenAIProvider
    return cls(settings.llm_api_key or "", model, timeout, client=client)


def run_probe(
    settings: Settings,
    *,
    client_factory: ClientFactory = lambda timeout: httpx.Client(timeout=timeout),
    emit: Emit = _emit_stdout,
) -> int:
    """Run every step; return 0 when each observed outcome is the expected one."""
    if settings.llm_provider not in ("anthropic", "openai") or not settings.llm_api_key:
        emit({"step": "preflight", "outcome": "refused", "reason": "no real provider configured"})
        return 2
    model = settings.llm_model or ""
    no_retry = settings.model_copy(update={"llm_max_retries": 0})
    failures = 0

    def record(step: str, expected: str, fn: Callable[[], dict[str, Any]]) -> None:
        nonlocal failures
        try:
            observed = {"outcome": "ok", **fn()}
        except LLMError as exc:
            observed = {"outcome": type(exc).__name__, "detail": str(exc)}
        ok = observed["outcome"] == expected
        failures += 0 if ok else 1
        emit({"step": step, "expected": expected, "as_expected": ok, **observed})

    def via_service(task: str, inputs: dict[str, Any]) -> dict[str, Any]:
        timeout = settings.llm_timeout_seconds
        provider = _provider(settings, model, timeout, client_factory(timeout))
        validated, response = LLMService(provider, settings=no_retry).run(task, inputs)
        return {
            "provider": response.provider,
            "model": response.model,
            "is_simulated": response.is_simulated,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "latency_ms": response.latency_ms,
            "validated_keys": sorted(validated.model_dump()),
        }

    def direct(timeout: float, max_tokens: int, model_id: str) -> dict[str, Any]:
        provider = _provider(settings, model_id, timeout, client_factory(timeout))
        prompt = get_prompt("classify_signal")
        request = LLMRequest(
            task="classify_signal", inputs=SYNTHETIC_CLASSIFY, max_tokens=max_tokens
        )
        response = provider.generate(request, prompt.name, prompt.version)
        return {"provider": response.provider, "output_tokens": response.usage.output_tokens}

    record("success_classify", "ok", lambda: via_service("classify_signal", SYNTHETIC_CLASSIFY))
    record("success_explain", "ok", lambda: via_service("explain_opportunity", SYNTHETIC_EXPLAIN))
    record("truncated", "LLMMalformedError", lambda: direct(settings.llm_timeout_seconds, 4, model))
    record("timeout", "LLMTimeoutError", lambda: direct(0.001, 64, model))
    record(
        "rejected_model",
        "LLMProviderError",
        lambda: direct(settings.llm_timeout_seconds, 16, REJECTED_MODEL),
    )
    for step in ("rate_limit_429", "server_error_5xx"):
        emit({"step": step, "outcome": "not_induced", "covered_by": REPLAY_COVERAGE})
    emit({"step": "summary", "unexpected": failures})
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    # allow_abbrev=False: a prefix such as ``--confirm`` must never count as confirmation.
    parser = argparse.ArgumentParser(
        prog="python -m app.llm.live_probe", description=__doc__, allow_abbrev=False
    )
    parser.add_argument(
        CONFIRM_FLAG,
        dest="confirmed",
        action="store_true",
        help="required: acknowledges that this spends money on the real provider",
    )
    args = parser.parse_args(argv)
    if not args.confirmed:
        print(f"refusing: {CONFIRM_FLAG} is required (this calls the real provider)",
              file=sys.stderr)
        return 2
    return run_probe(get_settings())


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
