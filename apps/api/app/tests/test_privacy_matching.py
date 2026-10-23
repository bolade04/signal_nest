"""P6-PRIV-1: the shared matcher that erasure and the E10 verifier both use.

``app.privacy.matching`` decides whether a text names a subject. An address occurrence is
``WHOLE`` when no address the product would register contains it as part of a longer one. It
is ``AMBIGUOUS`` when some registrable address does: another person's address, or the
subject's own in a context the grammar cannot tell apart from one.

* **The grammar is the product's.** For every boundary character in question, the matcher's
  :func:`registrable` agrees with what ``POST /auth/register`` accepts (``RegisterRequest``).
  The one difference is surrounding whitespace, which registration strips first and which is
  never part of a stored address.
* **Every context from adversarial review r2** (caps 019, 022, 065), in both directions. The
  subject's own address is whole in ``mailto:``, ``<…>``, ``"…"``, ``(…)``, in capitals,
  before a sentence-ending dot and percent-encoded. Other people's addresses (``xcarol_x@…``,
  ``o'carol_x@…``, ``ícarol_x@…``, ``…é.com``, ``….org``) are ambiguous, and so is the
  subject's own after ``email=``, ``users/``, ``#`` or ``{``, or before ``.Word``. Each of
  those characters can belong to a registrable address (registration accepts
  ``email=carol@…``), so these are the recorded residual ambiguity (``D-PRIV-7``).
  ``carol_x+tag@…`` is another address and no occurrence.
* **Every form of the address** (review r3). The forms are NFC and NFD, any letter case
  (lower-cased, never case-folded: review r4), JSON ``\\uXXXX`` escapes, percent-encoding,
  and the IDNA A-label and U-label domain. The confirmation finds each one, and the SQL
  pre-filter's ASCII fragments select each one under SQLite's ASCII-only ``lower()``, whether
  as raw text or as the JSON text SQLAlchemy stores. A text whose local part and domain are
  joined by an explicit separator instead of ``@`` (``&#64;``, ``[at]``, `` at ``), but holds
  no form of the address, is undecided, so it is flagged. Mere co-occurrence is not (review r4).
* **No folding joins two distinct addresses** (review r4). ``ß``/``ss``, ``ſ``/``s``, ``ﬃ``/``ffi``
  and ``ς``/``σ`` stay apart, as registration keeps them.
* **Opaque ids** are whole unless an id character touches them.
* **JSON** is judged string by string, object keys included.
"""

from __future__ import annotations

import json
import unicodedata

import pytest

from app.auth.schemas import RegisterRequest
from app.privacy.matching import (
    Mention,
    Subject,
    address_forms,
    encoded_forms,
    mention,
    mention_in_json,
    registrable,
    same_address,
)
from app.tests._privacy_support import CAROL, EMAIL

E = EMAIL[CAROL]  # carol_x@privacy.example
L, D = E.split("@")
HERS = Subject(ids=(CAROL,), addresses=(E,))
WHOLE, AMBIGUOUS, NONE = Mention.WHOLE, Mention.AMBIGUOUS, Mention.NONE

#: (case, text, expected). Caps 019/022 (o1-o6 other people's, u1-u10 hers), cap 065, and the
#: grammar's own edges.
CONTEXTS = [
    ("o1-longer-local-part", f"x{E}", AMBIGUOUS),
    ("o2-apostrophe", f"o'{E}", AMBIGUOUS),
    ("o3-non-ascii-before", f"í{E}", AMBIGUOUS),
    ("o4-non-ascii-domain", f"{E}é.com", AMBIGUOUS),
    ("o5-longer-domain", f"{E}.org", AMBIGUOUS),
    ("o6-plus-tag", f"{L}+tag@{D}", NONE),
    ("u1-key-equals", f"email={E}", AMBIGUOUS),
    ("u2-path", f"users/{E}", AMBIGUOUS),
    ("u3-braces", f"{{{E}}}", AMBIGUOUS),
    ("u4-dot-word", f"{E}.Thanks", AMBIGUOUS),
    ("u5-mailto", f"mailto:{E}", WHOLE),
    ("u6-display-name", f'"Carol" <{E}>', WHOLE),
    ("u7-url-encoded", f"{L}%40{D}", WHOLE),
    ("u9-capitals", E.upper(), WHOLE),
    ("u10-hash", f"#{E}", AMBIGUOUS),
    ("sentence-end", f"Spoke to {E}.", WHOLE),
    ("double-quotes", f'"{E}"', WHOLE),
    ("parentheses", f"({E})", WHOLE),
    ("single-quotes", f"'{E}'", AMBIGUOUS),
    ("curly-apostrophe", f"’{E}", AMBIGUOUS),
    ("leading-dot", f".{E}", WHOLE),
    ("dotted-local-part", f"x.{E}", AMBIGUOUS),
    ("underscore-after", f"{E}_x", WHOLE),
    ("numeric-label-after", f"{E}.1", WHOLE),
    ("comma-list", f"bob@privacy.example,{E}", WHOLE),
    ("newline", f"line\n{E}\nline", WHOLE),
    ("absent", "nobody@privacy.example", NONE),
]


