"""Whole-identifier matching for erasure and its verification (``P6-PRIV-1``).

One matcher serves both :mod:`app.privacy.erasure` (what a deletion clears) and
:mod:`app.privacy.verification` (what remains afterwards). The verifier therefore can never call
a record clean that still holds a value the deletion should have cleared. Every occurrence of an
identifier is classified as one :class:`Mention`.

**Where an address can occur, in any form.** A writer may have stored the subject's address in
several forms:
- Unicode NFC or NFD;
- another letter case, non-ASCII letters included;
- inside JSON ``\\uXXXX`` escapes;
- percent-encoded;
- with its domain as the IDNA A-label (``xn--…``), or in a spelling registration's UTS46
  mapping turns into the stored U-label (fullwidth letters and stops, ``。``, mathematical
  letters).

**What counts as the same address.** Registration's own rule decides (measured, fix pass 4).
- **Same address.** Registration stores the NFC form and maps the domain through UTS46 to its
  lower-case U-label. So those spellings are one address to it, and to this matcher, which
  resolves a domain in text with the same mapping (:func:`_domain_of`).
- **Distinct accounts.** Registration keeps ``straße``/``strasse``, ``weiß``/``weiss``,
  ``ſ``/``s``, ``ﬃ``/``ffi`` and ``ς``/``σ`` apart. So does this matcher, which lower-cases
  letter by letter (``str.lower()``) rather than case-folding (``str.casefold()`` would join
  each pair).
- **The one exception: the local part's letter case.** Registration keeps ``Kate`` and ``kate``
  apart. Erasure and export still match an account's own address in another letter case, gated
  on its verification: a recorded engineering decision. Making account e-mail identity
  case-insensitive instead is left open as operator decisions packet row 5.1 (F2).
- **A letter case shared with another address decides only when that address exists** (reviews
  r5 R5-1; coordinator decision, fix pass 6).
  - Some upper-case spellings are shared by letters of different lower case
    (:func:`shares_upper_case`):
    ``ΣΣ`` is the upper case of ``σσ`` and of ``σς``; ``IA`` of ``ia`` and ``ıa``; ``JOSÉ`` of
    ``josé`` and ``joſé``.
  - An occurrence relates to the subject only through such a spelling when it is written with
    one (other than the subject's exact spelling) or matches only the subject's upper case
    (:func:`_local_case`). It is ``AMBIGUOUS`` when a *twin* exists in the product: an account's
    e-mail or an invitation's address with the occurrence's upper case and another canonical
    form (NFC, lower-cased letter by letter, the domain as registration stores it). It is then
    never exported or erased for either twin. When no twin exists, it is the subject's:
    ``WHOLE`` (:func:`_twin_case`).
  - The lookup (:func:`existing_addresses`) runs only for such an occurrence, once per spelling,
    and selects with a ``LIKE`` pattern built from that upper case (:func:`_twin_pattern`).
    Without a lookup (:attr:`Subject.existing` unset) every such occurrence stays ``AMBIGUOUS``.

Two steps cover every form:

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
  sees them. For an address with such a run, the local part followed by ``@``, ``%40``, ``＠``
  or ``﹫`` is selected too, so domain spellings without an ASCII fragment are read.
* **The confirmation** (:func:`mention`) reads each candidate text under several views: as
  stored, with up to three layers of JSON escapes undone, and each percent-decoded. Every view
  is in NFC and keeps its own letter case. In every view it finds the local part, by its lower
  case or its upper case, letter by letter. It then reads the domain after ``@``, or after a
  separator, with registration's UTS46 mapping.

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
  its domain are joined by an explicit separator written instead of ``@``. The separators are
  ``[at]``, ``(at)``, ``{at}``, ``<at>``, ``[@]``, ``(@)``, ``&#64;``, ``&#x40;``, ``&commat;``,
  the fullwidth ``＠`` and the small ``﹫`` (registration accepts neither), or ``at`` / ``@``
  between spaces. Examples are ``a&#64;b.co``, ``a [at] b.co`` and ``a＠b.co``. The matcher
  cannot tell whether that is the address, so it flags it rather than pass it. Mere
  co-occurrence is not flagged: a text naming ``John`` and ``acme.example`` separately is not
  about ``john@acme.example``.

Whitespace and ``@`` are never part of an unquoted address, so the search for a longer address
stops there. Its bounds are search limits, not validity rules (see ``_LOCAL_MAX``). The
classification assumes the subject's address is itself registrable; every address the product
stores is.

**Opaque ids** (account, organization, workspace) are ``WHOLE`` when no id character
(``[A-Za-z0-9_-]``) touches them on either side, and ``AMBIGUOUS`` otherwise.

**Not detected (recorded limits).** Exactly these pass unseen: texts that keep neither the
local part followed by ``@`` and a domain spelling registration maps to the account's domain,
nor that local part joined to such a domain by one of the listed separators, in the views
:func:`_views` decodes (up to three JSON-escape layers, each also percent-decoded once).
Examples are a hash, base64 or other encoding of the address (``%2540``, a fourth JSON layer,
``%5Cu0040``, ``&#46;``, quoted-printable ``=40``); characters inserted into it (a soft hyphen,
a zero-width space, a line break, quotes around the local part); the domain spelled out
(``b [dot] co``); or a separator not in that list. No product writer produces any of them;
third-party or customer text can. A local part in a compatibility spelling (fullwidth
``ｃａｒｏｌ``) is not one of them: registration keeps it as a distinct account.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from functools import cache, lru_cache
from typing import Any
from urllib.parse import quote, unquote

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import Text, and_, cast, func, or_, select, true
from sqlalchemy.orm import Session

from app.organizations.models import OrganizationInvitation, User

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
#: An explicit separator written between a local part and a domain instead of "@", including the
#: fullwidth "＠" and the small "﹫", which registration does not accept in an address (review r5
#: R5-3). Mere co-occurrence of the two is never enough (review r4 R4-3).
_AT = r"(?:at|@|\uff20|\ufe6b)"
_SEPARATOR = re.compile(
    rf"\s*(?:\[\s*{_AT}\s*\]|\(\s*{_AT}\s*\)|\{{\s*{_AT}\s*\}}|<\s*{_AT}\s*>"
    r"|&#0*64;|&#x0*40;|&commat;|\uff20|\ufe6b)\s*"
    rf"|\s+{_AT}\s+",
    re.IGNORECASE,
)
#: What would continue a local part before it, or a domain after it.
_LOCAL_TOUCH = re.compile(r"[\w.+-]")
_DOMAIN_TOUCH = re.compile(r"[\w-]")


class Mention(IntEnum):
    """How a text holds an identifier; the strongest occurrence decides."""

    NONE = 0
    AMBIGUOUS = 1
    WHOLE = 2


# --------------------------------------------------------------------------- #
# The forms of an address
# --------------------------------------------------------------------------- #
def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _lower(text: str) -> str:
    """Lower-cased letter by letter: ``str.lower()``, never ``str.casefold()`` (case-folding maps
    distinct letters together, ``ß``→``ss``, ``ſ``→``s``, ``ﬃ``→``ffi``, ``ς``→``σ``, and
    registration keeps those addresses apart: review r4 R4-1), and without the final-sigma rule,
    so a letter's reading never depends on its neighbour."""
    return "".join(c.lower() for c in text)


