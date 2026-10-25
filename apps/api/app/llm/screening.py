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

All model text is first Unicode-normalised (NFKC, then invisible characters
removed: ``Cf`` and every ``Default_Ignorable_Code_Point``;
:func:`app.llm.untrusted.normalise`) and capped at its field limit. A zero-width,
fullwidth or variation-selector character therefore cannot split a link, an
address or an echoed phrase. The screen's cost is bounded whatever the reply
length, and the normalised, capped text it screened is exactly what gets
persisted.

Explanation text is withheld (replaced by fixed copy, ``explanation_source =
"withheld"``) when any field contains:

* a link, an e-mail address, a phone number or markup - none of which the prompt
  ever asks for, so their presence means the model copied them from scraped content.
  A link is a scheme, ``www.``, ``domain.tld/path``, a ``.com``-style domain
  ("Amazon.com" included, deliberately), or a bare domain whose final label is a
  ccTLD or a listed generic TLD ("bestdeals.shop"), or any dot-TLD token holding a
  non-ASCII letter (look-alike scripts). An e-mail address can be literal or
  spelled out ("sales (at) deals (dot) com"). A phone number is 10 or more digits
  written like one, with ``+`` or ``( ) - .``. Ordinary copy passes: "Node.js",
  "report.pdf", "long.customers", sizes "10 12 14", year runs, ISO dates;
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

# Every pattern below is linear in the text length: each repeated part is either
# bounded or can only start where a run of its own characters starts (a negative
# lookbehind on that character class), so a long adversarial run is scanned once.
# The texts are also capped before they are screened (see ``_prepared``).
_LINK = re.compile(
    r"(?:https?://|ftp://|www\.)\S"  # scheme or www
    r"|(?<![a-z0-9.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}/\S"  # domain.tld/path
    r"|(?<![a-z0-9-])[a-z0-9-]+\.(?:com|net|org|io|co|ly|me|xyz|info|biz|app|dev|link|click"
    r"|top)\b",
    re.I,
)
#: A dot-joined token ending in a 2-24 letter final label (Unicode letters, so
#: look-alike scripts are seen too). :func:`_has_bare_domain` decides whether the
#: final label is a TLD.
_DOMAIN_TOKEN = re.compile(
    r"(?<![\w@.-])((?:\w(?:[\w-]{0,61}\w)?\.){1,8})([^\W\d_]{2,24})(?![\w-])"
)
#: Generic TLDs counted as links when bare (two-letter final labels are ccTLDs).
#: ``.com``-style TLDs are also in ``_LINK``, case-insensitively, so "Amazon.com"
#: and "Booking.com" are withheld: they are real domains, which the prompt never
#: asks for. That is a deliberate choice.
_GENERIC_TLDS = frozenset(
    "com net org info biz xyz top club online site store shop app dev io co me ly link "
    "click live life world today news blog tech space website fun icu vip pro win bid "
    "loan buzz rest cyou monster sbs cfd lol quest cam bar deals sale promo gift fit tv "
    "cc ws mobi name page cloud digital agency email network global inc ltd llc group "
    "services solutions finance money cash bank bet casino poker games wiki host press "
    "media studio download stream watch video tube example".split()
)
#: Two-letter final labels that are file extensions or language names in ordinary
#: copy ("Node.js", "index.ts", "script.py", "README.md"); they are not treated as
#: ccTLDs. (Some, e.g. ``.py``/``.md``, are real ccTLDs; a bare domain under them is
#: a recorded residual.)
_NOT_TLDS = frozenset({"js", "ts", "py", "rb", "go", "gz", "md"})
#: Product names whose dot is not a domain: removed before the link checks only.
_TECH_NAMES = re.compile(
    r"\b(?:node|vue|next|nuxt|react|express|three|d3|chart|ember|backbone)\.js\b"
    r"|\b(?:asp|vb|ado)\.net\b",
    re.I,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]+")
