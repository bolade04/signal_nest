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

**Jurisdiction-restricted entries (Project Phase 5D, 5D-W1; exit criterion 6).** An entry whose
``jurisdiction_codes`` lists codes applies only where those codes say, as decided by the
shared jurisdiction gate :func:`app.jurisdiction.resolver.jurisdiction_permits` (the gate every
jurisdiction consumer shares; never a substring). Which way an unknown answer falls depends on
what the entry does, so the review can only become stricter (fail closed):

* an entry that **permits** (``approved``) applies only where the draft's jurisdiction is a
  code inside one of the entry's codes (the gate's ``allowed`` limb);
* an entry that **restricts** (a blocking kind, or ``required_disclaimer``) applies unless the
  draft's jurisdiction is a code known to lie outside every one of the entry's codes (the
  gate's ``excluded`` limb: a code that may overlap one of them, or an entry value that is
  not a code, cannot be ruled out);
* a draft with **no jurisdiction** (``None``: none was captured, which since 5D-W3 is only a
  legacy draft, generation being refused from a brief without one) or an ``UNRESOLVED`` or
  malformed one therefore gets every restricted entry that restricts and none that permits
  (ruling B-2).

An entry with ``NULL`` or an empty list applies everywhere, as before.

**Reachable codes only (operator ruling Q-2, 2026-11-08).** An entry may carry only a code the
shared resolver can produce, or a country that contains such codes
(:func:`jurisdiction_code_refusal`, read through the resolver's own readers, never a copy of
their tables): ``ClaimIn`` refuses any other on create and update, so ``US-PR`` is refused and
``PR`` accepted. A stored value no draft can carry (one written before the rule, or by hand) is
read as **unreadable** (:func:`jurisdiction_restriction`): a restricting entry then applies to
every draft and an approved one clears nothing for it, the same fail-closed reading as any
unreadable value. A restriction is never silently dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import ClaimRisk
from app.jurisdiction.codes import is_jurisdiction_code
from app.jurisdiction.resolver import country_code_of, jurisdiction_permits, subdivision_code_of
from app.jurisdiction.subdivisions import SUBDIVISION_JURISDICTIONS
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
#: The library kinds the review reads (``ClaimsLibraryEntry.kind``), in canonical form: a
#: stored kind is always compared through :func:`canonical_kind`, never exactly.
BLOCKED_KIND = "blocked"
APPROVED_KIND = "approved"
DISCLAIMER_KIND = "required_disclaimer"
#: The canonical kinds whose entries block a draft. ``prohibited`` is the campaign-context
#: UI's name for a blocked claim (its kind select offers approved / restricted / prohibited).
BLOCKING_KINDS: frozenset[str] = frozenset({BLOCKED_KIND, "prohibited"})

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


def canonical_kind(kind: str | None) -> str:
    """A library entry's ``kind`` in the one canonical form the 5C review reads
    (DEFECT-5CW4-2): :func:`fold`, the claims matching form, so case, spacing, invisible
    characters and compatibility forms never decide what an entry does.

    ``ClaimIn.kind`` is free text in the CF-2 contract (a closed vocabulary is a later
    contract decision, matrix O11), and the review used to compare it exactly: a steward's
    "Blocked", "BLOCKED" or " blocked", and every "prohibited" entry the campaign-context UI
    creates, were stored and never blocked. Read through this form, fail closed by name:

    * ``blocked`` and ``prohibited`` block (:data:`BLOCKING_KINDS`; :func:`is_blocking_kind`);
    * ``required_disclaimer`` must be carried (:func:`is_disclaimer_kind`);
    * ``approved`` is an approved claim offered to the model (:func:`is_approved_kind`);
    * any other kind — the UI's ``restricted``, a typo, an empty kind — is none of these: it
      blocks nothing, requires nothing and is never offered to the model as approved.
    """
    return fold(kind or "")


def is_blocking_kind(kind: str | None) -> bool:
    """Whether an entry of ``kind`` blocks a draft that contains it."""
    return canonical_kind(kind) in BLOCKING_KINDS


def is_disclaimer_kind(kind: str | None) -> bool:
    """Whether an entry of ``kind`` is a disclaimer the draft must carry."""
    return canonical_kind(kind) == DISCLAIMER_KIND


def is_approved_kind(kind: str | None) -> bool:
    """Whether an entry of ``kind`` is an approved claim."""
    return canonical_kind(kind) == APPROVED_KIND


@dataclass(frozen=True)
class LibraryEntry:
    """One claims-library entry as the engine reads it (a plain projection of the row).

    ``jurisdiction_codes`` is the entry's restriction as :func:`jurisdiction_restriction`
    reads the stored column: ``None`` applies everywhere."""

    id: str
    text: str
    kind: str
    retired_at: datetime | None = None
    jurisdiction_codes: tuple[str, ...] | None = None


