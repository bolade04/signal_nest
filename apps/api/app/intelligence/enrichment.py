"""Provider-neutral enrichment boundary.

Enrichment is the step that turns raw signal text into structured facts +
inference. This module makes the *provider* a swappable seam, exactly like
``app.llm.service`` does for language models, with one crucial default:

* :class:`DeterministicEnricher` — the **default**. Pure, offline, no model call.
* :class:`ModelEnricher` — a disabled stub. It **raises** unless an explicit,
  non-default opt-in is set, and it is never selected in tests or by default. This
  guarantees no customer/source text is sent to an external model during normal
  operation or CI. Wiring a real model provider is a deliberate, separately-gated
  follow-up.

Selection is config-driven (``settings.intelligence_enricher``), so turning on a
model provider is an explicit, auditable configuration decision — never implicit.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.intelligence.extraction import extract_facts, extract_intelligence
from app.intelligence.models import AnalysisInput, ExtractedIntelligence, SignalFacts


@runtime_checkable
class Enricher(Protocol):
    """Turns an :class:`AnalysisInput` into (facts, inference). Deterministic."""

    name: str

    def enrich(self, signal: AnalysisInput) -> tuple[SignalFacts, ExtractedIntelligence]: ...


class DeterministicEnricher:
    """Default enricher: lexicon/regex extraction only. No network, no model."""

    name = "deterministic"

    def enrich(self, signal: AnalysisInput) -> tuple[SignalFacts, ExtractedIntelligence]:
        facts = extract_facts(
            signal.content,
            source_type=signal.source_type,
            market=signal.market,
            author=signal.author,
            language=signal.language,
            published_days_ago=signal.published_days_ago,
            engagement=signal.engagement,
            distinct_source_types=signal.distinct_source_types,
            duplicate_count=signal.duplicate_count,
        )
        intelligence = extract_intelligence(facts)
        return facts, intelligence


class ModelEnricher:
    """Disabled placeholder for a future model-backed enricher.

    Constructing it is fine (so it can be referenced/tested), but :meth:`enrich`
    refuses to run. Sending customer or source text to an external model is out of
    scope for this batch and must be a separate, explicitly-approved change; until
    then this stub fails closed rather than silently degrading to network egress.
    """

    name = "model"

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled

    def enrich(self, signal: AnalysisInput) -> tuple[SignalFacts, ExtractedIntelligence]:
        raise RuntimeError(
            "ModelEnricher is disabled: sending signal text to an external model is "
            "out of scope for Phase 3B Batch 3 and requires explicit approval. Use the "
            "deterministic enricher."
        )


_DETERMINISTIC = DeterministicEnricher()


def get_enricher(name: str | None = None) -> Enricher:
    """Return the configured enricher. Defaults to the deterministic one.

    Only ``"deterministic"`` yields a usable enricher. ``"model"`` returns the
    disabled stub, whose ``enrich`` raises — so a misconfiguration fails loudly
    instead of quietly emitting network traffic.
    """
    if name in (None, "deterministic"):
        return _DETERMINISTIC
    if name == "model":
        return ModelEnricher(enabled=False)
    raise ValueError(f"Unknown enricher '{name}'")
