"""Whole-identifier matching for erasure and its verification (``P6-PRIV-1``).

One matcher serves both :mod:`app.privacy.erasure` (what a deletion clears) and
:mod:`app.privacy.verification` (what remains afterwards), so the verifier can never call a
record clean that still holds a value the deletion should have cleared. Every occurrence of an
identifier (in any letter case) is classified as one :class:`Mention`:

**Email addresses** are judged by the product's own address grammar, the one every stored
address passed: ``RegisterRequest.email`` is pydantic ``EmailStr``, which strips the value and
calls ``email_validator.validate_email(value, check_deliverability=False)``
(:func:`registrable`).

* ``WHOLE``: no registrable address contains the occurrence as part of a longer one. The
  characters before it cannot extend its local part, and the characters after it cannot extend
  its domain, into an address that grammar accepts. Examples: ``mailto:a@b.co``,
  ``<a@b.co>``, ``"a@b.co"``, ``(a@b.co)``, a sentence-ending dot, ``a@b.co_x``. The
  occurrence names the subject: erasure clears it and the verifier counts it as a finding.
* ``AMBIGUOUS``: some registrable address does contain it. Other people's addresses fall here,
  such as ``o'a@b.co``, ``xa@b.co``, ``ía@b.co`` and ``a@b.co.org``. So does the subject's own
  address in contexts such as ``email=a@b.co``, ``users/a@b.co``, ``#a@b.co``, ``{a@b.co}``
  and ``a@b.co.Word``, because ``=``, ``/``, ``#``, ``{``, the apostrophes ``'`` and ``’``,
  ``«`` and a following ``.Word`` can all be part of a registrable address. The grammar cannot
  tell these cases apart. Erasure therefore leaves the occurrence, because clearing it could
  destroy another person's data, and the verifier reports it apart from its findings for an
  operator to review.

Whitespace and ``@`` are never part of an unquoted address, so the search for a longer address
stops there and is bounded by the address length limits. The classification assumes the
subject's address is itself registrable; every address the product stores is. Opaque ids
(account, organization, workspace) are ``WHOLE`` when no id character (``[A-Za-z0-9_-]``)
touches them on either side, and ``AMBIGUOUS`` otherwise.

Not found at all, which are recorded limits: a URL-encoded address (``a%40b.co``), and an
address in a different Unicode normalization form (NFD) than the stored one (NFC).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from typing import Any

from email_validator import EmailNotValidError, validate_email

#: The longest local part and the longest domain an address can have (RFC 5321).
_LOCAL_MAX, _DOMAIN_MAX = 64, 255
_ID_CHAR = re.compile(r"[A-Za-z0-9_-]")


class Mention(IntEnum):
    """How a text holds an identifier; the strongest occurrence decides."""

    NONE = 0
    AMBIGUOUS = 1
    WHOLE = 2


@dataclass(frozen=True)
class Subject:
    """The identifiers to look for: opaque ids, and email addresses."""

    ids: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.ids or self.addresses)

    def sql_needles(self) -> tuple[str, ...]:
        """Lower-cased substrings for a SQL pre-filter: each identifier as text and, when it is
        not ASCII, as JSON-escaped text. A pre-filter only narrows; :func:`mention` decides."""
        out: list[str] = []
        for value in (*self.ids, *self.addresses):
            lowered = value.lower()
            escaped = json.dumps(lowered)[1:-1]
            out += [lowered] if escaped == lowered else [lowered, escaped]
        return tuple(dict.fromkeys(out))


@lru_cache(maxsize=8192)
def registrable(candidate: str) -> bool:
    """Whether the product would accept ``candidate`` as an account's address (unstripped)."""
    try:
        validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        return False
    return True


def _extends(char: str) -> bool:
    """Whether ``char`` might continue an address; :func:`registrable` decides for sure."""
    return not char.isspace() and char != "@"


def _address_mention(text: str, start: int, end: int) -> Mention:
    left = start
    while left > 0 and start - left < _LOCAL_MAX and _extends(text[left - 1]):
        left -= 1
    if any(registrable(text[i:end]) for i in range(start - 1, left - 1, -1)):
        return Mention.AMBIGUOUS
    right = end
    while right < len(text) and right - end < _DOMAIN_MAX and _extends(text[right]):
        right += 1
    if any(registrable(text[start:j]) for j in range(end + 1, right + 1)):
        return Mention.AMBIGUOUS
    return Mention.WHOLE


def _id_mention(text: str, start: int, end: int) -> Mention:
    touched = (start > 0 and _ID_CHAR.match(text[start - 1])) or (
        end < len(text) and _ID_CHAR.match(text[end])
    )
    return Mention.AMBIGUOUS if touched else Mention.WHOLE


def _occurrences(text: str, value: str) -> Iterable[re.Match[str]]:
    return re.finditer(re.escape(value), text, re.IGNORECASE)


def mention(text: str, subject: Subject) -> Mention:
    """How ``text`` holds any of ``subject``'s identifiers."""
    best = Mention.NONE
    for kinds, classify in ((subject.ids, _id_mention), (subject.addresses, _address_mention)):
        for value in kinds:
            for match in _occurrences(text, value):
                best = max(best, classify(text, match.start(), match.end()))
                if best is Mention.WHOLE:
                    return best
    return best


def mention_in_json(value: Any, subject: Subject) -> Mention:
    """:func:`mention` over every string of a decoded JSON value, object keys included."""
    if isinstance(value, str):
        return mention(value, subject)
    best = Mention.NONE
    if isinstance(value, dict):
        parts: Iterable[Any] = (*value.keys(), *value.values())
    elif isinstance(value, list):
        parts = value
    else:
        return best
    for part in parts:
        best = max(best, mention_in_json(part, subject))
        if best is Mention.WHOLE:
            break
    return best


__all__ = ["Mention", "Subject", "mention", "mention_in_json", "registrable"]
