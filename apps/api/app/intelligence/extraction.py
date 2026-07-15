"""Deterministic, offline signal extraction.

This module derives :class:`SignalFacts` (literal) and
:class:`ExtractedIntelligence` (inference) from a signal's text using only
lexicon/regex matchers — **no** model call, **no** network, **no** ``eval``. The
same text always yields the same result.

Untrusted-content safety
-------------------------
Source text is adversarial input. Before any matching or span recording,
:func:`sanitize_text` strips control characters and *defangs* prompt-injection
markers by quoting them (``[quoted:...]``) so that no downstream consumer — a
model enricher, a log, or the UI — can be steered by embedded instructions. Every
:class:`EvidenceSpan` therefore points into the sanitized excerpt, never the raw
bytes.
"""

from __future__ import annotations

import re

from app.intelligence.models import (
    EvidenceSpan,
    ExtractedIntelligence,
    InferredAttribute,
    SignalFacts,
)

# Injection markers that must never be obeyed if they appear in source text. We
# neutralize by quoting, mirroring the Batch 2 connector-safety discipline.
_INJECTION_MARKERS = [
    r"ignore previous instructions",
    r"ignore all previous instructions",
    r"disregard (?:the )?above",
    r"system prompt",
    r"you are now",
    r"act as",
    r"assistant:",
    r"</?system>",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_MARKERS), re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD_RE = re.compile(r"[A-Za-z0-9']+")

MAX_EXCERPT_CHARS = 400


def sanitize_text(text: str) -> str:
    """Return display-safe text: control chars stripped, injection markers defanged.

    Defanging quotes the marker (``ignore previous instructions`` ->
    ``[quoted:ignore previous instructions]``) so the phrase is preserved for audit
    but can never read as a live instruction to any downstream model or tool.
    """
    cleaned = _CONTROL_RE.sub(" ", text)

    def _quote(match: re.Match[str]) -> str:
        return f"[quoted:{match.group(0)}]"

    return _INJECTION_RE.sub(_quote, cleaned).strip()


# Lexicons: token -> (signal_type, weight). Longest/most-specific wins.
_SIGNAL_LEXICON: dict[str, tuple[str, float]] = {
    "want to buy": ("buying_intent", 0.9),
    "looking to buy": ("buying_intent", 0.9),
    "ready to purchase": ("buying_intent", 0.9),
    "how much does": ("buying_intent", 0.7),
    "where can i get": ("buying_intent", 0.7),
    "switching from": ("competitor_dissatisfaction", 0.8),
    "cancel my": ("competitor_dissatisfaction", 0.75),
    "worse than": ("competitor_dissatisfaction", 0.7),
    "terrible": ("complaint", 0.7),
    "awful": ("complaint", 0.7),
    "so slow": ("complaint", 0.75),
    "too slow": ("complaint", 0.75),
    "frustrated": ("complaint", 0.7),
    "hate": ("complaint", 0.65),
    "keeps breaking": ("complaint", 0.7),
    "wish there was": ("feature_request", 0.7),
    "i wish": ("feature_request", 0.6),
    "should add": ("feature_request", 0.65),
    "how do i": ("question", 0.6),
    "does anyone know": ("question", 0.6),
    "trending": ("trend_discussion", 0.6),
    "everyone is talking about": ("trend_discussion", 0.65),
}

_PAIN_LEXICON: dict[str, tuple[str, float]] = {
    "slow": ("speed_complaint", 0.7),
    "delay": ("speed_complaint", 0.65),
    "late": ("speed_complaint", 0.6),
    "expensive": ("price_frustration", 0.7),
    "overpriced": ("price_frustration", 0.75),
    "hidden fee": ("hidden_fees", 0.8),
    "extra charge": ("hidden_fees", 0.7),
    "rude": ("poor_customer_service", 0.75),
    "no response": ("poor_customer_service", 0.7),
    "confusing": ("product_confusion", 0.7),
    "hard to use": ("bad_user_experience", 0.7),
    "scam": ("fear_of_being_scammed", 0.8),
    "unreliable": ("need_for_reliability", 0.7),
    "broke": ("quality_concern", 0.65),
    "cheap quality": ("quality_concern", 0.7),
}

_POSITIVE_WORDS = {"love", "great", "excellent", "amazing", "best", "recommend", "fantastic"}
_NEGATIVE_WORDS = {
    "hate",
    "terrible",
    "awful",
    "worst",
    "bad",
    "slow",
    "rude",
    "broken",
    "frustrated",
    "scam",
    "disappointed",
}

