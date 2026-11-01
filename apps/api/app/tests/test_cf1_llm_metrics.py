"""Project Phase 5A LLM call metrics (W21 CF-1 amendment, coordinator decision): the contract.

The 5A seam lane emits these; CF-1 freezes the names, the label keys and the closed label
values so the lane cannot widen them.

* Four dedicated names, not ``dependency_operation_total``, so an LLM provider failure never
  joins the generic dependency-failure alert.
* Every label key is in ``ALLOWED_LABELS``; no tenant, user or free-text key is possible, and
  ``validate_metric`` refuses one.
* Closed values: ``dependency`` is a configured provider (``LLMProvider``); ``operation`` a
  task of the registry; ``outcome`` and ``error_class`` the frozen sets, and ``error_class``
  covers the whole ``app.llm`` error taxonomy (a new error class fails here until the
  vocabulary decides it).
* Units: the duration is milliseconds, the rest are counts.
"""

from __future__ import annotations

import typing

import pytest

from app.core.config import LLMProvider
from app.core.metrics import (
    ALLOWED_LABELS,
    DEPENDENCY_OPERATION_TOTAL,
    LLM_CALL_DURATION_MS,
    LLM_CALLS_TOTAL,
    LLM_INPUT_TOKENS_TOTAL,
    LLM_METRIC_ERROR_CLASSES,
    LLM_METRIC_LABELS,
    LLM_METRIC_OUTCOMES,
    LLM_OUTPUT_TOKENS_TOTAL,
    METRIC_NAMES,
    InMemoryMetrics,
    MetricError,
    validate_metric,
)
from app.core.metrics_emf import metric_unit
from app.llm.base import (
    LLMError,
    LLMMalformedError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.llm.metering import LLMBudgetExceededError
from app.llm.schemas import TASK_SCHEMAS

LLM_METRICS = (LLM_CALLS_TOTAL, LLM_CALL_DURATION_MS, LLM_INPUT_TOKENS_TOTAL,
               LLM_OUTPUT_TOKENS_TOTAL)

#: The error-class code of each class of the app.llm error taxonomy.
TAXONOMY = {
    LLMTimeoutError: "timeout",
    LLMRateLimitError: "rate_limit",
    LLMRefusalError: "refusal",
    LLMMalformedError: "malformed",
    LLMUnavailableError: "unavailable",
    LLMProviderError: "provider_error",
    LLMBudgetExceededError: "budget_exceeded",
}


def _subclasses(cls: type) -> set[type]:
    found = set()
    for sub in cls.__subclasses__():
        found |= {sub, *_subclasses(sub)}
    return found


def test_the_names_are_dedicated_and_catalogued() -> None:
    assert {
        "llm_calls_total", "llm_call_duration_ms", "llm_input_tokens_total",
        "llm_output_tokens_total",
    } == set(LLM_METRICS)
    assert set(LLM_METRICS) <= METRIC_NAMES
    assert DEPENDENCY_OPERATION_TOTAL not in LLM_METRICS
    assert set(LLM_METRIC_LABELS) == set(LLM_METRICS)


def test_label_keys_are_exact_bounded_and_tenant_free() -> None:
    assert LLM_METRIC_LABELS == {
        LLM_CALLS_TOTAL: {"dependency", "operation", "outcome", "error_class"},
        LLM_CALL_DURATION_MS: {"dependency", "operation", "outcome"},
        LLM_INPUT_TOKENS_TOTAL: {"dependency", "operation"},
        LLM_OUTPUT_TOKENS_TOTAL: {"dependency", "operation"},
    }
    for keys in LLM_METRIC_LABELS.values():
        assert keys <= ALLOWED_LABELS
        assert not {k for k in keys if "id" in k.split("_") or k in {"tenant", "user"}}


@pytest.mark.parametrize("name", LLM_METRICS)
def test_validation_accepts_the_declared_labels_and_refuses_a_tenant_label(name: str) -> None:
    labels = {
        "dependency": "mock", "operation": "classify_signal", "outcome": "failure",
        "error_class": "timeout",
    }
    declared = {k: v for k, v in labels.items() if k in LLM_METRIC_LABELS[name]}
    validate_metric(name, declared)
    for leak in ("workspace_id", "organization_id", "user_id", "prompt", "model"):
        with pytest.raises(MetricError):
            validate_metric(name, {**declared, leak: "x"})


def test_closed_label_values() -> None:
    assert LLM_METRIC_OUTCOMES == {"success", "failure"}
    assert set(typing.get_args(LLMProvider)) == {"mock", "openai", "anthropic"}
    assert TASK_SCHEMAS  # operation = a key of the task registry
    assert LLM_METRIC_ERROR_CLASSES == {*TAXONOMY.values(), "none", "other"}


def test_error_class_covers_the_whole_llm_error_taxonomy() -> None:
    """A new LLMError subclass fails here until the vocabulary is decided for it."""
    assert _subclasses(LLMError) == set(TAXONOMY)


def test_units_and_an_in_memory_emission() -> None:
    assert metric_unit(LLM_CALL_DURATION_MS) == "Milliseconds"
    for name in (LLM_CALLS_TOTAL, LLM_INPUT_TOKENS_TOTAL, LLM_OUTPUT_TOKENS_TOTAL):
        assert metric_unit(name) == "Count"
    m = InMemoryMetrics()
    m.increment(
        LLM_CALLS_TOTAL, dependency="mock", operation="classify_signal", outcome="success",
        error_class="none",
    )
    m.observe(LLM_CALL_DURATION_MS, 12.0, dependency="mock", operation="classify_signal",
              outcome="success")
    m.increment(LLM_INPUT_TOKENS_TOTAL, amount=100, dependency="mock",
                operation="classify_signal")
    assert m.telemetry_failures == 0