def _upper(text: str) -> str:
    return "".join(c.upper() for c in text)


@lru_cache(maxsize=1)
def _shared_uppers() -> tuple[str, ...]:
    """Upper-case spellings shared by letters whose lower case differs, from Unicode's own case
    mappings: ``S`` (``s``, ``ſ``), ``I`` (``i``, ``ı``), ``Σ`` (``σ``, ``ς``), ``Μ`` (``μ``,
    ``µ``), ``В`` (``в``, ``ᲀ``), ``SS`` (``ß``), ``FFI`` (``ﬃ``) and the like. Longest first."""
    shared = set()
    for code in range(0x110000):
        letter = chr(code)
        upper = letter.upper()
        if _lower(upper) != letter.lower():
            shared.add(upper)
    return tuple(sorted(shared, key=lambda u: (-len(u), u)))


def shares_upper_case(text: str) -> bool:
    """Whether ``text`` holds an upper-case spelling that two letters of different lower case
    share, so it may be the upper case of a different registrable address (review r5 R5-1)."""
    return any(upper in text for upper in _shared_uppers())


def _local_case(written: str, own: str) -> Mention:
    """Whether a local part written in a text is the subject's own ``own`` (NFC both).

    ``WHOLE``: its exact spelling, or a letter case of it that no other registrable address
    shares. ``AMBIGUOUS``: a case it shares with another registrable address (``ΣΣ`` is the upper
    case of both ``σσ`` and ``σς``), or one that matches only its upper case (``straße`` for
    ``strasse``); :func:`_twin_case` then decides with the addresses the product holds.
    ``NONE``: neither.
    """
    if written == own:
        return Mention.WHOLE
    same_lower = _lower(written) == _lower(own)
    if same_lower and not shares_upper_case(written):
        return Mention.WHOLE
    if same_lower or _upper(written) == _upper(own):
        return Mention.AMBIGUOUS
    return Mention.NONE


