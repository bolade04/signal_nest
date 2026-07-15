"""Untrusted-content isolation for feed text (Phase 3B Batch 2).

Everything a connector reads from a feed — titles, descriptions, entry URLs — is
**untrusted**. Before any of it reaches downstream AI processing it is:

* **stripped to plain text** (no HTML markup, no scripts) and length-capped;
* **neutralized** against prompt injection — the text is treated purely as quoted
  data, never as instructions, and common instruction-injection markers are
  defanged so a feed can never redirect model behavior or connector policy;
* **labeled with provenance** so downstream code always knows the text is external,
  quoted source content — not a system or operator instruction.

This module performs no network I/O and holds no state. It never *executes* markup
or URLs; it only sanitizes and labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

#: Hard cap on any single text field handed downstream.
MAX_TEXT_CHARS = 4000

#: Phrases an injected feed might use to try to override instructions. These are
#: defanged (not removed silently) so the text stays readable but inert.
_INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "system prompt",
    "you are now",
    "act as",
    "developer mode",
    "override your",
)


class _TextExtractor(HTMLParser):
    """Collect only text nodes; drop all tags, attributes, scripts and styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppress = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self._suppress = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._suppress = False

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(raw: str) -> str:
    """Return the visible text of ``raw`` with all markup removed."""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # Malformed markup is untrusted input, not an error: fall back to a
        # conservative tag strip rather than raising.
        return re.sub(r"<[^>]*>", " ", raw)
    return parser.text()


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def neutralize_injection(text: str) -> str:
    """Defang instruction-injection markers so feed text stays inert data.

    Markers are wrapped in brackets rather than deleted, keeping the text readable
    for humans while ensuring it cannot read as an imperative to a model.
    """
    out = text
    for marker in _INJECTION_MARKERS:
        out = re.sub(
            re.escape(marker),
            lambda m: f"[quoted:{m.group(0)}]",
            out,
            flags=re.IGNORECASE,
        )
    return out


def sanitize_text(raw: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Full pipeline: strip markup → collapse whitespace → cap → neutralize."""
    text = _collapse_whitespace(strip_html(raw))
    if len(text) > max_chars:
        text = text[:max_chars]
    return neutralize_injection(text)


def is_safe_entry_url(url: str | None) -> bool:
    """True when a feed entry URL is a plain http(s) link safe to store/display.

    Rejects ``javascript:``, ``data:``, ``file:`` and other non-web schemes and any
    URL carrying embedded credentials. This gates URLs *stored on a signal*; it is
    independent of the fetch allowlist (which is far stricter).
    """
    if not url:
        return False
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.username or parts.password or "@" in parts.netloc:
        return False
    return bool(parts.hostname)


@dataclass(frozen=True)
class QuotedContent:
    """Feed text packaged as clearly-labeled, untrusted quoted data.

    Downstream code should render/consume ``text`` only inside the ``provenance``
    frame, never as a trusted instruction.
    """

    text: str
    source_id: str
    is_untrusted: bool = True
    provenance: str = "external_feed_quoted_content"


def quote_for_ai(raw: str, *, source_id: str, max_chars: int = MAX_TEXT_CHARS) -> QuotedContent:
    """Sanitize ``raw`` and wrap it as labeled untrusted quoted content."""
    return QuotedContent(text=sanitize_text(raw, max_chars=max_chars), source_id=source_id)
