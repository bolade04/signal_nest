"""Noise filter + pre-analysis gate.

Core rule: collect broadly, notify selectively. Pre-analysis score bands:
  0-24 ignore | 25-49 monitor lightly | 50-69 analyze | 70-84 rank | 85-100 alert-worthy
Full analysis generally begins at >= 50.
"""

from __future__ import annotations

import re

from app.scoring.types import SignalInput

PRE_ANALYSIS_FULL_THRESHOLD = 50

_SPAM_PATTERNS = [
    r"\bfree money\b",
    r"\bclick here\b",
    r"\bbuy followers\b",
    r"\bwork from home\b.*\$\$\$",
    r"https?://\S+\s+https?://\S+\s+https?://\S+",  # link spam
    r"\bDM me\b.*\bcrypto\b",
]
_ENGAGEMENT_BAIT = [r"\blike and share\b", r"\btag a friend\b", r"\bcomment below\b"]
_UNSAFE = [r"\bshooting\b", r"\bterror\b", r"\bsuicide\b", r"\bdeath toll\b"]


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def evaluate_noise(signal: SignalInput) -> tuple[int, bool, list[str]]:
    """Return (pre_analysis_score 0..100, is_noise, reasons)."""
    reasons: list[str] = []
    text = signal.content.strip()

    if len(text) < 15:
        reasons.append("low_context")
    if _matches_any(text, _SPAM_PATTERNS):
        reasons.append("spam")
    if _matches_any(text, _ENGAGEMENT_BAIT):
        reasons.append("engagement_bait")
    if _matches_any(text, _UNSAFE):
        reasons.append("unsafe_sensitive")
    if signal.author and signal.author.lower().startswith("bot_"):
        reasons.append("bot_like")
    if signal.duplicate_count > 6 and signal.distinct_source_types == 1:
        reasons.append("duplicate_flood")

    # Start from a base and reward signal quality.
    score = 50
    if signal.signal_type in {"complaint", "pain_point", "buying_intent", "question"}:
        score += 15
    if signal.has_buying_intent:
        score += 10
    if signal.engagement >= 50:
        score += 10
    elif signal.engagement >= 10:
        score += 5
    if signal.distinct_source_types >= 2:
        score += 10
    if signal.age_days > 120:
        score -= 20
    elif signal.age_days > 45:
        score -= 8

    # Penalize each noise reason heavily.
    score -= 25 * len(reasons)
    score = max(0, min(100, score))

    hard_noise = any(
        r in reasons for r in ("spam", "unsafe_sensitive", "bot_like", "engagement_bait")
    )
    is_noise = hard_noise or score < 25
    return score, is_noise, reasons