#: Looks up the local parts of the addresses the product holds (account e-mails, invitation
#: addresses) whose letter-by-letter upper case is the first argument, at the domain given second.
Existing = Callable[[str, str], frozenset[str]]


def _twin_case(written: str, own: _Parts, existing: Existing | None) -> Mention:
    """For an occurrence :func:`_local_case` calls ``AMBIGUOUS``: ``AMBIGUOUS`` if a twin exists,
    an address the product holds with the occurrence's upper case and another canonical form
    than ``own``; ``WHOLE`` if none does. Without a lookup, ``AMBIGUOUS``."""
    if existing is None:
        return Mention.AMBIGUOUS
    canonical = _lower(own.local)
    twins = [local for local in existing(_upper(written), own.domain) if _lower(local) != canonical]
    return Mention.AMBIGUOUS if twins else Mention.WHOLE


@dataclass(frozen=True)
class _Parts:
    local: str  # NFC, in the subject's own letter case
    domain: str  # the domain as registration stores it (U-label, lower case)


@lru_cache(maxsize=1024)
def _parts(address: str) -> _Parts:
    try:
        parsed = validate_email(address, check_deliverability=False)
    except EmailNotValidError:
        local, _, domain = address.rpartition("@")
        return _Parts(_nfc(local), _lower(_nfc(domain)))
    return _Parts(_nfc(parsed.local_part), parsed.domain)


@lru_cache(maxsize=8192)
def _domain_of(text: str) -> str | None:
    """The domain registration stores for ``text``, through the same UTS46 mapping: letter case,
    fullwidth letters and stops, the ideographic full stop, mathematical letters, A-labels
    (review r5 R5-2). None when ``text`` is no domain."""
    try:
        return validate_email(f"a@{text}", check_deliverability=False).domain
    except (EmailNotValidError, ValueError, UnicodeError):
        return None


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


def _local_tail_terms(address: str) -> list[tuple[str, ...]]:
    """Terms that select the local part followed by ``@`` (or an encoded or fullwidth ``@``),
    whatever the domain's spelling: registration maps fullwidth letters, ideographic stops and
    mathematical letters to the same domain, and none of those keeps an ASCII fragment of it
    (review r5 R5-2)."""
    try:
        local = validate_email(address, check_deliverability=False).local_part
    except EmailNotValidError:
        local = address.rpartition("@")[0]
    ats = ("@", "%40", "\uff20", "\\uff20", "\ufe6b", "\\ufe6b")
    match = re.search(r"[A-Za-z0-9._~-]+$", local)
    if match and len(match.group()) > 1:
        return [(_ascii_lower(match.group() + at),) for at in ats]
    return [(*_runs(local), _ascii_lower(at)) for at in ats]


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
    #: The product's addresses, for an occurrence related to an address only through a shared
    #: letter case (:func:`existing_addresses`). ``None``: such an occurrence stays ``AMBIGUOUS``.
    existing: Existing | None = field(default=None, compare=False, repr=False)

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
                terms += _local_tail_terms(address)
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


