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
* **A shared upper case decides only when a twin exists** (review r5; fix pass 6). Without a
  lookup of the product's addresses such an occurrence is ``AMBIGUOUS``. With one, it is
  ``AMBIGUOUS`` only if the product holds an address of that upper case with another canonical
  form, and ``WHOLE`` otherwise. The lookup's ``LIKE`` pattern selects every spelling of an upper
  case.
* **Opaque ids** are whole unless an id character touches them.
* **JSON** is judged string by string, object keys included.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata

import pytest
from email_validator import validate_email

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
    shares_upper_case,
)
from app.privacy.matching import _local_case as local_case
from app.privacy.matching import _spells as spells
from app.privacy.matching import _twin_pattern as twin_pattern
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


def _cased_texts(address: str):
    """(text, its local part as cased): every form x encoding x letter case, as raw text and as
    stored JSON text."""
    for form in address_forms(address):
        for cased in (form, form.upper(), form.title()):
            local = cased.rpartition("@")[0]
            for encoded in encoded_forms(cased):
                yield f"Note: {encoded}.", local
                yield json.dumps({"v": f"Note: {encoded}."}), local  # SQLAlchemy's JSON text


def _texts(address: str):
    return (text for text, _local in _cased_texts(address))


@pytest.mark.parametrize("address", [E, JOSE, IDN])
def test_the_sql_prefilter_selects_every_form_under_ascii_only_lower(address):
    terms = Subject(addresses=(address,)).sql_terms()
    assert all(f == _sqlite_lower(f) for term in terms for f in term)  # only ASCII lower-cased
    for text in _texts(address):
        stored = _sqlite_lower(text)
        assert any(all(f in stored for f in term) for term in terms), text