def jurisdiction_code_refusal(code: object) -> str | None:
    """Why a claims-library entry may not carry ``code``, or ``None`` when it may (operator
    ruling Q-2: "Refuse new or edited claims-library codes that the supported jurisdiction
    resolver cannot produce").

    An entry may carry exactly the codes that appear in a resolution's ``codes``
    (:func:`app.jurisdiction.resolver.resolve_jurisdiction`):

    * a country code the resolver reads as itself
      (:func:`~app.jurisdiction.resolver.country_code_of`, its reader of a location's country
      field): every ISO 3166-1 code it knows, ``PR`` and ``GB`` included. A country that
      resolves by first-level subdivision (the item-33 list,
      :data:`~app.jurisdiction.subdivisions.SUBDIVISION_JURISDICTIONS`: US, CA, AU, NG) is
      accepted too, as the country that contains its subdivision-level drafts (ruling B-3);
    * a subdivision code the resolver reads as itself
      (:func:`~app.jurisdiction.resolver.subdivision_code_of`), which exists only for a country
      on that list and only for a subdivision in its table: ``US-TX``, ``NG-LA``.

    So ``US-PR`` is refused (Puerto Rico has its own country code, ``PR``, and is not in the
    US table), as are ``US-ZZ``, ``GB-ENG`` (GB resolves as a whole country) and any country
    code the resolver does not know. The definitions are the resolver's own: this composes its
    public readers and its subdivision list and copies no table, so a change to them changes
    this rule with them. It lives here, not in ``app.jurisdiction`` (lane 5D-W2's, merged),
    because the claims library is its only caller. The answer names only codes, so a 422 may
    carry it."""
    if not isinstance(code, str) or not is_jurisdiction_code(code):
        return "it is not a jurisdiction code"
    country, _, part = code.partition("-")
    if country_code_of(country) != country:
        return f"{country} is not an ISO 3166-1 country code"
    if not part:
        return None
    if country not in SUBDIVISION_JURISDICTIONS:
        return f"{country} applies as a whole country, so use {country}"
    if subdivision_code_of(country, code) != code:
        own = f" ({part} has its own country code, so use {part})"
        return f"{part} is not a state or province of {country}" + (
            own if country_code_of(part) == part else ""
        )
    return None


def is_reachable_jurisdiction_code(code: object) -> bool:
    """Whether a claims-library entry may carry ``code`` (:func:`jurisdiction_code_refusal`)."""
    return jurisdiction_code_refusal(code) is None


def jurisdiction_restriction(stored: object) -> tuple[str, ...] | None:
    """An entry's jurisdiction restriction from its stored ``jurisdiction_codes`` value.

    ``None`` (applies everywhere) for ``NULL`` or an empty list, the column's meaning (CF-3).
    Otherwise the listed values. A value no draft can carry becomes ``""``: one that is not a
    string, one outside the grammar, or (Q-2) a well-formed code the resolver cannot produce
    (:func:`is_reachable_jurisdiction_code`: ``US-PR``, ``US-ZZ``, ``GB-ENG``, written before
    the rule or by hand). A stored value that is not a list becomes ``("",)``. ``""`` names
    nothing readable: no jurisdiction lies inside it and none can be ruled out of it, so a
    restricting entry applies to every draft and an approved one clears nothing for it (fail
    closed both ways). A restriction is never silently dropped."""
    if stored is None:
        return None
    if isinstance(stored, list | tuple):
        if not stored:
            return None
        return tuple(code if is_reachable_jurisdiction_code(code) else "" for code in stored)
    return ("",)


def _restricts(kind: str | None) -> bool:
    """Whether an entry of ``kind`` can only make a draft's review stricter."""
    return is_blocking_kind(kind) or is_disclaimer_kind(kind)


def applies_in_jurisdiction(entry: LibraryEntry, jurisdiction: str | None) -> bool:
    """Whether ``entry`` applies to a draft of ``jurisdiction`` (fail closed; module docstring)."""
    restriction = jurisdiction_restriction(entry.jurisdiction_codes)
    if restriction is None:
        return True
    if _restricts(entry.kind):
        return not jurisdiction_permits(jurisdiction, excluded=restriction)
    return jurisdiction_permits(jurisdiction, allowed=restriction)


def active_entries(
    entries: Iterable[LibraryEntry], *, jurisdiction: str | None = None
) -> list[LibraryEntry]:
    """The entries a review applies: every entry that is not retired (CF-2 soft retirement)
    and applies in the draft's ``jurisdiction`` (:func:`applies_in_jurisdiction`; ``None``, the
    default, is a draft with no captured jurisdiction).

    A retired entry blocks nothing, requires nothing and approves nothing; nor does a
    jurisdiction-restricted entry where it does not apply."""
    return [
        e
        for e in entries
        if e.retired_at is None and applies_in_jurisdiction(e, jurisdiction)
    ]


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
    *,
    jurisdiction: str | None = None,
) -> LibraryReview:
    """Review generated ``text`` against the workspace's claims library (Phase 5C §2).

    ``check_claim_safety`` runs over the folded text with every **active** blocking entry
    (``blocked`` or ``prohibited`` in :func:`canonical_kind` form) as a blocked claim (one hit
    makes the result ``blocked``); the hit entries' ids are returned. Required disclaimers
    are the active ``required_disclaimer`` entries (canonical form) plus
    ``extra_disclaimers`` (the offer calendar's), each resolved when the text contains it.
    Retired entries, and jurisdiction-restricted entries that do not apply in the draft's
    ``jurisdiction``, are ignored (:func:`active_entries`).
    """
    active = active_entries(entries, jurisdiction=jurisdiction)
    folded = fold(text)
    blocked = [e for e in active if is_blocking_kind(e.kind) and fold(e.text)]
    safety = check_claim_safety(folded, industry, blocked_claims=[fold(e.text) for e in blocked])
    hit_ids = [e.id for e in blocked if contains_phrase(folded, fold(e.text))]
    disclaimers = _unique(
        [*(e.text for e in active if is_disclaimer_kind(e.kind)), *extra_disclaimers]
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