def same_address(value: str | None, address: str, existing: Existing | None = None) -> bool:
    """Whether ``value`` is ``address`` itself: the same domain as registration stores it (NFC,
    case, U-/A-label, UTS46 spellings) and a local part :func:`_local_case` calls ``WHOLE``. A
    letter case another registrable address shares is enough only when no twin exists
    (:func:`_twin_case`; review r5 R5-1, fix pass 6)."""
    if not value:
        return False
    theirs, ours = _parts(value.strip()), _parts(address)
    if theirs.domain != ours.domain:
        return False
    case = _local_case(theirs.local, ours.local)
    if case is Mention.AMBIGUOUS:
        case = _twin_case(theirs.local, ours, existing)
    return case is Mention.WHOLE


# --------------------------------------------------------------------------- #
# The product's own addresses (twins of a shared letter case)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _upper_images() -> tuple[frozenset[str], tuple[str, ...]]:
    """The ASCII characters a non-ASCII letter upper-cases to on its own (``ſ``→``S``,
    ``ı``→``I``), and every upper-case image longer than one character (``ß``→``SS``,
    ``ﬃ``→``FFI``, ``ŉ``→``ʼN``), from Unicode's own case mappings."""
    single, multi = set(), set()
    for code in range(0x110000):
        letter = chr(code)
        upper = letter.upper()
        if len(upper) > 1:
            multi.add(upper)
        elif upper.isascii() and not letter.isascii():
            single.add(upper)
    return frozenset(single), tuple(sorted(multi))


