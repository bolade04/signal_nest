"""Whole-identifier matching for erasure and its verification (``P6-PRIV-1``).

One matcher serves both :mod:`app.privacy.erasure` (what a deletion clears) and
:mod:`app.privacy.verification` (what remains afterwards). The verifier therefore can never call
a record clean that still holds a value the deletion should have cleared. Every occurrence of an
identifier is classified as one :class:`Mention`.

**Where an address can occur, in any form.** A writer may have stored the subject's address in
several forms: Unicode NFC or NFD; any letter case, non-ASCII letters included; inside JSON
``\\uXXXX`` escapes; percent-encoded; or with its domain as the IDNA A-label (``xn--…``) instead
of the U-label the product stores. Two steps cover every form:

* **The SQL pre-filter** (:func:`prefilter`) selects every row that may hold one of the forms.
  It does not rely on any dialect's ``lower()`` for non-ASCII letters (SQLite's is ASCII-only)
  or on a JSON escape's spelling (``\\u00c9`` versus ``\\u00e9``). For each generated form
  (:func:`address_forms` × :func:`encoded_forms`) it requires only that form's runs of ASCII
  characters that no percent-encoder or JSON writer rewrites (``[A-Za-z0-9._~-]``), compared in
  ASCII lower case. Every form of the address contains those runs verbatim.
* **The confirmation** (:func:`mention`) reads each candidate text under four views: as
  stored, with JSON ``\\uXXXX`` escapes decoded, percent-decoded, and both. Each view is
  normalized to NFC and fully case-folded. It then finds the address's own forms (U-label and
  A-label domain) in every view.

**How an occurrence is judged.** It is judged by the product's own address grammar, the one
every stored address passed. ``RegisterRequest.email`` is pydantic ``EmailStr``, which strips
the value and calls ``email_validator.validate_email(value, check_deliverability=False)``
(:func:`registrable`). This rule is unchanged. The residual-ambiguity policy belongs to the
operator (``D-PRIV-7``).

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
  destroy another person's data. The verifier reports it apart from its findings (``needs
  review``) for an operator.
* **Undecided, flagged ``AMBIGUOUS``**: no form of the address occurs, yet its local part and
  its domain both stand as separate tokens in the same text, as in ``a&#64;b.co`` or
  ``a [at] b.co``. The matcher cannot tell whether that is the address, so it flags it rather
  than pass it.

Whitespace and ``@`` are never part of an unquoted address, so the search for a longer address
stops there. Its bounds are search limits, not validity rules (see ``_LOCAL_MAX``). The
classification assumes the subject's address is itself registrable; every address the product
stores is.

**Opaque ids** (account, organization, workspace) are ``WHOLE`` when no id character
(``[A-Za-z0-9_-]``) touches them on either side, and ``AMBIGUOUS`` otherwise.

**Not detected (recorded limits).** These are transformations that keep neither the address nor
both its local part and its domain as text: a hash or base64 of it, or the domain spelled out
(``b [dot] co``). No product writer produces any of them.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from typing import Any
from urllib.parse import quote, unquote

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import Text, and_, cast, func, or_, true

#: How far the search for a longer address reaches on each side. These are search bounds, not
#: validity rules: the validator accepts longer local parts than RFC 5321's 64 characters, but
#: any registrable longer address implies a registrable extension of one or two characters
#: (an ``atext`` character, or a dot and one character), which the search always reaches first.
_LOCAL_MAX, _DOMAIN_MAX = 64, 255
_ID_CHAR = re.compile(r"[A-Za-z0-9_-]")
#: Characters no percent-encoder or JSON writer rewrites (RFC 3986 "unreserved").
_UNRESERVED_RUN = re.compile(r"[A-Za-z0-9._~-]+")
#: A JSON string escape: a UTF-16 surrogate pair, a ``\\uXXXX`` escape, or a one-character one.
_JSON_ESCAPE = re.compile(
    r"\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
    r"|\\u([0-9a-fA-F]{4})"
    r"|\\([\"\\/bfnrt])"
)
_JSON_SHORT = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
#: How many layers of JSON escaping the confirmation undoes (a value escaped inside JSON text).
_ESCAPE_LAYERS = 3
#: Characters that would continue a local part or an address around a token.
_LOCAL_TOUCH = re.compile(r"[\w.+-]")
_DOMAIN_BEFORE = re.compile(r"[\w.@-]")
_DOMAIN_AFTER = re.compile(r"[\w-]")


class Mention(IntEnum):
    """How a text holds an identifier; the strongest occurrence decides."""

    NONE = 0
    AMBIGUOUS = 1
    WHOLE = 2


# --------------------------------------------------------------------------- #
# The forms of an address
# --------------------------------------------------------------------------- #
def _canonical(text: str) -> str:
    """NFC, fully case-folded (non-ASCII letters included), NFC again."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


