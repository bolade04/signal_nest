"""D-N4 item 39: the output screen's two Unicode rules for generated drafts
(:func:`app.llm.screening.screen_creative`).

* ``blank_character``: a draft holding a blank that is not whitespace and that neither the
  persisted form nor the screening copy removes (U+2800 BRAILLE PATTERN BLANK and the others of
  :data:`app.claims.unicode_guard.WITHHELD_BLANKS`) is withheld: nothing is stored. The Hangul
  fillers and the zero-width characters are *not* withheld — both forms remove them, so a reader
  of the stored draft sees exactly the text the review matched — and that is pinned here too.
* ``mixed_script``: a draft with a word that is not Highly Restrictive (UTS #39) is withheld.
* **Legitimate text passes and is stored as written**: accented names, whole Greek and Cyrillic
  words, Han with kana, Latin with Han, Hangul or Katakana, brand names with digits, emoji.
* **False positives found by this battery are pinned by class** (``…_stated``).
* The rules are for drafts only: explanation text is screened as before.
"""

from __future__ import annotations

import pytest

from app.claims.unicode_guard import WITHHELD_BLANKS
from app.creative.service import TEXT_LIMITS
from app.llm.screening import screen_creative, screen_explanation


def _screen(body: str, headline: str = "", cta: str = ""):
    return screen_creative(
        {"headline": headline, "body": body, "call_to_action": cta}, TEXT_LIMITS
    )


# --------------------------------------------------------------------------- #
# Blank characters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", sorted(WITHHELD_BLANKS), ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("field", ["headline", "body", "cta"])
def test_a_draft_with_a_blank_character_is_withheld(blank, field):
    texts = {"headline": "Weekend news", "body": "Plenty of room.", "cta": "Visit"}
    texts[field] = f"Try free{blank}parking forever"
    screened = _screen(texts["body"], texts["headline"], texts["cta"])
    assert (screened.output, screened.withheld_reason) == (None, "blank_character")


def test_a_braille_blank_between_spaces_is_withheld_too():
    screened = _screen("Try free \u2800 parking forever today.")
    assert screened.withheld_reason == "blank_character"


@pytest.mark.parametrize(
    "invisible, name",
    [
        ("\u3164", "HANGUL FILLER"), ("\uffa0", "HALFWIDTH HANGUL FILLER"),
        ("\u115f", "HANGUL CHOSEONG FILLER"), ("\u1160", "HANGUL JUNGSEONG FILLER"),
        ("\u200b", "ZERO WIDTH SPACE"), ("\u2060", "WORD JOINER"),
        ("\ufeff", "ZERO WIDTH NO-BREAK SPACE"), ("\u00ad", "SOFT HYPHEN"),
        ("\u034f", "COMBINING GRAPHEME JOINER"), ("\u180e", "MONGOLIAN VOWEL SEPARATOR"),
    ],
)
def test_fillers_and_zero_width_characters_are_removed_not_withheld_stated(invisible, name):
    """STATED, the decision: both forms remove these (``Default_Ignorable_Code_Point``), so the
    stored draft never shows a blank and its reader sees the text the review matched."""
    screened = _screen(f"Try free{invisible}parking today.")
    assert screened.withheld_reason is None, name
    assert screened.output["body"] == "Try freeparking today."
    assert "freeparking" in screened.screening_text


@pytest.mark.parametrize("joiner", ["\u200c", "\u200d"])
def test_joiners_between_letters_are_kept_in_the_stored_text_as_before(joiner):
    persian = f"\u0645\u06cc{joiner}\u062e\u0648\u0627\u0647\u0645"
    screened = _screen(f"{persian} today.")
    assert screened.withheld_reason is None
    assert screened.output["body"] == f"{persian} today."


# --------------------------------------------------------------------------- #
# Mixed script within a word
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "word",
    [
        "p\u0430rking",  # Cyrillic a
        "fr\u0435\u0435",  # Cyrillic ie, twice
        "f\u03bfrever",  # Greek omicron
        "PA\u0420KING",  # Cyrillic capital ER
        "\u0570ello",  # Armenian ho
        "\u13aapple",  # Cherokee A
        "p\u200c\u0430\u200crking",  # ZWNJ between the letters does not split the word
        "c\u0430f\u00e9",  # with an accented letter
    ],
)
@pytest.mark.parametrize("field", ["headline", "body", "cta"])
def test_a_draft_with_a_mixed_script_word_is_withheld(word, field):
    texts = {"headline": "Weekend news", "body": "Plenty of room.", "cta": "Visit"}
    texts[field] = f"Our {word} offer"
    screened = _screen(texts["body"], texts["headline"], texts["cta"])
    assert (screened.output, screened.withheld_reason) == (None, "mixed_script")


