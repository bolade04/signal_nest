"""Relevance engine.

Answers: does this signal matter to *this* business/product/audience/market/campaign?
Hard rule enforced by callers: relevance < 40 => never recommend action.
"""

from __future__ import annotations

import re

from app.scoring.types import BusinessContext, SignalInput

_TOKEN_RE = re.compile(r"[a-z0-9']+")
RELEVANCE_ACTION_FLOOR = 40


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _overlap_score(content_tokens: set[str], terms: list[str]) -> tuple[float, list[str]]:
    hits: list[str] = []
    for term in terms:
        term_tokens = _tokens(term)
        if term_tokens and term_tokens.issubset(content_tokens):
            hits.append(term)
        elif _tokens(term) & content_tokens:
            hits.append(term)
    if not terms:
        return 0.0, hits
    return len(hits) / len(terms), hits


def score_relevance(signal: SignalInput, ctx: BusinessContext) -> tuple[int, dict]:
    """Return (0..100 relevance, explanation dict)."""
    content_tokens = _tokens(signal.content)

    # Exclusion rules can hard-kill relevance.
    excluded_hit = [t for t in ctx.exclusion_terms if _tokens(t) & content_tokens]
    if excluded_hit:
        return 0, {
            "reason": "excluded_terms",
            "excluded": excluded_hit,
            "note": "Signal matched product/business exclusion rules.",
        }

    kw, kw_hits = _overlap_score(content_tokens, ctx.keywords)
    pain, pain_hits = _overlap_score(content_tokens, ctx.pain_points)
    aud, aud_hits = _overlap_score(content_tokens, ctx.audiences)
    comp, comp_hits = _overlap_score(content_tokens, ctx.competitors)

    # Weighted blend (keywords + pain points dominate topical relevance).
    raw = kw * 0.4 + pain * 0.3 + aud * 0.2 + comp * 0.1

    # Signal-type nudges: strong commercial/pain signals are more relevant.
    type_bonus = 0.0
    if signal.signal_type in {"buying_intent", "pain_point", "complaint"}:
        type_bonus += 0.1
    if signal.has_buying_intent:
        type_bonus += 0.05

    score = int(round(min(1.0, raw + type_bonus) * 100))
    explanation = {
        "keyword_hits": kw_hits,
        "pain_point_hits": pain_hits,
        "audience_hits": aud_hits,
        "competitor_hits": comp_hits,
        "type_bonus": round(type_bonus, 2),
        "below_action_floor": score < RELEVANCE_ACTION_FLOOR,
    }
    return score, explanation