def _like_escape(text: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", text)


def _twin_pattern(upper_local: str) -> str:
    """A ``LIKE`` pattern, in ASCII lower case, that every local part whose letter-by-letter upper
    case is ``upper_local`` matches. A character inside an image longer than one character
    (``SS``, ``ST``, ``FFI``) may come from one letter or several: any run. An ASCII character
    no other letter upper-cases to is itself, in either case. Any other is exactly one
    character."""
    single, multi = _upper_images()
    spanned = [False] * len(upper_local)
    for image in multi:
        start = upper_local.find(image)
        while start != -1:
            spanned[start : start + len(image)] = [True] * len(image)
            start = upper_local.find(image, start + 1)
    out: list[str] = []
    for char, inside in zip(upper_local, spanned, strict=True):
        if inside:
            piece = "%"
        elif char.isascii() and char not in single:
            piece = _like_escape(char.lower())
        else:
            piece = "_"
        if not (piece == "%" and out and out[-1] == "%"):
            out.append(piece)
    return "".join(out)


def _domain_forms(domain: str) -> tuple[str, ...]:
    """``domain`` as registration stores it (U-label) and as its IDNA A-label."""
    try:
        ascii_domain = validate_email(f"a@{domain}", check_deliverability=False).ascii_domain
    except EmailNotValidError:
        ascii_domain = None
    return tuple(dict.fromkeys(d for d in (domain, ascii_domain) if d))


def existing_addresses(db: Session) -> Existing:
    """A lookup, for :attr:`Subject.existing`, of the addresses the product holds: every account's
    e-mail and every invitation's address, in any state. Each distinct upper case is looked up
    once, with a ``LIKE`` per column that only addresses of that upper case at that domain (and
    few others) satisfy; Python then compares exactly."""
    columns = (User.email, OrganizationInvitation.email)

    @cache
    def find(upper_local: str, domain: str) -> frozenset[str]:
        local = _twin_pattern(upper_local)
        patterns = [f"{local}@{_like_escape(_ascii_lower(d))}" for d in _domain_forms(domain)]
        found: set[str] = set()
        for column in columns:
            text = func.lower(cast(column, Text))
            values = db.execute(
                select(column).where(or_(*(text.like(p, escape="\\") for p in patterns)))
            ).scalars()
            for value in values:
                parts = _parts(value.strip())
                if parts.domain == domain and _upper(parts.local) == upper_local:
                    found.add(parts.local)
        return frozenset(found)

    return find


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
    percent-decoded; every view in NFC, in its own letter case."""
    layers = [text]
    for _ in range(_ESCAPE_LAYERS):
        unescaped = _unescape_json(layers[-1])
        if unescaped == layers[-1]:
            break
        layers.append(unescaped)
    views = [view for layer in layers for view in (layer, unquote(layer))]
    return tuple(dict.fromkeys(_nfc(v) for v in views))


def _spans(view: str, needle: str, case: Any) -> Iterable[tuple[int, int]]:
    """Spans of ``view`` whose letters, each mapped by ``case``, spell ``needle``, in whole
    letters of ``view`` (a mapping can expand one letter, ``ß``→``SS``)."""
    if not needle:
        return
    mapped, origin = [], []
    for index, letter in enumerate(view):
        image = case(letter)
        mapped.append(image)
        origin.extend([index] * len(image))
    joined = "".join(mapped)
    for match in re.finditer(re.escape(needle), joined):
        a, b = match.start(), match.end()
        if (a and origin[a - 1] == origin[a]) or (b < len(joined) and origin[b] == origin[b - 1]):
            continue  # the match starts or ends inside one letter's image
        yield origin[a], origin[b - 1] + 1


def _domain_end(view: str, start: int, domain: str) -> int | None:
    """Where a spelling of ``domain`` that starts at ``start`` ends (the shortest), or None."""
    end = start
    while end < len(view) and end - start < _DOMAIN_MAX and _extends(view[end]):
        end += 1
    for stop in range(start + 1, end + 1):
        if _domain_of(view[start:stop]) == domain:
            return stop
    return None


def _undecided(view: str, start: int, stop: int, domain: str) -> bool:
    """The local part at ``start:stop`` and the domain are joined by an explicit separator."""
    if start and _LOCAL_TOUCH.match(view[start - 1]):
        return False
    separator = _SEPARATOR.match(view, stop)
    if separator is None:
        return False
    end = _domain_end(view, separator.end(), domain)
    if end is None:
        return False
    after = view[end : end + 2]
    if not after:
        return True
    return not (_DOMAIN_TOUCH.match(after[0]) or (after[0] == "." and after[1:].isalnum()))


def _address_in_view(view: str, parts: _Parts, existing: Existing | None) -> Mention:
    best = Mention.NONE
    spans = {
        *_spans(view, _lower(parts.local), str.lower),
        *_spans(view, _upper(parts.local), str.upper),
    }
    for start, stop in sorted(spans):
        case = _local_case(view[start:stop], parts.local)
        if case is Mention.NONE:
            continue
        if view.startswith("@", stop):
            end = _domain_end(view, stop + 1, parts.domain)
            if end is not None:
                found = _address_mention(view, start, end)
                if case is Mention.AMBIGUOUS and found is Mention.WHOLE:
                    found = _twin_case(view[start:stop], parts, existing)
                best = max(best, found)
                if best is Mention.WHOLE:
                    return best
                continue
        if _undecided(view, start, stop, parts.domain):
            best = max(best, Mention.AMBIGUOUS)
    return best


def mention(text: str, subject: Subject) -> Mention:
    """How ``text`` holds any of ``subject``'s identifiers, in any of their forms."""
    best = Mention.NONE
    views = _views(text)
    for value in subject.ids:
        for view in views:
            for start, stop in _spans(view, _lower(value), str.lower):
                best = max(best, _id_mention(view, start, stop))
                if best is Mention.WHOLE:
                    return best
    for address in subject.addresses:
        parts = _parts(address)
        for view in views:
            best = max(best, _address_in_view(view, parts, subject.existing))
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


__all__ = [
    "Mention",
    "Subject",
    "address_forms",
    "encoded_forms",
    "existing_addresses",
    "mention",
    "mention_in_json",
    "prefilter",
    "registrable",
    "same_address",
    "shares_upper_case",
]