def test_an_earlier_rule_names_the_reason_first():
    assert _screen("Try free\u2800parking at deals.shop").withheld_reason == "link"
    assert _screen("Try p\u0430rking\u2800today").withheld_reason == "blank_character"


# --------------------------------------------------------------------------- #
# Legitimate text: passes, stored exactly as written
# --------------------------------------------------------------------------- #
LEGITIMATE = [
    "Zo\u00eb, Bj\u00f6rk, Jos\u00e9 and Nguy\u1ec5n welcome you.",
    "S\u00f8ren, \u0141ukasz, \u015eebnem, \u00d0or\u0111e and Ay\u015fe run the market.",
    "Fran\u00e7ois and M\u00fcller-L\u00fcdenscheidt bring cr\u00e8me br\u00fbl\u00e9e.",
    "\u0391\u03b8\u03ae\u03bd\u03b1 and "
    "\u0398\u03b5\u03c3\u03c3\u03b1\u03bb\u03bf\u03bd\u03af\u03ba\u03b7 fans are welcome.",
    "\u041c\u043e\u0441\u043a\u0432\u0430 and \u041a\u0438\u0457\u0432 editions are here.",
    "\u041c\u043e\u0441\u043a\u0432\u0430-Moscow edition (a hyphen separates the words).",
    "\u6771\u4eac\u30bf\u30ef\u30fc\u3078\u3088\u3046\u3053\u305d\u3002",
    "iPhone\u5145\u96fb\u5668 and \uc0bc\uc131Galaxy deals.",
    "Pok\u00e9mon\u30ab\u30fc\u30c9 fans and \u6ce8\u97f3\u3105\u3106 readers.",
    "7-Eleven, 3M, Web3, B2B, 4K and R2D2 fans: open 24/7.",
    "Celebrate \U0001f389\U0001f468\u200d\U0001f469\u200d\U0001f467 with us "
    "\U0001f1ec\U0001f1e7 \U0001f44d\U0001f3fd 1\ufe0f\u20e3",
    "Caf\u00e9 \u2615 \u2014 \u201cthe best in town\u201d \u00a35 \u20ac6 \u00b0C 50% off\u2122",
]


@pytest.mark.parametrize("body", LEGITIMATE)
def test_legitimate_text_passes_and_is_stored_as_written(body):
    screened = _screen(body, "Weekend at the market", "Visit us")
    assert screened.withheld_reason is None, body
    assert screened.output == {
        "headline": "Weekend at the market", "body": body, "call_to_action": "Visit us"
    }


@pytest.mark.parametrize(
    "body, word, why",
    [
        ("Only 500\u03bcg per dose.", "500\u03bcg", "Greek mu glued to Latin g"),
        ("Only 5\u00b5g per dose.", "5\u03bcg", "micro sign: NFKC makes it Greek mu"),
        ("A 10k\u03a9 resistor.", "10k\u03a9", "Greek omega glued to Latin k"),
        ("Follow us on VK\u043e\u043d\u0442\u0430\u043a\u0442\u0435.",
         "VK\u043e\u043d\u0442\u0430\u043a\u0442\u0435", "a mixed-script brand name"),
        ("Tag #\u041c\u043e\u0441\u043a\u0432\u0430_Moscow now.",
         "\u041c\u043e\u0441\u043a\u0432\u0430_Moscow", "an underscore joins the words"),
        ("Rich in \u03b2carotene.", "\u03b2carotene", "a Greek letter glued to Latin"),
    ],
)
def test_mixed_script_false_positives_are_deliberate_stated(body, word, why):
    """STATED, the price of failing closed (UTS #39 Highly Restrictive admits no Greek or
    Cyrillic with Latin in one word): these legitimate drafts are withheld."""
    from app.claims.unicode_guard import mixed_script_word

    assert _screen(body).withheld_reason == "mixed_script", why
    assert mixed_script_word(_screen(body).screening_text) == word


def test_explanation_text_does_not_get_the_draft_rules():
    """Scope: the explanation screen holds no protected claim and is unchanged."""
    output = {
        "why_it_matters": "Families mention p\u0430rking and free\u2800spaces.",
        "who_cares": "Local families", "ai_inference": "", "recommended_action": "Share a map.",
        "risk_note": "", "suggested_angles": ["Easy visits"],
    }
    screened = screen_explanation(output, None)
    assert screened.withheld_reason is None and screened.output is not None