_BUYING_INTENT_RE = re.compile(
    r"\b(buy|purchase|order|subscribe|sign up|pricing|quote|book|hire)\b", re.IGNORECASE
)


def extract_facts(
    content: str,
    *,
    source_type: str,
    market: str | None,
    author: str | None,
    language: str,
    published_days_ago: float,
    engagement: int,
    distinct_source_types: int,
    duplicate_count: int,
) -> SignalFacts:
    """Derive only literally-observable fields. Nothing is inferred here."""
    excerpt = sanitize_text(content)[:MAX_EXCERPT_CHARS]
    return SignalFacts(
        source_type=source_type,
        market=market,
        author=author,
        language=language,
        published_days_ago=published_days_ago,
        char_count=len(excerpt),
        word_count=len(_WORD_RE.findall(excerpt)),
        excerpt=excerpt,
        distinct_source_types=distinct_source_types,
        duplicate_count=duplicate_count,
        engagement=engagement,
    )


def _find_spans(haystack_lower: str, excerpt: str, phrase: str, method: str) -> list[EvidenceSpan]:
    spans: list[EvidenceSpan] = []
    start = 0
    while True:
        idx = haystack_lower.find(phrase, start)
        if idx == -1:
            break
        spans.append(
            EvidenceSpan(start=idx, end=idx + len(phrase), quote=excerpt[idx : idx + len(phrase)],
                         method=method)
        )
        start = idx + len(phrase)
    return spans


def _best_from_lexicon(
    excerpt: str, lexicon: dict[str, tuple[str, float]], method_prefix: str
) -> InferredAttribute | None:
    lower = excerpt.lower()
    best: tuple[float, str, list[EvidenceSpan]] | None = None
    for phrase, (label, weight) in lexicon.items():
        if phrase in lower:
            spans = _find_spans(lower, excerpt, phrase, f"{method_prefix}:{label}")
            if best is None or weight > best[0]:
                best = (weight, label, spans)
    if best is None:
        return None
    weight, label, spans = best
    return InferredAttribute(
        value=label, confidence=weight, method=f"{method_prefix}:{label}", evidence=tuple(spans)
    )


def _sentiment(excerpt: str) -> InferredAttribute | None:
    lower = excerpt.lower()
    tokens = set(_WORD_RE.findall(lower))
    pos = tokens & _POSITIVE_WORDS
    neg = tokens & _NEGATIVE_WORDS
    if not pos and not neg:
        return None
    if len(neg) >= len(pos):
        label, hits = "negative", neg
    else:
        label, hits = "positive", pos
    spans: list[EvidenceSpan] = []
    for word in sorted(hits):
        spans.extend(_find_spans(lower, excerpt, word, f"sentiment:{label}"))
    confidence = min(1.0, 0.5 + 0.15 * len(hits))
    return InferredAttribute(
        value=label, confidence=confidence, method=f"sentiment:{label}", evidence=tuple(spans)
    )


def extract_intelligence(facts: SignalFacts) -> ExtractedIntelligence:
    """Derive the evidence-backed inference layer from already-sanitized facts."""
    excerpt = facts.excerpt
    lower = excerpt.lower()

    signal_type = _best_from_lexicon(excerpt, _SIGNAL_LEXICON, "lexicon")
    pain = _best_from_lexicon(excerpt, _PAIN_LEXICON, "pain")
    sentiment = _sentiment(excerpt)

    intent_spans: list[EvidenceSpan] = []
    for m in _BUYING_INTENT_RE.finditer(excerpt):
        intent_spans.append(
            EvidenceSpan(start=m.start(), end=m.end(), quote=m.group(0), method="intent:keyword")
        )
    has_buying_intent = bool(intent_spans) or (
        signal_type is not None and signal_type.value == "buying_intent"
    )
    has_competitor_dissatisfaction = (
        signal_type is not None and signal_type.value == "competitor_dissatisfaction"
    ) or "switching from" in lower

    return ExtractedIntelligence(
        signal_type=signal_type,
        pain_point_dna=pain,
        sentiment=sentiment,
        has_buying_intent=has_buying_intent,
        has_competitor_dissatisfaction=has_competitor_dissatisfaction,
        intent_evidence=tuple(intent_spans),
    )
