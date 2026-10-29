"""Untrusted feed text → plain, bounded, inert text (ported from draft PR #34).

Everything read from a feed — titles, descriptions, entry links — is untrusted. Before
it reaches a signal it is reduced to visible plain text (no markup, no script or style
bodies, no control characters), whitespace-collapsed and length-capped. The few
unambiguous instruction-override phrases a hostile feed might plant for a downstream
model are bracketed so they read as quoted data; ordinary prose is left alone (an
earlier list also bracketed phrases such as "act as", which corrupts real headlines).

Entry links are kept only when they are plain ``http(s)`` URLs without credentials;
anything else (``javascript:``, ``data:``, ``file:`` …) is dropped. Nothing here
performs I/O or follows a link.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

#: Hard cap on any single text field taken from a feed.
MAX_TEXT_CHARS = 4000
#: Hard cap on a stored entry URL.
MAX_URL_CHARS = 1000

_OVERRIDE_PHRASES = re.compile(
    r"(ignore (?:all )?previous instructions|disregard (?:all )?previous instructions"
    r"|system prompt)",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    """Collect text nodes only; drop tags, attributes, scripts and styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self._suppress += 1
        elif tag in {"br", "p", "div", "li"}:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._suppress:
            self._suppress -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(raw: str) -> str:
    """Visible text of ``raw`` with all markup removed."""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # malformed markup is untrusted input, not an error
        return re.sub(r"<[^>]*>", " ", raw)
    return parser.text()


def sanitize_text(raw: str | None, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Strip markup → drop control characters → collapse whitespace → cap → defang."""
    if not raw:
        return ""
    text = _WHITESPACE.sub(" ", _CONTROL.sub(" ", strip_html(raw))).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return _OVERRIDE_PHRASES.sub(lambda m: f"[quoted: {m.group(0)}]", text)


def safe_entry_url(url: str | None) -> str | None:
    """``url`` when it is a plain http(s) link without credentials; otherwise ``None``."""
    if not url:
        return None
    url = url.strip()
    if len(url) > MAX_URL_CHARS or _CONTROL.search(url) or " " in url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password or "@" in parts.netloc:
        return None
    return url