@dataclass(frozen=True)
class _Parts:
    local: str  # canonical
    domains: tuple[str, ...]  # canonical U-label, then A-label when it differs

    @property
    def forms(self) -> tuple[str, ...]:
        return tuple(f"{self.local}@{d}" for d in self.domains)


@lru_cache(maxsize=1024)
def _parts(address: str) -> _Parts:
    local, _, domain = address.rpartition("@")
    domains = [domain]
    try:
        parsed = validate_email(address, check_deliverability=False)
    except EmailNotValidError:
        pass
    else:
        local, domains = parsed.local_part, [parsed.domain, parsed.ascii_domain or parsed.domain]
    canonical = [_canonical(d) for d in domains]
    return _Parts(_canonical(local), tuple(dict.fromkeys(canonical)))


def address_forms(address: str) -> tuple[str, ...]:
    """The textual forms of ``address`` a writer may have produced: the local part in NFC and NFD,
    the domain as U-label (NFC and NFD) and as IDNA A-label. Letter case is left to the matcher,
    which case-folds, and to :func:`prefilter`, which compares ASCII only."""
    try:
        parsed = validate_email(address, check_deliverability=False)
        local, domains = parsed.local_part, [parsed.domain, parsed.ascii_domain or parsed.domain]
    except EmailNotValidError:
        local, _, domain = address.rpartition("@")
        domains = [domain]
    out = [
        f"{loc}@{dom}"
        for loc in (unicodedata.normalize("NFC", local), unicodedata.normalize("NFD", local))
        for d in domains
        for dom in (unicodedata.normalize("NFC", d), unicodedata.normalize("NFD", d))
    ]
    return tuple(dict.fromkeys(out))


def encoded_forms(form: str) -> tuple[str, ...]:
    """``form`` as stored text, JSON-escaped (``\\uXXXX``) and percent-encoded (all reserved
    characters, or all but ``@``)."""
    out = [form, json.dumps(form)[1:-1], quote(form, safe=""), quote(form, safe="@")]
    return tuple(dict.fromkeys(out))


def _runs(text: str) -> tuple[str, ...]:
    return tuple(r.lower() for r in _UNRESERVED_RUN.findall(text) if len(r) > 1)


def _contains_all(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
    return all(any(s in fragment for fragment in longer) for s in shorter)


@dataclass(frozen=True)
class Subject:
    """The identifiers to look for: opaque ids, and email addresses."""

    ids: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.ids or self.addresses)

    def sql_terms(self) -> tuple[tuple[str, ...], ...]:
        """For the SQL pre-filter: alternatives (OR) of fragments that must all occur (AND),
        each lower-case ASCII. A row holding any form of any identifier satisfies one of them.
        A term stricter than another (it needs everything the other needs) is dropped."""
        terms: list[tuple[str, ...]] = [(i.lower(),) for i in self.ids]
        for address in self.addresses:
            for form in address_forms(address):
                terms += [_runs(e) for e in encoded_forms(form)]
        kept: list[tuple[str, ...]] = []
        # Least restrictive first, so of two terms that imply each other one is always kept.
        for term in sorted(dict.fromkeys(terms), key=lambda t: (len(t), sum(map(len, t)), t)):
            if not any(_contains_all(term, other) for other in kept):
                kept.append(term)
        return tuple(kept)


