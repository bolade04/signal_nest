"""Untrusted text -> prompt data (P6-LLM-2).

Every value interpolated into a prompt is treated as data, never as instructions:
scraped third-party content, tenant-entered context (audiences, positioning) and
anything derived from earlier model output (an audience label, a pain-point label)
alike - a classifier that read an injected post can carry the injection forward.

:func:`data_block` turns one value into a delimited block:

1. Unicode-normalised (NFKC: fullwidth and compatibility forms such as ``＜`` or
   ``．`` become their ASCII equivalents) and stripped of Unicode format characters
   (category ``Cf``: bidi overrides, zero-width characters, tag characters), so
   hidden text cannot ride along and look-alike text cannot dodge step 2;
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
"""

from __future__ import annotations

import unicodedata
from typing import Any

from app.intelligence.extraction import sanitize_text

TRUNCATION_MARKER = " [truncated]"


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    return str(value)


def _strip_format_characters(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def normalise(text: str) -> str:
    """NFKC, then without Unicode format (``Cf``) characters. Stripping can expose new
    compatibility sequences, so NFKC runs again (idempotent on ordinary text)."""
    stripped = _strip_format_characters(unicodedata.normalize("NFKC", text))
    return unicodedata.normalize("NFKC", stripped)


def clean(value: Any, max_chars: int) -> str:
    """Normalised, sanitized, length-capped text (not yet escaped)."""
    text = sanitize_text(normalise(as_text(value)))
    if len(text) > max_chars:
        text = text[:max_chars] + TRUNCATION_MARKER
    return text


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def data_block(name: str, value: Any, max_chars: int) -> str:
    return f'<untrusted_data name="{name}">\n{_escape(clean(value, max_chars))}\n</untrusted_data>'