#: An address spelled out to dodge filters: "sales (at) deals (dot) com".
_SPELLED_EMAIL = re.compile(
    r"\w{1,64}\s{0,3}[(\[]\s{0,3}at\s{0,3}[)\]]\s{0,3}[\w-]{1,63}"
    r"\s{0,3}(?:[(\[]\s{0,3}dot\s{0,3}[)\]]|\.)\s{0,3}[a-z]{2,24}",
    re.I,
)
#: Maximal runs of digits and phone punctuation, found in one linear pass; each run
#: is judged by :func:`_has_phone`.
_PHONE_RUN = re.compile(r"[+(]?\d[\d ().-]*\d")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_YEAR = re.compile(r"(?:19|20)\d\d")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BENIGN_QUOTED = re.compile(r"\[quoted:(act as)\]", re.I)
_MARKUP = re.compile(r"<\s*/?\s*[a-z!?][^>]*>|&lt;|&gt;", re.I)
_INSTRUCTION_ECHO = re.compile(
    r"\[quoted:|ignore\s+(?:all\s+)?previous\s+instructions|disregard\s+(?:the\s+)?above"
    r"|system\s+prompt"
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
    """A phone number: a run of at least 10 digits that is written like one - it starts
    with ``+`` or uses ``( ) - .`` punctuation. Space-separated numbers ("sizes 10 12
    14 16 18"), ISO dates and runs of years ("2019-2020-2021") are not phones."""
    for match in _PHONE_RUN.finditer(text):
        run = match.group(0)
        if sum(ch.isdigit() for ch in run) < 10:
            continue
        if not (run.startswith("+") or any(ch in run for ch in "().-")):
            continue
        if _ISO_DATE.match(run):
            continue
        groups = [g for g in re.split(r"[ ().+-]+", run) if g]
        if groups and all(_YEAR.fullmatch(g) for g in groups):
            continue
        return True
    return False


def _has_bare_domain(text: str) -> bool:
    """A bare domain: a dot-joined token whose final label is a TLD.

    An all-ASCII token counts when its final label is lowercase and either a
    two-letter ccTLD (not a file extension in ``_NOT_TLDS``) or a generic TLD in
    ``_GENERIC_TLDS``. So "long.customers", "report.pdf" and "rush.Promote" pass.
    A token with any non-ASCII letter always counts. It is either an
    internationalised domain or a look-alike (Cyrillic "dеals.shop"), and NFKC does
    not fold either; English copy has no such token. This is a simpler, documented
    stand-in for a UTS #39 confusable skeleton.
    """
    for match in _DOMAIN_TOKEN.finditer(text):
        token = match.group(0)
        tld = match.group(2)
        if not token.isascii():
            return True
        if tld != tld.lower():
            continue
        if (len(tld) == 2 and tld not in _NOT_TLDS) or tld in _GENERIC_TLDS:
            return True
    return False


def _normalised(text: str) -> str:
    """Model text as it will be screened and stored."""
    return _BENIGN_QUOTED.sub(r"\1", _CONTROL.sub(" ", normalise(text)))


def _prepared(text: str, limit: int) -> str:
    """Normalised and capped at ``limit`` BEFORE screening: the screen's cost is
    bounded whatever the reply length, and it screens exactly what is stored. The raw
    text is pre-cut (with slack, since NFKC may expand) so normalising is bounded too."""
    return _cap(_normalised(text[: limit * 4]), limit)


def _pattern_reason(text: str) -> str | None:
    """The first content rule ``text`` breaks, or None."""
    for reason, pattern in (
        ("markup", _MARKUP),
        ("instruction_echo", _INSTRUCTION_ECHO),
        ("contact_detail", _EMAIL),
        ("contact_detail", _SPELLED_EMAIL),
    ):
        if pattern.search(text):
            return reason
    linkable = _TECH_NAMES.sub(" ", text)
    if _LINK.search(linkable) or _has_bare_domain(linkable):
        return "link"
    if _has_phone(text):
        return "contact_detail"
    return None


def screen_audience(value: str | None, industry: str | None = None) -> str | None:
    """The model's audience label, or None when it breaks any explanation rule."""
    if not isinstance(value, str):
        return None
    label = _prepared(value, MAX_LABEL_CHARS)
    if not label or _pattern_reason(label):
        return None
    if check_claim_safety(label, industry).risk_level in (ClaimRisk.HIGH, ClaimRisk.BLOCKED):
        return None
    return label


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
        k: _prepared(v, MAX_FIELD_CHARS) if isinstance(v := output.get(k), str) else ""
        for k in _TEXT_FIELDS
    }
    raw_angles = [a for a in (output.get("suggested_angles") or []) if isinstance(a, str)]
    angles = [_prepared(a, MAX_ANGLE_CHARS) for a in raw_angles[:MAX_ANGLES]]
    everything = "\n".join([*texts.values(), *angles])

    reason = _pattern_reason(everything)
    if reason:
        return ScreenedExplanation(None, [], reason)

    safety = check_claim_safety(everything, industry)
    if safety.risk_level in (ClaimRisk.HIGH, ClaimRisk.BLOCKED):
        warnings = [f"AI explanation withheld: {w}" for w in safety.warnings]
        return ScreenedExplanation(None, warnings, f"claim_risk_{safety.risk_level.value}")

    screened = dict(texts)  # already normalised and capped by _prepared
    screened["ai_inference"] = screened["ai_inference"] or None
    screened["suggested_angles"] = angles
    warnings = [f"AI explanation: {w}" for w in safety.warnings]
    return ScreenedExplanation(screened, warnings, None)