@pytest.mark.parametrize(
    ("text", "expected"), [c[1:] for c in CONTEXTS], ids=[c[0] for c in CONTEXTS]
)
def test_each_context_is_classified_by_the_registration_grammar(text, expected):
    assert mention(text, HERS) is expected


#: Every boundary character in question, written around the address with no whitespace.
_CANDIDATES = [
    E, f"email={E}", f"users/{E}", f"#{E}", f"{{{E}", f"|{E}", f"'{E}", f"o'{E}", f"x{E}",
    f"í{E}", f".{E}", f"x.{E}", f"x..{E}", f":{E}", f"<{E}", f'"{E}', f"({E}", f"%{E}",
    f"’{E}", f"«{E}", f"{E}.Thanks", f"{E}.", f"{E}.org", f"{E}é.com", f"{E}-x",
    f"{E}_x", f"{E}}}", f"{E}>", f"{E}.1", f"{E}..", f"{E}:", f"{E}/x", f"{E}#", f"{E}é",
]


def _registers(value: str) -> bool:
    try:
        RegisterRequest.model_validate(
            {"email": value, "full_name": "x", "password": "long-enough-1",
             "organization_name": "o"}
        )
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("candidate", _CANDIDATES)
def test_the_grammar_is_the_one_registration_enforces(candidate):
    assert registrable(candidate) is _registers(candidate)


def test_whitespace_is_never_part_of_a_stored_address():
    """Registration strips it first, so the stored address never holds it."""
    assert _registers(f" {E}") and not registrable(f" {E}")
    assert mention(f"to {E} now", HERS) is WHOLE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CAROL, WHOLE),
        (f'{{"accepted_by_user_id": "{CAROL}"}}', WHOLE),
        (f"/users/{CAROL}/x", WHOLE),
        (f"{CAROL}2", AMBIGUOUS),
        (f"x-{CAROL}", AMBIGUOUS),
        ("prv-bob", NONE),
    ],
)
def test_opaque_ids_are_whole_unless_an_id_character_touches_them(text, expected):
    assert mention(text, Subject(ids=(CAROL,))) is expected


def test_json_is_judged_string_by_string_keys_included():
    assert mention_in_json({"email": E.upper(), "role": "viewer"}, HERS) is WHOLE
    assert mention_in_json({E: "viewer"}, HERS) is WHOLE
    assert mention_in_json({"v": [f"x{E}"]}, HERS) is AMBIGUOUS
    assert mention_in_json({"v": [1, None, "other"]}, HERS) is NONE


# --------------------------------------------------------------------------- #
# Every form of the address (review r3)
# --------------------------------------------------------------------------- #
JOSE, IDN = "josé@privacy.example", "carol@exämple.com"


def test_the_forms_cover_nfc_nfd_and_both_domain_labels():
    assert set(address_forms(JOSE)) == {JOSE, unicodedata.normalize("NFD", JOSE)}
    assert "carol@xn--exmple-cua.com" in address_forms(IDN)
    assert set(address_forms("carol@xn--exmple-cua.com")) == set(address_forms(IDN))
    assert set(encoded_forms(JOSE)) == {
        JOSE, "jos\\u00e9@privacy.example", "jos%C3%A9%40privacy.example",
        "jos%C3%A9@privacy.example",
    }


def _sqlite_lower(text: str) -> str:
    """What SQLite's ``lower()`` returns: ASCII letters only."""
    return "".join(c.lower() if c.isascii() else c for c in text)


def _texts(address: str):
    """Every form x encoding x letter case, as raw text and as stored JSON text."""
    for form in address_forms(address):
        for cased in (form, form.upper(), form.title()):
            for encoded in encoded_forms(cased):
                yield f"Note: {encoded}."
                yield json.dumps({"v": f"Note: {encoded}."})  # SQLAlchemy's JSON text


