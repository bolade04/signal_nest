"""Claim-safety / brand-safety engine (pure, framework-free).

Two responsibilities used by Phase 2 reasoning:
  1. ``check_claim_safety`` — flag risky claims in generated/derived text.
  2. ``translate_competitor_weakness`` — turn a competitor complaint into a customer
     pain point and *safe* positive positioning (never an unsupported product claim).

Core rule: use competitor complaints as market insight, not as proof the user's
product is better.

**The workspace claims library (Project Phase 5C).** :func:`review_against_library` is the
output-side content review of a generated draft: ``check_claim_safety`` over the text with the
workspace's **active** ``blocked`` entries as ``blocked_claims``, the ids of the entries the
text hits, and the resolution of every required disclaimer (the library's active
``required_disclaimer`` entries plus any the caller threads in, e.g. the offer calendar's).
**A retired entry is ignored** (:func:`active_entries`): retiring a claim (``retired_at``,
the CF-2 soft retirement) removes it from every review from then on, whichever caller loaded
the library. The scouting pipeline's claim check (``app.jobs.pipeline``) runs on third-party
discussion text and passes no library at all, so no library entry — active or retired — can
reach it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import ClaimRisk
from app.llm.untrusted import normalise

# Industries requiring stricter controls.
STRICT_INDUSTRIES = {
    "alcohol", "cosmetics", "healthcare", "finance", "supplements", "legal",
    "legal services", "insurance", "children", "cybersecurity", "real estate",
    "education", "food and beverage",
}

_FLAGS: list[tuple[str, str, ClaimRisk]] = [
    (r"\b(cure|cures|treats?|prevents?|heals?)\b", "health_claim", ClaimRisk.HIGH),
    (r"\b(guarantee[ds]?|guaranteed)\b", "guarantee", ClaimRisk.HIGH),
    (r"\b(no\s+side\s+effects|risk[- ]free|100%\s+safe)\b", "safety_claim", ClaimRisk.HIGH),
    (r"\b(FDA|clinically proven|doctor recommended)\b", "regulated_claim", ClaimRisk.HIGH),
    (r"\b(guaranteed returns?|double your money|get rich)\b", "financial_claim", ClaimRisk.BLOCKED),
    (r"\bbest\b", "superlative_best", ClaimRisk.MEDIUM),
    (r"\b(number\s*one|#1|no\.?\s*1)\b", "superlative_number_one", ClaimRisk.MEDIUM),
    (r"\bwon'?t\s+(cause|give you)\b", "negative_absolute_claim", ClaimRisk.HIGH),
    (r"\b(better than|beats|outperforms)\s+\w+", "comparative_claim", ClaimRisk.MEDIUM),
    (
        r"\b(they|competitor|brand x)\b.*\b(ignore|scam|lie|fail)\b",
        "competitor_attack",
        ClaimRisk.HIGH,
    ),
]

_RISK_ORDER = {ClaimRisk.LOW: 0, ClaimRisk.MEDIUM: 1, ClaimRisk.HIGH: 2, ClaimRisk.BLOCKED: 3}


def contains_phrase(text: str, phrase: str | None) -> bool:
    """Whether ``text`` contains the non-empty ``phrase`` (case-insensitive substring) — the
    one matching rule for blocked claims and required disclaimers."""
    return bool(phrase) and phrase.lower() in text.lower()


@dataclass
class ClaimFinding:
    category: str
    risk: ClaimRisk
    excerpt: str


@dataclass
class ClaimSafetyResult:
    risk_level: ClaimRisk
    findings: list[ClaimFinding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked_terms: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return self.risk_level == ClaimRisk.BLOCKED


def check_claim_safety(
    text: str,
    industry: str | None = None,
    blocked_claims: list[str] | None = None,
) -> ClaimSafetyResult:
    findings: list[ClaimFinding] = []
    warnings: list[str] = []
    blocked_hit: list[str] = []
    highest = ClaimRisk.LOW

    for pattern, category, risk in _FLAGS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            findings.append(ClaimFinding(category=category, risk=risk, excerpt=m.group(0)))
            if _RISK_ORDER[risk] > _RISK_ORDER[highest]:
                highest = risk
            warnings.append(f"Detected {category.replace('_', ' ')}: '{m.group(0)}'")

    for claim in blocked_claims or []:
        if contains_phrase(text, claim):
            blocked_hit.append(claim)
            highest = ClaimRisk.BLOCKED
            warnings.append(f"Contains a blocked claim: '{claim}'")

    # Strict industries escalate medium findings to high.
    if industry and industry.lower() in STRICT_INDUSTRIES and findings:
        if highest == ClaimRisk.MEDIUM:
            highest = ClaimRisk.HIGH
        warnings.append(f"'{industry}' is a strictly regulated category; extra review required.")

    return ClaimSafetyResult(
        risk_level=highest,
        findings=findings,
        warnings=warnings,
        blocked_terms=blocked_hit,
    )


# --------------------------------------------------------------------------- #
# The workspace claims library (Project Phase 5C output-side content review)
# --------------------------------------------------------------------------- #
#: The library kinds the review reads (``ClaimsLibraryEntry.kind``).
BLOCKED_KIND = "blocked"
APPROVED_KIND = "approved"
DISCLAIMER_KIND = "required_disclaimer"

_WHITESPACE = re.compile(r"\s+")
#: Full stops that IDNA and browsers treat as "." (ideographic, fullwidth, halfwidth
#: ideographic) — the same map the output screen applies (``app.llm.screening``).
_FULL_STOPS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})


def fold(text: str) -> str:
    """The one canonical matching form of a text, applied to BOTH sides of every library
    match — the draft text and every library entry and required disclaimer, in the review
    (:func:`review_against_library`) and in the approval-time re-check
    (``app.creative.service.draft_approval_refusal``) alike:

    ``casefold(collapse_ws(normalise(text).translate(FULL_STOPS)))`` — NFKC, every ``Cf`` and
    Default_Ignorable code point removed (soft hyphen, ZWJ/ZWNJ, variation selectors), NFKC
    again (:func:`app.llm.untrusted.normalise`, the output screen's form), the ideographic,
    fullwidth and halfwidth full stops mapped to ".", whitespace runs collapsed, trimmed,
    case-folded. So an invisible character, a line break, a compatibility form ("ｃｕｒｅ") or a
    different full stop can neither hide a blocked claim nor keep a carried disclaimer from
    resolving, on either side."""
    canonical = normalise(text).translate(_FULL_STOPS)
    return _WHITESPACE.sub(" ", canonical).strip().casefold()


@dataclass(frozen=True)
class LibraryEntry:
    """One claims-library entry as the engine reads it (a plain projection of the row)."""

    id: str
    text: str
    kind: str
    retired_at: datetime | None = None


def active_entries(entries: Iterable[LibraryEntry]) -> list[LibraryEntry]:
    """The entries a review applies: every entry that is not retired (CF-2 soft retirement).

    A retired entry blocks nothing, requires nothing and approves nothing."""
    return [e for e in entries if e.retired_at is None]


def _unique(texts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for text in texts:
        key = fold(text or "")
        if key and key not in seen:
            seen.add(key)
            kept.append(text.strip())
    return kept


@dataclass(frozen=True)
class LibraryReview:
    """The output-side review of one generated text against one workspace's library."""

    safety: ClaimSafetyResult
    #: Ids of the active ``blocked`` entries the text contains.
    blocked_claim_ids: list[str]
    #: Every disclaimer the text had to carry (library entries first, then the caller's).
    required_disclaimers: list[str]
    #: The required disclaimers the text does not carry.
    missing_disclaimers: list[str]

    @property
    def is_blocked(self) -> bool:
        return self.safety.is_blocked

    @property
    def disclaimers_resolved(self) -> bool:
        return not self.missing_disclaimers

    @property
    def passed(self) -> bool:
        """Nothing blocks and every required disclaimer is carried."""
        return not self.is_blocked and self.disclaimers_resolved


def review_against_library(
    text: str,
    industry: str | None,
    entries: Iterable[LibraryEntry],
    extra_disclaimers: Sequence[str] = (),
) -> LibraryReview:
    """Review generated ``text`` against the workspace's claims library (Phase 5C §2).

    ``check_claim_safety`` runs over the folded text with every **active** ``blocked`` entry
    as a blocked claim (one hit makes the result ``blocked``); the hit entries' ids are
    returned. Required disclaimers are the active ``required_disclaimer`` entries plus
    ``extra_disclaimers`` (the offer calendar's), each resolved when the text contains it.
    Retired entries are ignored (:func:`active_entries`).
    """
    active = active_entries(entries)
    folded = fold(text)
    blocked = [e for e in active if e.kind == BLOCKED_KIND and fold(e.text)]
    safety = check_claim_safety(folded, industry, blocked_claims=[fold(e.text) for e in blocked])
    hit_ids = [e.id for e in blocked if contains_phrase(folded, fold(e.text))]
    disclaimers = _unique(
        [*(e.text for e in active if e.kind == DISCLAIMER_KIND), *extra_disclaimers]
    )
    missing = [d for d in disclaimers if not contains_phrase(folded, fold(d))]
    return LibraryReview(safety, hit_ids, disclaimers, missing)


# Competitor pain point -> safe positioning translation table.
_SAFE_POSITIONING = {
    "poor_customer_service": "Support that actually feels responsive.",
    "speed_complaint": "Built for a faster, smoother experience.",
    "hidden_fees": "Clear, upfront pricing with no surprises.",
    "lack_of_transparency": "Transparency you can verify.",
    "quality_concern": "Consistent quality you can rely on.",
    "trust_issue": "Earned trust, backed by real proof.",
    "bad_user_experience": "An experience designed to feel effortless.",
    "price_frustration": "Fair pricing that respects your budget.",
    "product_confusion": "Clear guidance so you always know what you're getting.",
    "safety_concern": "Consider brand-safe, educational messaging about your standards.",
}


def translate_competitor_weakness(pain_point_dna: str | None, category: str | None = None) -> str:
    """Return safe positive positioning (never an attack or unsupported claim)."""
    if pain_point_dna and pain_point_dna in _SAFE_POSITIONING:
        return _SAFE_POSITIONING[pain_point_dna]
    if category in STRICT_INDUSTRIES:
        return (
            "Consumers are discussing product quality and experience in this category. "
            "Consider brand-safe, educational messaging using only approved claims."
        )
    return "Focus on your proven strengths with brand-safe, approved messaging."
