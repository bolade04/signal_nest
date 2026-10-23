"""Whole-identifier matching for erasure and its verification (``P6-PRIV-1``).

One matcher serves both :mod:`app.privacy.erasure` (what a deletion clears) and
:mod:`app.privacy.verification` (what remains afterwards). The verifier therefore can never call
a record clean that still holds a value the deletion should have cleared. Every occurrence of an
identifier is classified as one :class:`Mention`.

**Where an address can occur, in any form.** A writer may have stored the subject's address in
several forms: Unicode NFC or NFD; any letter case, non-ASCII letters included; inside JSON
``\\uXXXX`` escapes; percent-encoded; or with its domain as the IDNA A-label (``xn--…``) instead
of the U-label the product stores.

**What counts as the same address.** Registration's own rule decides (measured, fix pass 4). It
stores the NFC form, the U-label domain and a lower-cased domain, so those spellings are one
address to it and to this matcher. It keeps ``straße``/``strasse``, ``weiß``/``weiss``, ``ſ``/``s``,
``ﬃ``/``ffi`` and ``ς``/``σ`` apart as distinct accounts. So does this matcher, which lower-cases
(``str.lower()``) rather than case-folds (``str.casefold()`` would join each pair). The local
part's letter case is the one exception: registration keeps ``Kate`` and ``kate`` apart, but
erasure and export match an account's own address in any letter case, gated on its verification
(a recorded policy). Two steps cover every form:

* **The SQL pre-filter** (:func:`prefilter`) selects every row that may hold one of the forms.
  It does not rely on any dialect's ``lower()`` for non-ASCII letters (SQLite's is ASCII-only)
  or on a JSON escape's spelling (``\\u00c9`` versus ``\\u00e9``). For each generated form
  (:func:`address_forms` × :func:`encoded_forms`) it requires only that form's runs of ASCII
  characters that no percent-encoder or JSON writer rewrites (``[A-Za-z0-9._~-]``), compared in
  ASCII lower case. Every form of the address contains those runs verbatim. An address whose
  local part has no such run of two characters (``иван@пример.рф``, ``a@b.co``) has no selective
  fragment. In a request its whole forms are matched instead, in lower, upper and capitalised
  case, raw and encoded. That keeps the request's work bounded, but other mixed casings of a
  non-ASCII local part are then not selected on SQLite, nor in a JSON column on either engine.
  The operator's verification (``exhaustive=True``) reads every row for such an address, so it
  sees them.
* **The confirmation** (:func:`mention`) reads each candidate text under four views: as
  stored, with JSON ``\\uXXXX`` escapes decoded, percent-decoded, and both. Each view is
  normalized to NFC and lower-cased. It then finds the address's own forms (U-label and
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
* **Undecided, flagged ``AMBIGUOUS``**: no form of the address occurs, but its local part and
  its domain are joined by an explicit separator written instead of ``@``: ``[at]``, ``(at)``,
  ``{at}``, ``<at>``, ``[@]``, ``(@)``, ``&#64;``, ``&#x40;``, ``&commat;``, or ``at`` / ``@``
  between spaces. Examples are ``a&#64;b.co`` and ``a [at] b.co``. The matcher cannot tell
  whether that is the address, so it flags it rather than pass it. Mere co-occurrence is not
  flagged: a text naming ``John`` and ``acme.example`` separately is not about
  ``john@acme.example``.

Whitespace and ``@`` are never part of an unquoted address, so the search for a longer address
stops there. Its bounds are search limits, not validity rules (see ``_LOCAL_MAX``). The
classification assumes the subject's address is itself registrable; every address the product
stores is.

**Opaque ids** (account, organization, workspace) are ``WHOLE`` when no id character
(``[A-Za-z0-9_-]``) touches them on either side, and ``AMBIGUOUS`` otherwise.

**Not detected (recorded limits).** These are transformations that keep neither the address nor
its local part and domain joined by one of those separators: a hash or base64 of it, the domain
spelled out (``b [dot] co``), or a separator not in that list. No product writer produces any of
them; third-party or customer text can.
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
#: An explicit separator written between a local part and a domain instead of "@" (lower case:
#: the views are lower-cased). Mere co-occurrence of the two is never enough (review r4 R4-3).
_SEPARATOR = (
    r"(?:\s*(?:\[\s*(?:at|@)\s*\]|\(\s*(?:at|@)\s*\)|\{\s*(?:at|@)\s*\}|<\s*(?:at|@)\s*>"
    r"|&#0*64;|&#x0*40;|&commat;)\s*|\s+(?:at|@)\s+)"
)


class Mention(IntEnum):
    """How a text holds an identifier; the strongest occurrence decides."""

    NONE = 0
    AMBIGUOUS = 1
    WHOLE = 2


# --------------------------------------------------------------------------- #
# The forms of an address
# --------------------------------------------------------------------------- #
def _canonical(text: str) -> str:
    """NFC, lower-cased (non-ASCII letters included), NFC again.

    ``str.lower()``, never ``str.casefold()``: case-folding maps distinct letters together
    (``ß``→``ss``, ``ſ``→``s``, ``ﬃ``→``ffi``, ``ς``→``σ``), and registration keeps those
    addresses apart as distinct accounts (review r4 R4-1).
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).lower())


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
    which lower-cases, and to :func:`prefilter`, which compares ASCII only."""
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


def _ascii_lower(text: str) -> str:
    """What every SQL ``lower()`` does: ASCII letters only (SQLite stops there)."""
    return "".join(c.lower() if c.isascii() else c for c in text)


def _has_local_run(address: str) -> bool:
    """Whether the local part offers a selective ASCII fragment (a run of two or more)."""
    try:
        local = validate_email(address, check_deliverability=False).local_part
    except EmailNotValidError:
        local = address.rpartition("@")[0]
    return bool(_runs(local))


def _whole_needles(form: str) -> tuple[str, ...]:
    """Whole-form needles for an address without an ASCII fragment, as a SQL ``lower()`` sees
    them. Raw text in the casings a person types (local part lower, upper or capitalised; domain
    lower or upper). JSON-escaped with the domain lower-case, as every product writer stores it
    (``EmailStr`` lower-cases the domain). Percent-encoded in lower case. Each is one ``LIKE``
    per row, so the set is kept small. Other casings are left to the operator's exhaustive scan.
    """
    local, _, domain = form.rpartition("@")
    locals_ = (local.lower(), local.upper(), local.capitalize())
    raw = [f"{loc}@{dom}" for loc in locals_ for dom in (domain.lower(), domain.upper())]
    escaped = [json.dumps(f"{loc}@{domain.lower()}")[1:-1] for loc in locals_]
    lowered = form.lower()
    percent = [quote(lowered, safe=""), quote(lowered, safe="@")]
    return tuple(dict.fromkeys(_ascii_lower(n) for n in (*raw, *escaped, *percent)))


@dataclass(frozen=True)
class Subject:
    """The identifiers to look for: opaque ids, and email addresses."""

    ids: tuple[str, ...] = ()
    addresses: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.ids or self.addresses)

    def sql_terms(self, *, exhaustive: bool = False) -> tuple[tuple[str, ...], ...]:
        """For the SQL pre-filter: alternatives (OR) of fragments that must all occur (AND),
        compared after an ASCII-only ``lower()``. A row holding any form of any identifier
        satisfies one of them. A term stricter than another (it needs everything the other needs)
        is dropped.

        An address with no ASCII run in its local part gets its whole forms in typed casings
        instead of fragments, so a request never selects every row (review r4 R4-2). With
        ``exhaustive=True`` (the operator's verification) it gets the empty term, which is TRUE:
        every row is read."""
        terms: list[tuple[str, ...]] = [(i.lower(),) for i in self.ids]
        for address in self.addresses:
            if _has_local_run(address):
                for form in address_forms(address):
                    terms += [_runs(e) for e in encoded_forms(form)]
            elif exhaustive:
                terms.append(())
            else:
                for form in address_forms(address):
                    terms += [(needle,) for needle in _whole_needles(form)]
        kept: list[tuple[str, ...]] = []
        # Least restrictive first, so of two terms that imply each other one is always kept.
        for term in sorted(dict.fromkeys(terms), key=lambda t: (len(t), sum(map(len, t)), t)):
            if not any(_contains_all(term, other) for other in kept):
                kept.append(term)
        return tuple(kept)


def prefilter(column: Any, subject: Subject, *, exhaustive: bool = False) -> Any:
    """A SQL condition true for every row whose ``column`` may hold one of ``subject``'s
    identifiers in any form (a superset; :func:`mention` decides). Only ASCII is lower-cased.
    ``exhaustive`` is for the operator's verification, off the request path (see
    :meth:`Subject.sql_terms`)."""
    text = func.lower(cast(column, Text))
    alternatives = [
        and_(*(text.contains(f, autoescape=True) for f in term)) if term else true()
        for term in subject.sql_terms(exhaustive=exhaustive)
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
    percent-decoded; every view canonical (NFC, lower-cased)."""
    layers = [text]
    for _ in range(_ESCAPE_LAYERS):
        unescaped = _unescape_json(layers[-1])
        if unescaped == layers[-1]:
            break
        layers.append(unescaped)
    views = [view for layer in layers for view in (layer, unquote(layer))]
    return tuple(dict.fromkeys(_canonical(v) for v in views))


def _undecided(view: str, parts: _Parts) -> bool:
    """The local part and a domain form are joined by an explicit separator instead of "@"."""
    return any(
        re.search(
            r"(?<![\w.+-])" + re.escape(parts.local) + _SEPARATOR + re.escape(domain)
            + r"(?![\w-])(?!\.\w)",
            view,
        )
        for domain in parts.domains
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