@pytest.mark.parametrize("address", [E, JOSE, IDN])
def test_the_confirmation_finds_every_form(address):
    """Every form is found: WHOLE, unless its letter case is shared with another registrable
    address (``JOSÉ`` is also the upper case of ``joſé``), which is flagged (review r5)."""
    subject = Subject(addresses=(address,))
    stored = address.rpartition("@")[0]
    for text, local in _cased_texts(address):
        value = json.loads(text)["v"] if text.startswith("{") else text
        shared = unicodedata.normalize("NFC", local) != unicodedata.normalize("NFC", stored) and (
            shares_upper_case(local)
        )
        assert mention(value, subject) is (AMBIGUOUS if shared else WHOLE), value
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
        # A longer local part ending in hers is someone else's, separator or not.
        (f"Note: x{L} [at] {D}", NONE),
        (f"Note: j.{L}&#64;{D}", NONE),
        # Review r5 R5-3: the fullwidth and the small commercial at are explicit separators.
        (f"Note: {L}\uff20{D}", AMBIGUOUS),
        (f"Note: {L}\ufe6b{D}", AMBIGUOUS),
        (f"Note: {L}\uff20\uff50rivacy.example", AMBIGUOUS),  # and a UTS46 domain spelling
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
    letter case (a recorded policy) are folded. The other address written in lower case is
    another address (fix pass 7); only an all-capitals spelling relates to both (review r5)."""
    assert not same_address(other, one)
    assert mention(f"Invited {other} today", Subject(addresses=(one,))) is NONE


def test_what_registration_treats_as_one_address_still_matches():
    assert same_address(unicodedata.normalize("NFD", JOSE), JOSE)
    assert same_address("carol@xn--exmple-cua.com", IDN)
    assert same_address("carol@EXÄMPLE.COM", IDN)
    assert same_address("CAROL_X@privacy.example", E)  # local-part case: the recorded policy
    assert same_address("carol_x@privacy\u3002example", E)  # UTS46, as registration maps it


def test_same_address_compares_every_form():
    assert not same_address("JOSÉ@privacy.example", JOSE)  # also joſé's upper case (review r5)
    assert same_address("José@privacy.example", JOSE)
    assert same_address(unicodedata.normalize("NFD", JOSE), JOSE)
    assert same_address("carol@XN--EXMPLE-CUA.COM", IDN)
    assert not same_address(f"x{E}", E)
    assert not same_address(None, E)


# --------------------------------------------------------------------------- #
# A shared upper case, without a lookup of the product's addresses (reviews r5, R5-1)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("subject", "text"),
    [
        ("σσ@privacy.example", "ΣΣ@privacy.example"),
        ("σς@privacy.example", "ΣΣ@privacy.example"),
        ("ia@privacy.example", "IA@privacy.example"),
        ("ıa@privacy.example", "IA@privacy.example"),
        ("straße@privacy.example", "STRASSE@privacy.example"),
        ("strasse@privacy.example", "STRASSE@privacy.example"),
        ("oﬃce@privacy.example", "OFFICE@privacy.example"),
        ("office@privacy.example", "OFFICE@privacy.example"),
        (JOSE, "JOSÉ@privacy.example"),
    ],
)
def test_a_shared_upper_case_is_ambiguous_for_either_twin(subject, text):
    assert mention(f"Note: {text}.", Subject(addresses=(subject,))) is AMBIGUOUS
    assert not same_address(text, subject)


@pytest.mark.parametrize(
    ("subject", "text", "expected"),
    [
        (E, "CAROL_X@PRIVACY.EXAMPLE", WHOLE),  # no letter here has two lower cases
        ("kate@privacy.example", "KATE@privacy.example", WHOLE),
        ("Sam@privacy.example", "Sam@privacy.example", WHOLE),  # its own exact spelling
        ("Sam@privacy.example", "sam@privacy.example", WHOLE),  # lower case: one reading
        ("Sam@privacy.example", "SAM@privacy.example", AMBIGUOUS),  # also ſam's upper case
        ("ſam@privacy.example", "SAM@privacy.example", AMBIGUOUS),
        ("straße@privacy.example", "STRAẞE@privacy.example", AMBIGUOUS),  # S: also ſtraße
    ],
)
def test_only_an_upper_case_with_one_reading_is_whole(subject, text, expected):
    assert mention(f"Note: {text}.", Subject(addresses=(subject,))) is expected


def test_the_shared_upper_case_spellings_are_the_unicode_ones():
    """The set is computed from Unicode's own case mappings: an upper-case spelling is shared
    when lower-casing it does not give back every letter whose upper case it is."""
    for shared in ("S", "I", "Σ", "Μ", "SS", "FF", "FFI", "ST", "В"):
        assert shares_upper_case(shared), shared
    for single in ("K", "C", "A", "X", "Ẑ", "ẞ", "И"):
        assert not shares_upper_case(single), single
    assert not shares_upper_case("josé")  # lower case shares nothing


# --------------------------------------------------------------------------- #
# ... and with one: AMBIGUOUS only when a twin exists (fix pass 6)
# --------------------------------------------------------------------------- #
def _holding(*addresses: str):
    """A stand-in for ``existing_addresses``: the product holds exactly ``addresses``."""

    def find(upper_local: str, domain: str) -> frozenset[str]:
        found = set()
        for address in addresses:
            local, _, at = address.rpartition("@")
            if at == domain and _nfc("".join(c.upper() for c in local)) == upper_local:
                found.add(local)
        return frozenset(found)

    return find


#: (subject, text, twin): the text's local part is the upper case of both addresses.
TWINS = [
    ("σσ@privacy.example", "ΣΣ@privacy.example", "σς@privacy.example"),
    ("σς@privacy.example", "ΣΣ@privacy.example", "σσ@privacy.example"),
    ("ia@privacy.example", "IA@privacy.example", "ıa@privacy.example"),
    ("ıa@privacy.example", "IA@privacy.example", "ia@privacy.example"),
    ("straße@privacy.example", "STRASSE@privacy.example", "strasse@privacy.example"),
    ("strasse@privacy.example", "STRASSE@privacy.example", "straße@privacy.example"),
    ("oﬃce@privacy.example", "OFFICE@privacy.example", "office@privacy.example"),
    ("office@privacy.example", "OFFICE@privacy.example", "oﬃce@privacy.example"),
    (JOSE, "JOSÉ@privacy.example", "joſé@privacy.example"),
    ("sales@privacy.example", "SALES@privacy.example", "ſales@privacy.example"),
    ("иван@пример.рф", "ИВАН@пример.рф", "иᲀан@пример.рф"),
]


@pytest.mark.parametrize(("subject", "text", "twin"), TWINS)
def test_without_a_twin_a_shared_letter_case_is_the_subjects(subject, text, twin):
    held = _holding(subject, "bob@privacy.example")
    assert mention(f"Note: {text}.", Subject(addresses=(subject,), existing=held)) is WHOLE
    assert same_address(text, subject, held)


@pytest.mark.parametrize(("subject", "text", "twin"), TWINS)
def test_with_a_twin_a_shared_letter_case_stays_ambiguous(subject, text, twin):
    held = _holding(subject, twin)
    assert mention(f"Note: {text}.", Subject(addresses=(subject,), existing=held)) is AMBIGUOUS
    # Registration lower-cases a few well-known mailbox names (RFC 2142), so it stores SALES@ as
    # sales@, the subject's own address; every other spelling here it keeps as written.
    stored = validate_email(text, check_deliverability=False).normalized
    assert same_address(text, subject, held) is (stored == subject)
    # The twin's own spelling has lower-case letters, so it is another address (fix pass 7).
    assert mention(f"Note: {twin}.", Subject(addresses=(subject,), existing=held)) is NONE


#: (subject, spelling, route). Capital positions are ambiguous, lower-case positions are exact
#: (fix pass 8; fix pass 7 related only all-capitals spellings). "none": a lower-case letter
#: contradicts the subject, so it is another address. "twin": the spelling relates through a
#: shared capital, and the twin rule decides. Both directions, mixed case included, and every
#: spelling of adversarial review r6 cap 196.
SPELLINGS = [
    ("straße@privacy.example", "strasse", "none"),
    ("straße@privacy.example", "Strasse", "none"),
    ("straße@privacy.example", "sTRASSE", "twin"),
    ("straße@privacy.example", "STRASSE", "twin"),
    ("straße@privacy.example", "StraSSe", "twin"),
    ("straße@privacy.example", "Straße", "twin"),
    ("strasse@privacy.example", "straße", "none"),
    ("strasse@privacy.example", "Straße", "none"),
    ("strasse@privacy.example", "STRASSE", "twin"),
    ("strasse@privacy.example", "Strasse", "twin"),
    ("strasse@privacy.example", "StraSSe", "twin"),
    ("σσ@privacy.example", "σς", "none"),
    ("σσ@privacy.example", "Σς", "none"),
    ("σσ@privacy.example", "ΣΣ", "twin"),
    ("σσ@privacy.example", "Σσ", "twin"),
    ("σσ@privacy.example", "σΣ", "twin"),
    ("σς@privacy.example", "σσ", "none"),
    ("σς@privacy.example", "Σσ", "none"),
    ("σς@privacy.example", "ΣΣ", "twin"),
    ("σς@privacy.example", "Σς", "twin"),
    ("σς@privacy.example", "σΣ", "twin"),
    ("ia@privacy.example", "ıa", "none"),
    ("ia@privacy.example", "ıA", "none"),
    ("ia@privacy.example", "IA", "twin"),
    ("ia@privacy.example", "Ia", "twin"),
    ("ıa@privacy.example", "ia", "none"),
    ("ıa@privacy.example", "Ia", "twin"),
    ("ıa@privacy.example", "IA", "twin"),
    ("ırmak@privacy.example", "Irmak", "twin"),
    ("ırmak@privacy.example", "irmak", "none"),
    ("irmak@privacy.example", "Irmak", "twin"),
    ("irmak@privacy.example", "ırmak", "none"),
    ("ılgaz@privacy.example", "Ilgaz", "twin"),
    ("ilgaz@privacy.example", "Ilgaz", "twin"),
    ("ſales@privacy.example", "Sales", "twin"),
    ("ſales@privacy.example", "SaLES", "twin"),
    ("ſales@privacy.example", "sales", "none"),
    ("ſales@privacy.example", "saLES", "none"),
    ("sales@privacy.example", "Sales", "twin"),
    ("sales@privacy.example", "ſales", "none"),
]
_TWIN_OF = {
    "straße": "strasse", "strasse": "straße", "σσ": "σς", "σς": "σσ", "ia": "ıa", "ıa": "ia",
    "ırmak": "irmak", "irmak": "ırmak", "ılgaz": "ilgaz", "ilgaz": "ılgaz",
    "ſales": "sales", "sales": "ſales",
}


@pytest.mark.parametrize(("subject", "spelling", "route"), SPELLINGS)
def test_capital_positions_are_ambiguous_and_lower_case_positions_exact(
    subject, spelling, route
):
    """Fix pass 8 (adversarial review r6 R6-1): a spelling relates to the subject when every
    lower-case letter is the subject's letter there and every capital is the upper case of the
    subject's letter (or letters: ``ß`` is ``SS``) there. It is then the subject's unless a twin
    exists; a contradicting lower-case letter makes it another address."""
    local, _, domain = subject.rpartition("@")
    twin = f"{_TWIN_OF[local]}@{domain}"
    text = f"Note: {spelling}@{domain}."
    alone, paired = _holding(subject), _holding(subject, twin)
    if route == "none":
        expected = (NONE, NONE, NONE)
    else:
        expected = (AMBIGUOUS, WHOLE, AMBIGUOUS)
    assert (
        mention(text, Subject(addresses=(subject,))),
        mention(text, Subject(addresses=(subject,), existing=alone)),
        mention(text, Subject(addresses=(subject,), existing=paired)),
    ) == expected
    # A stored value is read as registration stores it (it lower-cases the RFC 2142 role names).
    if validate_email(f"{spelling}@{domain}", check_deliverability=False).local_part == spelling:
        assert same_address(f"{spelling}@{domain}", subject, alone) is (route == "twin")


#: The six letters whose upper case NFC re-composes (review r7 R7-1): ``ΐ`` upper-cases to a
#: decomposed ``Ϊ́`` (three code points), which NFC, like every stored address, writes as two.
NFC_GREEK = ["\u0390", "\u03b0", "\u1fd2", "\u1fd7", "\u1fe2", "\u1fe7"]


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@pytest.mark.parametrize("letter", NFC_GREEK)
def test_a_capital_that_nfc_recomposes_is_still_the_subjects(letter):
    """Fix pass 9: every case image is compared in NFC (decomposed in the span search), so the
    capitals of these letters, alone or beside a lower-case one, are the subject's."""
    upper = _nfc(letter.upper())
    assert upper != letter.upper()  # the raw upper case is not NFC
    subject = f"{letter}α@privacy.example"
    for spelling in (f"{upper}Α", f"{upper}α", f"{letter}Α"):
        text = f"Note: {_nfc(spelling)}@privacy.example."
        assert mention(text, Subject(addresses=(subject,))) is WHOLE, spelling
    # Two of them, one capital and one lower case, compose across the letters' boundary.
    pair = Subject(addresses=(f"{letter}{letter}@privacy.example",))
    assert mention(f"Note: {_nfc(upper + letter)}@privacy.example.", pair) is WHOLE
    assert same_address(_nfc(f"{upper}Α@privacy.example"), subject)
    # With a shared capital beside it, the twin rule decides as for any other letter.
    sigma, final = f"{letter}σ@privacy.example", f"{letter}ς@privacy.example"
    text = f"Note: {_nfc(upper)}Σ@privacy.example."
    assert mention(text, Subject(addresses=(sigma,), existing=_holding(sigma))) is WHOLE
    assert mention(text, Subject(addresses=(sigma,), existing=_holding(sigma, final))) is (
        AMBIGUOUS
    )