def prefilter(column: Any, subject: Subject) -> Any:
    """A SQL condition true for every row whose ``column`` may hold one of ``subject``'s
    identifiers in any form (a superset; :func:`mention` decides). Only ASCII is lower-cased."""
    text = func.lower(cast(column, Text))
    alternatives = [
        and_(*(text.contains(f, autoescape=True) for f in term)) if term else true()
        for term in subject.sql_terms()
    ]
    return or_(*alternatives)


def same_address(value: str | None, address: str) -> bool:
    """Whether ``value`` is ``address`` itself in any form (case, NFC/NFD, U-/A-label domain)."""
    if not value:
        return False
    theirs, ours = _parts(value.strip()), _parts(address)
    return theirs.local == ours.local and bool(set(theirs.domains) & set(ours.domains))


# --------------------------------------------------------------------------- #
# The grammar
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# The confirmation
# --------------------------------------------------------------------------- #
def _unescape_json(text: str) -> str:
    """One layer of JSON string escapes decoded; a lone surrogate escape is kept as written."""

    def decode(match: re.Match[str]) -> str:
        if match.group(4):
            return _JSON_SHORT.get(match.group(4), match.group(4))
        if match.group(3):
            code = int(match.group(3), 16)
            return match.group(0) if 0xD800 <= code <= 0xDFFF else chr(code)
        high, low = int(match.group(1), 16), int(match.group(2), 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))

    return _JSON_ESCAPE.sub(decode, text)


def _views(text: str) -> tuple[str, ...]:
    """``text`` as stored and with up to three layers of JSON escapes undone, each also
    percent-decoded; every view canonical (NFC, case-folded)."""
    layers = [text]
    for _ in range(_ESCAPE_LAYERS):
        unescaped = _unescape_json(layers[-1])
        if unescaped == layers[-1]:
            break
        layers.append(unescaped)
    views = [view for layer in layers for view in (layer, unquote(layer))]
    return tuple(dict.fromkeys(_canonical(v) for v in views))


def _token(view: str, part: str, *, before: re.Pattern[str], after: re.Pattern[str]) -> bool:
    for match in re.finditer(re.escape(part), view):
        s, e = match.start(), match.end()
        if s and before.match(view[s - 1]):
            continue
        if e < len(view) and (
            after.match(view[e]) or (view[e] == "." and e + 1 < len(view) and view[e + 1].isalnum())
        ):
            continue
        return True
    return False


def _undecided(view: str, parts: _Parts) -> bool:
    """The local part and the domain stand as separate tokens, but the address does not occur."""
    local_after = re.compile(r"[\w.+@-]")
    return _token(view, parts.local, before=_LOCAL_TOUCH, after=local_after) and any(
        _token(view, d, before=_DOMAIN_BEFORE, after=_DOMAIN_AFTER) for d in parts.domains
    )


def mention(text: str, subject: Subject) -> Mention:
    """How ``text`` holds any of ``subject``'s identifiers, in any of their forms."""
    best = Mention.NONE
    views = _views(text)
    for value in subject.ids:
        needle = _canonical(value)
        for view in views:
            for match in re.finditer(re.escape(needle), view):
                best = max(best, _id_mention(view, match.start(), match.end()))
                if best is Mention.WHOLE:
                    return best
    for address in subject.addresses:
        parts = _parts(address)
        found = False
        for view in views:
            for form in parts.forms:
                for match in re.finditer(re.escape(form), view):
                    found = True
                    best = max(best, _address_mention(view, match.start(), match.end()))
                    if best is Mention.WHOLE:
                        return best
        if not found and any(_undecided(view, parts) for view in views):
            best = max(best, Mention.AMBIGUOUS)
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


__all__ = [
    "Mention",
    "Subject",
    "address_forms",
    "encoded_forms",
    "mention",
    "mention_in_json",
    "prefilter",
    "registrable",
    "same_address",
]
