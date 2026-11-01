"""Untrusted text -> prompt data (P6-LLM-2).

Every value interpolated into a prompt is treated as data, never as instructions:
scraped third-party content, tenant-entered context (audiences, positioning) and
anything derived from earlier model output (an audience label, a pain-point label)
alike - a classifier that read an injected post can carry the injection forward.

:func:`data_block` turns one value into a delimited block:

1. Unicode-normalised (NFKC: fullwidth and compatibility forms such as ``＜`` or
   ``．`` become their ASCII equivalents), stripped of invisible characters
   (category ``Cf`` - bidi overrides, zero-width characters, tag characters - and
   every ``Default_Ignorable_Code_Point``, e.g. variation selectors), and with
   whitespace runs collapsed to one space, so hidden text cannot ride along and
   look-alike or re-spaced text cannot dodge step 2;
2. control characters stripped and known injection phrases defanged by
   :func:`app.intelligence.extraction.sanitize_text` (``[quoted:...]``), applied
   to the normalised text so a zero-width character cannot split a phrase;
3. capped at ``max_chars`` with an explicit truncation marker;
4. ``&``, ``<`` and ``>`` escaped, so the value can never close its own block or
   open a fake one;
5. wrapped in ``<untrusted_data name="...">`` ... ``</untrusted_data>``.

The system prompt (``providers_real.SYSTEM_PROMPT``) states the instruction
hierarchy: only the system message and the task text are instructions; nothing
inside an ``untrusted_data`` block is. Delimiting does not make injection
impossible - it makes it detectable and gives the model an unambiguous rule - so
model output is screened again before persistence (``app.llm.screening``).

Two layers (Phase 5A). :func:`sanitize_inputs` is applied by
:meth:`app.llm.service.LLMService.run` to every model input before any provider sees
it - the mock included - recursively over nested lists, tuples, sets and dicts: control
characters stripped and injection markers defanged (:func:`defang`, the
``sanitize_text`` rule). It changes nothing in well-formed text: no normalisation, no
whitespace collapsing, no trimming. The real adapters then build each prompt block with
:func:`data_block` (steps 1-5 above), which is the stronger layer.

:func:`defang` is :func:`app.intelligence.extraction.sanitize_text` made idempotent: a
marker that is already quoted (``[quoted:<marker>]``) is left as it is, so the boundary
pass and the block pass never quote a marker twice.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.intelligence.extraction import _CONTROL_RE, _INJECTION_RE

TRUNCATION_MARKER = " [truncated]"

#: A marker already defanged by an earlier pass (first alternative, kept as it is), or a
#: live marker (second alternative, quoted). One left-to-right scan, so a quoted marker is
#: consumed whole and the marker inside it is never matched again.
_DEFANG_RE = re.compile(
    r"\[quoted:(?:" + _INJECTION_RE.pattern + r")\]|(?:" + _INJECTION_RE.pattern + r")",
    re.IGNORECASE,
)


def _quote(match: re.Match[str]) -> str:
    found = match.group(0)
    return found if found.startswith("[") else f"[quoted:{found}]"


def defang(text: str, *, strip: bool = True) -> str:
    """``sanitize_text``, idempotent: control characters become spaces and every live
    injection marker is quoted (``[quoted:...]``); an already-quoted marker is kept.

    For text holding no ``[quoted:<marker>]`` it returns exactly what
    :func:`app.intelligence.extraction.sanitize_text` returns (``strip=True``).
    ``strip=False`` keeps surrounding whitespace (the service boundary changes nothing in
    well-formed text).
    """
    out = _DEFANG_RE.sub(_quote, _CONTROL_RE.sub(" ", text))
    return out.strip() if strip else out


def sanitize_inputs(value: Any) -> Any:
    """Every string in ``value`` defanged, recursively (the service boundary, Phase 5A).

    Lists, tuples, sets and frozensets come back as that base type; dicts keep their keys at the top
    level (task field names chosen by code) and have nested string keys defanged too;
    ``None``, booleans and numbers pass unchanged. Any other object is turned into its
    defanged ``str()``, so no custom ``__str__`` can carry raw text past this point.
    """
    return _sanitize(value, top_level=True)


def _sanitize(value: Any, *, top_level: bool = False) -> Any:
    if isinstance(value, str):
        return defang(str(value), strip=False)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, dict):
        return {
            (k if top_level or not isinstance(k, str) else _sanitize(k)): _sanitize(v)
            for k, v in value.items()
        }
    for container in (list, tuple, frozenset, set):
        if isinstance(value, container):
            return container(_sanitize(v) for v in value)
    return defang(str(value), strip=False)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    return str(value)


#: Unicode ``Default_Ignorable_Code_Point`` (DerivedCoreProperties.txt), the
#: code points that render as nothing: soft hyphen, combining grapheme joiner,
#: Hangul fillers, variation selectors, tag characters and the rest. ``Cf`` alone
#: misses several of them (U+034F, U+115F, U+3164, U+FE00-FE0F, U+FFA0, ...).
_DEFAULT_IGNORABLE = re.compile(
    "[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_WHITESPACE_RUN = re.compile(r"\s+")


def _strip_format_characters(text: str) -> str:
    text = _DEFAULT_IGNORABLE.sub("", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def normalise(text: str) -> str:
    """NFKC, then without invisible characters (``Cf`` and every Unicode
    ``Default_Ignorable_Code_Point``). Stripping can expose new compatibility
    sequences, so NFKC runs again (idempotent on ordinary text)."""
    stripped = _strip_format_characters(unicodedata.normalize("NFKC", text))
    return unicodedata.normalize("NFKC", stripped)


def clean(value: Any, max_chars: int) -> str:
    """Normalised, sanitized, length-capped text (not yet escaped). Whitespace runs,
    newlines included, become one space before defanging, so a doubled space or a
    line break cannot split an injection phrase."""
    text = defang(_WHITESPACE_RUN.sub(" ", normalise(as_text(value))))
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATION_MARKER
    return text


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def data_block(name: str, value: Any, max_chars: int) -> str:
    return f'<untrusted_data name="{name}">\n{_escape(clean(value, max_chars))}\n</untrusted_data>'
