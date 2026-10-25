"""Screen model output before it is persisted or shown (P6-LLM-3).

Schema validation (``app.llm.service``) proves only the *shape* of a reply. This
module decides whether its *content* may reach a customer. It is deterministic,
offline and free; it runs on every provider's output, the mock included.

Classification labels are constrained to the closed vocabularies in
``app.core.enums`` - an unknown label (an injected "pwned", a sentence) becomes
``None`` and the pipeline treats the signal as unclassified. The free-text
audience label is normalised and screened like explanation text, against the same
patterns and the claim check. A failing label is dropped, and the pipeline then falls
back to the business's own first audience.

All model text is Unicode-normalised first (NFKC, then Unicode format characters
removed; :func:`app.llm.untrusted.normalise`). A zero-width or fullwidth character
therefore cannot split a link, an address or an echoed phrase, and the normalised
text is what gets persisted.

Explanation text is withheld (replaced by fixed copy, ``explanation_source =
"withheld"``) when any field contains:

* a link (a scheme, ``www.``, ``domain.tld/path`` or a bare lowercase-TLD domain
  such as ``bestdeals.shop``), an e-mail address, a phone number (10 or more
  digits) or markup - none of which the prompt ever asks for, so their presence
  means the model copied them from scraped content;
* an echoed injection marker (``[quoted:`` from input sanitization, or the
  phrases themselves) - evidence the model followed or repeated injected text. The
  one weak marker ``act as`` is common in ordinary text ("parents who act as
  carers"), so its defanged echo ``[quoted:act as]`` is unwrapped to the plain
  phrase, not treated as an injection echo;
* a claim the deterministic claim-safety engine rates ``high`` or ``blocked``
  (``app.claims.engine``; regulated industries escalate ``medium`` to ``high``).

``medium`` findings are kept and surfaced as claim warnings. Over-long fields are
truncated rather than withheld.

The registered ``check_claim_safety`` *LLM* task is deliberately not invoked
here: it would add one paid model call per explained opportunity, it is itself
exposed to the same injected content it would be judging, and its verdict is not
reproducible. The deterministic engine has none of those costs; see the LLM
lane's decision memo for the recorded reasoning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.claims.engine import check_claim_safety
from app.core.enums import ClaimRisk, PainPointDNA, SignalType
from app.llm.untrusted import normalise

_SIGNAL_TYPES = frozenset(t.value for t in SignalType)
_PAIN_POINTS = frozenset(p.value for p in PainPointDNA)
_SENTIMENTS = frozenset({"positive", "neutral", "negative"})

MAX_LABEL_CHARS = 120
MAX_FIELD_CHARS = 600
MAX_ANGLES = 5
MAX_ANGLE_CHARS = 200

_LINK = re.compile(
    r"(?:https?://|ftp://|www\.)\S"  # scheme or www
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}/\S"  # domain.tld/path (shorteners too)
    r"|\b[a-z0-9-]+\.(?:com|net|org|io|co|ly|me|xyz|info|biz|app|dev|link|click|top)\b",
    re.I,
)
#: A bare domain with any TLD: labels of letters/digits/hyphens, then a lowercase TLD
#: of 2-24 letters ("bestdeals.shop", "Deals.example"). It never starts inside a
#: word, so "rush.Promote" (capital) and "e.g." or "3.5" (too short / digits) pass.
_BARE_DOMAIN = re.compile(
    r"(?<![\w@.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[a-z]{2,24}(?![\w-])"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
#: A run of digits with phone-number punctuation; it counts only when it holds at
#: least 10 digits (see :func:`_has_phone`), so years, prices and ranges pass.
_PHONE_RUN = re.compile(r"(?<![\w.])\+?\(?\d[\d ().-]{7,}\d(?![\w.])")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BENIGN_QUOTED = re.compile(r"\[quoted:(act as)\]", re.I)
_MARKUP = re.compile(r"<\s*/?\s*[a-z!?][^>]*>|&lt;|&gt;", re.I)
_INSTRUCTION_ECHO = re.compile(
    r"\[quoted:|ignore (?:all )?previous instructions|disregard (?:the )?above|system prompt"
    r"|untrusted_data",
    re.I,
)
_TEXT_FIELDS = ("why_it_matters", "who_cares", "ai_inference", "recommended_action", "risk_note")


@dataclass(frozen=True)
class ScreenedExplanation:
    output: dict | None  # None when withheld
    warnings: list[str] = field(default_factory=list)
    withheld_reason: str | None = None


def screen_label(value: str | None, vocabulary: frozenset[str]) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip().lower()
    return label if label in vocabulary else None


def _has_phone(text: str) -> bool:
    for match in _PHONE_RUN.finditer(text):
        run = match.group(0)
        if sum(ch.isdigit() for ch in run) >= 10 and not _ISO_DATE.match(run):
            return True
    return False


def _normalised(text: str) -> str:
    """Model text as it will be screened and stored."""
    return _BENIGN_QUOTED.sub(r"\1", _CONTROL.sub(" ", normalise(text)))


def _pattern_reason(text: str) -> str | None:
    """The first content rule ``text`` breaks, or None."""
    for reason, pattern in (
        ("link", _LINK),
        ("link", _BARE_DOMAIN),
        ("contact_detail", _EMAIL),
        ("markup", _MARKUP),
        ("instruction_echo", _INSTRUCTION_ECHO),
    ):
        if pattern.search(text):
            return reason
    if _has_phone(text):
        return "contact_detail"
    return None


def screen_audience(value: str | None, industry: str | None = None) -> str | None:
    """The model's audience label, or None when it breaks any explanation rule."""
    if not isinstance(value, str):
        return None
    label = _normalised(value).strip()
    if not label or _pattern_reason(label):
        return None
    if check_claim_safety(label, industry).risk_level in (ClaimRisk.HIGH, ClaimRisk.BLOCKED):
        return None
    return _cap(label, MAX_LABEL_CHARS)


def screen_classification(out, industry: str | None = None) -> dict:
    """The classification fields the pipeline may use, each constrained or screened."""
    audience = screen_audience(out.audience_fit_label, industry)
    return {
        "signal_type": screen_label(out.signal_type, _SIGNAL_TYPES),
        "pain_point_dna": screen_label(out.pain_point_dna, _PAIN_POINTS),
        "sentiment": screen_label(out.sentiment, _SENTIMENTS) or "neutral",
        "audience_fit_label": audience or None,
    }


def _cap(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def screen_explanation(output: dict, industry: str | None) -> ScreenedExplanation:
    texts = {
        k: _normalised(v) if isinstance(v := output.get(k), str) else "" for k in _TEXT_FIELDS
    }
    angles = [_normalised(a) for a in (output.get("suggested_angles") or []) if isinstance(a, str)]
    everything = "\n".join([*texts.values(), *angles])

    reason = _pattern_reason(everything)
    if reason:
        return ScreenedExplanation(None, [], reason)

    safety = check_claim_safety(everything, industry)
    if safety.risk_level in (ClaimRisk.HIGH, ClaimRisk.BLOCKED):
        warnings = [f"AI explanation withheld: {w}" for w in safety.warnings]
        return ScreenedExplanation(None, warnings, f"claim_risk_{safety.risk_level.value}")

    screened = {k: (_cap(v, MAX_FIELD_CHARS) if v else v) for k, v in texts.items()}
    screened["ai_inference"] = screened["ai_inference"] or None
    screened["suggested_angles"] = [_cap(a, MAX_ANGLE_CHARS) for a in angles[:MAX_ANGLES]]
    warnings = [f"AI explanation: {w}" for w in safety.warnings]
    return ScreenedExplanation(screened, warnings, None)