@pytest.mark.parametrize(
    ("written", "own"),
    [
        ("carol_xy", "carol_x"),
        ("CAROL_XY", "carol_x"),
        ("Irmakx", "ırmak"),
        ("IRMAKS", "ırmak"),
        ("StraSSeS", "straße"),
        ("Irma", "ırmak"),
        ("STRASS", "straße"),
    ],
)
def test_a_spelling_must_cover_the_whole_local_part(written, own):
    """Review r7 R7-3: ``_spells`` relates only a spelling of exactly the subject's letters, never
    a longer or shorter one (mutant S2, a prefix, is caught here)."""
    assert not spells(written, own)
    assert local_case(written, own) is NONE


def test_an_address_with_the_subjects_canonical_form_is_no_twin():
    """Another account at the subject's address in another letter case (the recorded policy,
    decisions packet row 5.1) is not a twin: its canonical form is the subject's."""
    held = _holding(JOSE, "JOSÉ@privacy.example", "José@privacy.example")
    assert mention("Note: JOSÉ@privacy.example.", Subject(addresses=(JOSE,), existing=held)) is (
        WHOLE
    )


def test_the_lookup_runs_only_for_a_shared_letter_case():
    asked: list[tuple[str, str]] = []

    def find(upper_local: str, domain: str) -> frozenset[str]:
        asked.append((upper_local, domain))
        return frozenset()

    subject = Subject(addresses=(E, "sales@privacy.example"), existing=find)
    for text in (f"Note: {E}", "Note: CAROL_X@PRIVACY.EXAMPLE", "Note: xcarol_x@privacy.example"):
        mention(text, subject)
    assert asked == []
    assert mention("Note: SALES@privacy.example", subject) is WHOLE
    assert asked == [("SALES", "privacy.example")]


