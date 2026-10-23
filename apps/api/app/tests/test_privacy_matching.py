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
  subject's own address is whole in ``mailto:``, ``<…>``, ``"…"``, ``(…)``, in capitals and
  before a sentence-ending dot. Other people's addresses (``xcarol_x@…``, ``o'carol_x@…``,
  ``ícarol_x@…``, ``…é.com``, ``….org``) are ambiguous, and so is the subject's own after
  ``email=``, ``users/``, ``#`` or ``{``, or before ``.Word``. Each of those characters can
  belong to a registrable address (registration accepts ``email=carol@…``), so these are the
  recorded residual ambiguity. ``carol_x+tag@…`` and the URL-encoded form are not
  occurrences at all.
* **Opaque ids** are whole unless an id character touches them.
* **JSON** is judged string by string, object keys included.
"""

from __future__ import annotations

import pytest

from app.auth.schemas import RegisterRequest
from app.privacy.matching import Mention, Subject, mention, mention_in_json, registrable
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
    ("u7-url-encoded", f"{L}%40{D}", NONE),
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


def test_the_sql_prefilter_needles_include_the_json_escaped_form():
    assert Subject(addresses=("josé@example.com",)).sql_needles() == (
        "josé@example.com", "jos\\u00e9@example.com"
    )