@pytest.mark.parametrize("address", [E, JOSE, IDN])
def test_the_sql_prefilter_selects_every_form_under_ascii_only_lower(address):
    terms = Subject(addresses=(address,)).sql_terms()
    assert all(f.isascii() and f == f.lower() for term in terms for f in term)
    for text in _texts(address):
        stored = _sqlite_lower(text)
        assert any(all(f in stored for f in term) for term in terms), text


@pytest.mark.parametrize("address", [E, JOSE, IDN])
def test_the_confirmation_finds_every_form(address):
    subject = Subject(addresses=(address,))
    for text in _texts(address):
        value = json.loads(text)["v"] if text.startswith("{") else text
        assert mention(value, subject) is WHOLE, value
        if text.startswith("{"):  # the same JSON text read raw, as a text column would hold it
            assert mention(text, subject) is not NONE, text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Note: {L}&#64;{D}", AMBIGUOUS),
        (f"Note: {L} [at] {D}", AMBIGUOUS),
        (f"Note: {L.upper()} (AT) {D.upper()}", AMBIGUOUS),
        (f"Note: {L}2&#64;{D}", NONE),  # another local part
        (f"Note: {L} wrote to bob@{D}", NONE),  # the domain belongs to another address
        (f"Note: {L} [at] {D} [dot] example", AMBIGUOUS),
        (f"Note: {L} [at] privacy [dot] example", NONE),  # recorded limit: spelled-out domain
        # Review r4 R4-3: only an explicit separator joining them is undecided, never mere
        # co-occurrence of the local part and the domain in the same text.
        (f"Note: {L} at {D}", AMBIGUOUS),
        (f"Note: {L}(@){D}", AMBIGUOUS),
        (f"Ticket by {L}; domain {D}", NONE),
        (f"{L} wrote about {D} yesterday", NONE),
    ],
)
def test_the_local_part_and_domain_apart_are_undecided_and_flagged(text, expected):
    assert mention(text, HERS) is expected


@pytest.mark.parametrize(
    ("address", "text", "expected"),
    [
        ("john@acme.example", "John from Acme (acme.example) called about pricing.", NONE),
        ("john@acme.example", "See acme.example for details. Regards, John", NONE),
        ("info@acme.example", "More info on acme.example.", NONE),
        ("a@privacy.example", "We wrote a note about privacy.example.", NONE),
        ("sales@shop.example", "Q3 sales at shop.example rose 4%.", AMBIGUOUS),  # " at "
        ("john@acme.example", "John [at] acme.example", AMBIGUOUS),
    ],
)
def test_prose_naming_a_person_and_a_domain_is_not_flagged(address, text, expected):
    """Review r4 cap 143: ordinary prose was flagged for review; only a separator form is."""
    assert mention(text, Subject(addresses=(address,))) is expected


#: Pairs registration keeps apart as two accounts (fix4 cap 136); no folding may join them.
_TWINS = [
    ("strasse@privacy.example", "straße@privacy.example"),
    ("bob@weiss.example", "bob@weiß.example"),
    ("sam@privacy.example", "ſam@privacy.example"),
    ("office@privacy.example", "oﬃce@privacy.example"),
    ("σσ@privacy.example", "σς@privacy.example"),
]


@pytest.mark.parametrize(("one", "other"), _TWINS + [(b, a) for a, b in _TWINS])
def test_distinct_addresses_never_fold_together(one, other):
    """Review r4 R4-1: full case-folding mapped each pair to one form. Only what registration
    itself treats as one address (NFC/NFD, U-/A-label domain, domain case) and the local part's
    letter case (a recorded policy) are folded."""
    assert not same_address(other, one)
    assert mention(f"Invited {other} today", Subject(addresses=(one,))) is NONE


def test_what_registration_treats_as_one_address_still_matches():
    assert same_address(unicodedata.normalize("NFD", JOSE), JOSE)
    assert same_address("carol@xn--exmple-cua.com", IDN)
    assert same_address("carol@EXÄMPLE.COM", IDN)
    assert same_address("JOSÉ@privacy.example", JOSE)  # local-part case: the recorded policy


def test_same_address_compares_every_form():
    assert same_address("JOSÉ@privacy.example", JOSE)
    assert same_address(unicodedata.normalize("NFD", JOSE), JOSE)
    assert same_address("carol@XN--EXMPLE-CUA.COM", IDN)
    assert not same_address(f"x{E}", E)
    assert not same_address(None, E)
