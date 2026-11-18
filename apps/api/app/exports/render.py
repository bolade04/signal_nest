"""Pure renderers for audited manual export (Project Phase 5E, lane 5E-W1).

Each renderer turns the stored text of one approved draft version into one closed
:class:`~app.core.enums.ExportFormat`. They are pure functions of an :class:`ExportSource`:
no database read, no I/O, no model call, no signed URL and no file store (5E plan §2). What
they render is the draft's persisted text exactly as it was reviewed and approved; nothing is
added to it, reworded or translated.

* ``text/plain``: the headline, the body and the call to action, each present one separated
  from the next by one blank line. The text is unchanged.
* ``text/markdown``: the headline as a level-one heading, then the body, then the call to
  action, one block each. Every character of the text that Markdown would read as syntax is
  backslash-escaped (CommonMark), so the rendered document shows the approved words and
  nothing else: no emphasis, link, image, list, heading, table, autolink, entity or raw HTML
  can come from the text. That is CommonMark's reading, where an autolink needs the angle
  brackets this escapes. A bare URL in the approved text (``https://…`` or ``www.…``) is left
  as written, and a renderer with GitHub Flavored Markdown's autolink extension links it, to
  the address the approved text itself shows. A line break inside a block is kept as a hard
  line break.
  Indentation at the start of a line is not carried over (four spaces would turn a line
  into a code block, which shows backslash escapes literally); a headline is one line.
* ``json``: one object, keys sorted, indented by two spaces, non-ASCII kept as written:
  ``draft_id``, ``draft_version``, ``channel``, ``jurisdiction_code``, ``is_simulated``,
  ``headline``, ``body`` and ``call_to_action`` (``null`` when the draft has none).

No renderer appends a trailing newline.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.core.enums import ExportFormat


@dataclass(frozen=True, slots=True)
class ExportSource:
    """The stored draft fields an export renders (read from the draft row by the service)."""

    draft_id: str
    draft_version: int
    channel: str
    jurisdiction_code: str
    is_simulated: bool
    headline: str | None
    body: str
    call_to_action: str | None


def _parts(source: ExportSource) -> list[str]:
    return [t for t in (source.headline, source.body, source.call_to_action) if t]


def render_plain(source: ExportSource) -> str:
    """The text blocks, separated by one blank line, unchanged."""
    return "\n\n".join(_parts(source))


#: Characters that can open or close Markdown syntax anywhere in a line: escape, code,
#: emphasis, strikethrough, link/image brackets, autolinks and raw HTML, tables, entities
#: and ATX headings. CommonMark allows a backslash before any ASCII punctuation.
_MD_INLINE = re.compile(r"([\\`*_~\[\]<>|&#])")
#: Syntax that only a line's first characters open: list markers, block quotes (already
#: escaped above), setext underlines and thematic breaks, and ordered-list numbers.
_MD_LINE_START = re.compile(r"^([-+=])|^(\d+)([.)])")


def _escape_md_line(line: str) -> str:
    line = _MD_INLINE.sub(r"\\\1", line.strip())
    return _MD_LINE_START.sub(
        lambda m: f"\\{m.group(1)}" if m.group(1) else f"{m.group(2)}\\{m.group(3)}", line
    )


def _md_block(text: str) -> str:
    """One block of text, escaped, its line breaks kept as hard breaks (a trailing ``\\``)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_escape_md_line(line) for line in text.split("\n")]
    out: list[str] = []
    for i, line in enumerate(lines):
        following = lines[i + 1] if i + 1 < len(lines) else ""
        out.append(f"{line}\\" if line and following else line)
    return "\n".join(out).strip("\n")


def render_markdown(source: ExportSource) -> str:
    """A level-one heading for the headline, then the body and the call to action."""
    blocks: list[str] = []
    if source.headline:
        heading = " ".join(source.headline.split())
        blocks.append(f"# {_escape_md_line(heading)}")
    blocks.append(_md_block(source.body))
    if source.call_to_action:
        blocks.append(_md_block(source.call_to_action))
    return "\n\n".join(b for b in blocks if b)


def render_json(source: ExportSource) -> str:
    """One sorted, indented JSON object of the draft's identity and text."""
    document = {
        "draft_id": source.draft_id,
        "draft_version": source.draft_version,
        "channel": source.channel,
        "jurisdiction_code": source.jurisdiction_code,
        "is_simulated": source.is_simulated,
        "headline": source.headline,
        "body": source.body,
        "call_to_action": source.call_to_action,
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)


#: Exactly one renderer per closed format (checked when the module is imported).
RENDERERS: dict[ExportFormat, Callable[[ExportSource], str]] = {
    ExportFormat.TEXT_PLAIN: render_plain,
    ExportFormat.TEXT_MARKDOWN: render_markdown,
    ExportFormat.JSON: render_json,
}
if set(RENDERERS) != set(ExportFormat):  # pragma: no cover - a new format needs a renderer
    raise RuntimeError("every ExportFormat needs exactly one renderer")


def render_export(source: ExportSource, export_format: ExportFormat) -> str:
    """Render ``source`` in ``export_format`` (a member of the closed set)."""
    return RENDERERS[ExportFormat(export_format)](source)


__all__ = [
    "RENDERERS",
    "ExportSource",
    "render_export",
    "render_json",
    "render_markdown",
    "render_plain",
]
