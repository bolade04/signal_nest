"""Screen model output before it is persisted or shown (P6-LLM-3).

Schema validation (``app.llm.service``) proves only the *shape* of a reply. This
module decides whether its *content* may reach a customer. It is deterministic,
offline and free; it runs on every provider's output, the mock included.

Classification labels are constrained to the closed vocabularies in
``app.core.enums`` - an unknown label (an injected "pwned", a sentence) becomes
``None`` and the pipeline treats the signal as unclassified - and the free-text
audience label is cleaned and capped.

Explanation text is withheld (replaced by fixed copy, ``explanation_source =
"withheld"``) when any field contains:

* a link, an e-mail address or markup - none of which the prompt ever asks for,
  so their presence means the model copied them from scraped content;
* an echoed injection marker (``[quoted:`` from input sanitization, or the
  phrases themselves) - evidence the model followed or repeated injected text;
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
from app.llm.untrusted import clean

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
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
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


def screen_classification(out) -> dict:
    """The classification fields the pipeline may use, each constrained or cleaned."""
    audience = clean(out.audience_fit_label, MAX_LABEL_CHARS) if out.audience_fit_label else None
    if audience and (_LINK.search(audience) or _EMAIL.search(audience)):
        audience = None
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
    texts = {k: output.get(k) or "" for k in _TEXT_FIELDS}
    angles = [a for a in (output.get("suggested_angles") or []) if isinstance(a, str)]
    everything = "\n".join([*texts.values(), *angles])

    for reason, pattern in (
        ("link", _LINK),
        ("contact_detail", _EMAIL),
        ("markup", _MARKUP),
        ("instruction_echo", _INSTRUCTION_ECHO),
    ):
        if pattern.search(everything):
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