def _like(value: str, pattern: str) -> bool:
    """SQL ``lower(value) LIKE pattern ESCAPE '\\'``, as SQLite evaluates it."""
    with sqlite3.connect(":memory:") as conn:
        row = conn.execute("SELECT lower(?) LIKE ? ESCAPE '\\'", (value, pattern)).fetchone()
    return row[0] == 1


@pytest.mark.parametrize(
    ("upper_local", "spellings"),
    [
        ("SALES", ["sales", "ſales", "SALES", "Sales", "ſaleſ"]),
        ("IA", ["ia", "ıa", "IA"]),
        ("ΣΣ", ["σσ", "σς", "ΣΣ", "Σς"]),
        ("STRASSE", ["strasse", "straße", "STRASSE", "ſtraße", "ﬆrasse", "ﬅraße"]),
        ("OFFICE", ["office", "oﬃce", "oﬀice", "OFFICE"]),
        ("JOSÉ", ["josé", "joſé", "JOSÉ"]),
        ("ИВАН", ["иван", "иᲀан", "ИВАН"]),
        ("CAROL_X", ["carol_x", "CAROL_X", "Carol_X"]),
        ("A%B_C", ["a%b_c", "A%B_C"]),
    ],
)
def test_the_twin_pattern_selects_every_spelling_of_that_upper_case(upper_local, spellings):
    pattern = twin_pattern(upper_local) + "@privacy.example"
    for spelling in spellings:
        assert "".join(c.upper() for c in spelling) == upper_local, spelling
        assert _like(f"{spelling}@privacy.example", pattern), (spelling, pattern)


@pytest.mark.parametrize(
    ("upper_local", "other"),
    [
        ("SALES", "sale"),
        ("SALES", "xsales"),
        ("CAROL_X", "carolxx"),  # "_" is escaped: no wildcard
        ("A%B_C", "aXbYc"),
        ("IA", "iaa"),
    ],
)
def test_the_twin_pattern_leaves_other_local_parts_out(upper_local, other):
    assert not _like(f"{other}@privacy.example", twin_pattern(upper_local) + "@privacy.example")


@pytest.mark.parametrize(
    "domain",
    [
        "privacy\u3002example",
        "privacy\uff0eexample",
        "privacy\uff61example",
        "\uff50\uff52\uff49\uff56\uff41\uff43\uff59.example",
        "\U0001d429rivacy.example",
        "PRIVACY.EXAMPLE",
    ],
)
def test_a_domain_registration_maps_to_hers_is_whole(domain):
    """Review r5 R5-2: registration's UTS46 mapping makes these the same domain."""
    assert mention(f"Note: {L}@{domain}.", HERS) is WHOLE
    assert same_address(f"{L}@{domain}", E)
