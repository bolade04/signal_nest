"""D-N4 item 39: how the 5C review matches protected text (:mod:`app.claims.engine`).

The operator's ruling (2026-11-12): "implement word-boundary checks, the blank-character
protection and reviewed confusable detection before real-provider activation. Cover
mixed-script and whole-script substitutions of protected claims. Do not defer that protection
solely because multilingual output is deferred. Preserve original text and test legitimate
names and false positives."

* **Presence** (a required disclaimer must appear; :func:`carries_phrase`): whole words only.
  The finding (W21-REV-R4 r1 F-1(b)): "Void where prohibited." was resolved by "Avoid where
  prohibited.". Both edges are tested, and no look-alike spelling carries a disclaimer.
* **Detection** (a blocked claim must not appear; :func:`contains_claim`): the canonical-form
  substring rule as before, plus the UTS #39 skeleton on both sides, so a Cyrillic or Greek
  look-alike — in one word (mixed script) or in whole words (whole script) — still blocks
  (F-1(a)). A word boundary is **not** adopted for detection: the battery below shows forms it
  would newly let pass. Detection is never weaker than before (a randomized property).
* **False positives** of the stricter detection are pinned by class, so they stay deliberate.
"""

from __future__ import annotations

import random

import pytest

from app.claims.engine import (
    LibraryEntry,
    carries_phrase,
    claim_forms,
    contains_claim,
    contains_phrase,
    fold,
    review_against_library,
)

VOID = "Void where prohibited."
TERMS = "Terms apply"


# --------------------------------------------------------------------------- #
# Presence: a required disclaimer is carried only as whole words
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, carried_before",
    [
        ("Spaces are limited. Avoid where prohibited.", True),  # the finding: the left edge
        ("2void where prohibited.", True),  # a digit continues a word
        ("_void where prohibited.", True),  # so does a connector
        ("Void where prohibited", False),  # the phrase's own full stop is part of it
        ("V\u043eid where prohibited.", False),  # Cyrillic o: no look-alike carries it
        ("V0id where prohibited.", False),  # digit zero
    ],
)
def test_a_disclaimer_inside_a_longer_word_or_misspelt_is_not_carried(text, carried_before):
    assert contains_phrase(fold(text), fold(VOID)) is carried_before  # the rule before item 39
    assert not carries_phrase(text, VOID)


@pytest.mark.parametrize(
    "text",
    ["Terms applying to all stores", "Terms apply2 days", "Terms apply_now", "Terms apply\u00e9"],
)
def test_a_disclaimer_glued_to_a_following_word_is_not_carried(text):
    """The right edge: a letter, digit, connector or combining mark after the phrase."""
    assert not carries_phrase(text, TERMS)
    assert contains_phrase(fold(text), fold(TERMS))  # the old rule carried every one


@pytest.mark.parametrize(
    "text",
    [
        VOID,
        "Offer void where prohibited.",
        "(Void where prohibited.)",
        "'Void where prohibited.'",
        "VOID WHERE PROHIBITED.",
        "Void   where\nprohibited.",
        "Sale ends Sunday.Void where prohibited.Extra copy",  # "." needs no boundary after it
        "\u2014Void where prohibited.",
        "\u6771\u4eacVoid where prohibited.",  # Han never continues a word
        "Avoid where prohibited. Void where prohibited.",  # a later whole-word occurrence
    ],
)
def test_a_disclaimer_at_word_boundaries_is_carried(text):
    assert carries_phrase(text, VOID)


@pytest.mark.parametrize(
    "text", ["Terms apply.", "Terms apply!", "terms apply", "Terms apply\u2014"]
)
def test_a_disclaimer_ending_in_a_letter_is_carried_before_punctuation_or_the_end(text):
    assert carries_phrase(text, TERMS)


def test_an_empty_disclaimer_is_never_carried():
    assert not carries_phrase("anything", "")
    assert not carries_phrase("anything", "\u200b\u00ad")
    assert not carries_phrase("anything", None)


def test_a_hyphen_is_a_word_boundary_stated():
    """STATED (UAX #29): a hyphen is a boundary, so "Non-void where prohibited." carries "Void
    where prohibited." although its sense differs. Recorded; the human approver reads it."""
    assert carries_phrase("Non-void where prohibited.", VOID)


def test_scripts_without_spaces_fall_back_to_substring_at_their_edges_stated():
    """STATED: Han, Hiragana and Thai never continue a word (UAX #29 needs a dictionary to
    segment them), so a disclaimer in running Japanese text is carried as a substring at those
    edges; Katakana continues Katakana."""
    assert carries_phrase("\u30bb\u30fc\u30eb\u54c1\u306f\u8fd4\u54c1\u4e0d\u53ef\u3067\u3059",
                          "\u8fd4\u54c1\u4e0d\u53ef")
    campaign = "\u30ad\u30e3\u30f3\u30da\u30fc\u30f3"  # キャンペーン
    assert carries_phrase(f"\u671f\u9593\u9650\u5b9a{campaign}", campaign)
    assert not carries_phrase(f"\u30b9\u30fc\u30d1\u30fc{campaign}", campaign)  # スーパー…


def test_the_review_resolves_disclaimers_only_at_word_boundaries():
    entry = LibraryEntry("d1", VOID, "required_disclaimer")
    bad = review_against_library("Spaces are limited. Avoid where prohibited.", None, [entry])
    assert bad.missing_disclaimers == [VOID] and not bad.passed
    good = review_against_library("Spaces are limited. Offer void where prohibited.", None,
                                  [entry], ["Terms apply"])
    assert good.missing_disclaimers == ["Terms apply"]
    good = review_against_library("Offer void where prohibited. Terms apply.", None, [entry],
                                  ["Terms apply"])
    assert good.passed and good.required_disclaimers == [VOID, "Terms apply"]


# --------------------------------------------------------------------------- #
# Detection: the confusable skeleton, both sides, both scripts' cases
# --------------------------------------------------------------------------- #
CLAIM = "free parking forever"

#: (id, the claim as attacked, the blocked claim): each attacked form is caught by detection
#: and was not before item 39.
LOOK_ALIKES = [
    ("cyrillic_a_in_word", "free p\u0430rking forever", CLAIM),
    ("cyrillic_ie_twice", "fr\u0435\u0435 parking forever", CLAIM),
    ("greek_omicron", "free parking f\u03bfrever", CLAIM),
    ("whole_script_cyrillic", "\u0435\u0430\u0455\u0443 \u0441\u0430\u0455\u04bb", "easy cash"),
    ("whole_script_greek_capitals", "\u039d\u039f \u03a4\u0391\u03a7", "no tax"),
    ("greek_capital_iota_in_word", "R\u0399SK FREE", "risk free"),
    ("latin_rn_for_m", "rnoney back", "money back"),
    ("latin_zero_for_o", "C00L all summer", "cool all summer"),
    ("braille_blank", "free\u2800parking forever", CLAIM),
    ("hangul_filler_kept", "free\u3164parking forever", CLAIM),
    ("latin_small_capitals",
     "\ua730\u0280\u1d07\u1d07 \u1d18\u1d00\u0280\u1d0b\u026a\u0274\u0262 forever", CLAIM),
]
LOOK_ALIKE_IDS = [c[0] for c in LOOK_ALIKES]


@pytest.mark.parametrize("attacked, claim", [c[1:] for c in LOOK_ALIKES], ids=LOOK_ALIKE_IDS)
def test_a_look_alike_of_a_blocked_claim_is_detected_on_either_side(attacked, claim):
    text, plain = f"Try {attacked} today.", f"Try {claim} today."
    assert not contains_phrase(fold(text), fold(claim))  # missed before item 39
    assert contains_claim(text, claim)  # the draft carries the look-alike
    assert contains_claim(plain, attacked)  # the library holds it


@pytest.mark.parametrize("attacked, claim", [c[1:] for c in LOOK_ALIKES], ids=LOOK_ALIKE_IDS)
def test_the_review_blocks_a_look_alike_and_names_its_entry(attacked, claim):
    entries = [LibraryEntry("b1", claim, "blocked"), LibraryEntry("b2", "unrelated", "blocked")]
    review = review_against_library(f"Try {attacked} today.", None, entries)
    assert review.blocked_claim_ids == ["b1"] and review.is_blocked
    assert f"Contains a blocked claim: '{fold(claim)}'" in review.safety.warnings
    entry_side = review_against_library(f"Try {claim} today.", None,
                                        [LibraryEntry("b3", attacked, "blocked")])
    assert entry_side.blocked_claim_ids == ["b3"] and entry_side.is_blocked


def test_capital_look_alikes_need_the_skeleton_before_case_folding():
    """"NO TAX" in Greek capitals (U+039D U+039F U+03A4 U+0391 U+03A7): folding first gives
    Greek small letters, whose skeleton starts "vo"; only the skeleton taken before folding
    reads "NO TAX" (form 3 of :func:`claim_forms`)."""
    text = "\u039d\u039f \u03a4\u0391\u03a7"
    forms = claim_forms(text)
    assert forms[1] != "no tax" and forms[2] == "no tax"
    assert contains_claim(text, "no tax") and contains_claim("no tax", text)


def test_lower_case_look_alikes_need_case_folding_before_the_skeleton():
    """Greek capital iota's (U+0399) prototype is "l", its small letter's is "i": only folding
    first reads "RISK" written with U+0399 as "risk" (form 2 of :func:`claim_forms`)."""
    text = "R\u0399SK FREE"
    forms = claim_forms(text)
    assert forms[1] == "risk free" and forms[2] != "risk free"
    assert contains_claim(text, "risk free")


def test_upper_case_text_with_a_lower_case_l_for_i_is_caught_only_by_a_capital_entry_stated():
    """STATED residual, single-script Latin: "RlSK FREE" (lower-case l for I) reads as RISK FREE
    in capitals. The skeleton maps capital I to l, so an entry written in capitals catches it,
    but an entry written in lower case does not (i and l are not confusable)."""
    assert contains_claim("Totally RlSK FREE trial.", "RISK FREE")
    assert not contains_claim("Totally RlSK FREE trial.", "risk free")


#: Capital look-alikes of a claim containing "i" (review W21-REV-DN439 r1 C-1): the skeleton
#: maps capital I to "l" and small "i" to itself, so these needed forms 6 and 7.
CAPITAL_I = [
    ("cyrillic_capitals", "\u0422\u041e\u0420 \u0420\u0406\u0421\u041a", "top pick"),
    ("greek_word_then_latin", "\u039d\u039f RISK", "no risk"),
    ("cyrillic_word_then_latin", "\u0422\u041e\u0420 PICK", "top pick"),
    ("greek_capitals", "\u039a\u0399\u03a4", "kit"),
    ("digit_zero_in_capitals", "FREE PARKING F0REVER", "free parking forever"),
]


@pytest.mark.parametrize("attacked, claim", [c[1:] for c in CAPITAL_I],
                         ids=[c[0] for c in CAPITAL_I])
def test_capital_look_alikes_of_a_claim_with_i_are_detected_on_either_side(attacked, claim):
    text, plain = f"Shop the {attacked} today.", f"Shop the {claim} today."
    assert not contains_phrase(fold(text), fold(claim))  # missed before item 39
    assert contains_claim(text, claim)  # the draft carries the look-alike
    assert contains_claim(plain, attacked)  # the library holds it
    forms_t, forms_c = claim_forms(text), claim_forms(claim)
    assert not any(c in t for c, t in zip(forms_c[:5], forms_t[:5], strict=True))
    assert forms_c[5] in forms_t[5]  # form 6: capital-I look-alikes read as "i"
    review = review_against_library(text, None, [LibraryEntry("b1", claim, "blocked")])
    assert review.blocked_claim_ids == ["b1"]


def test_a_capital_look_alike_split_by_a_blank_needs_form_7():
    """Review W21-REV-DN439 r2 FU-R2-1: form 7 (the capital-I reading of the text with its
    blanks read as spaces) is the only form that matches "TOP PICK" in Cyrillic capitals with a
    Braille blank (U+2800) between the words, as a library entry or a draft stored before the
    screen withheld blanks can hold it."""
    assert contains_claim("Shop the \u0422\u041e\u0420\u2800\u0420\u0406\u0421\u041a today.",
                          "top pick")


def test_blanks_read_as_spaces_and_invisibles_removed_are_both_matched():
    """A text that keeps a blank (a library entry, or a draft stored before the screen withheld
    blanks) is matched as it reads; one whose filler the matching form removes is still matched
    glued, with the skeleton too."""
    assert contains_claim("Try free parking forever.", "free\u2800parking forever")
    assert contains_claim("Try free parking forever.", "free\u3164parking forever")
    assert contains_claim("Try free dumbrel parking.", "fr\u0435\u0435 dum\u3164brel parking")
    assert not contains_phrase(fold("Try free dumbrel parking."),
                               fold("fr\u0435\u0435 dum\u3164brel parking"))


# --------------------------------------------------------------------------- #
# Why detection keeps the substring rule: the adversarial battery
# --------------------------------------------------------------------------- #
#: (id, text, claim, detected at the head). The word-boundary rule is
#: :func:`carries_phrase` applied to a blocked claim.
BATTERY = [
    ("glued_prefix", "Enjoy carefree parking forever.", "free parking forever", True),
    ("glued_suffix", "Free parking forevermore!", "free parking forever", True),
    ("glued_inside", "Enjoy ourfree parking forever.", "free parking forever", True),
    ("inflected", "It cures everything.", "cure", True),
    ("inflected_past", "Results guaranteed.", "guarantee", True),
    ("upper", "FREE PARKING FOREVER", "free parking forever", True),
    ("mixed_case", "fReE pArKiNg FoReVeR", "free parking forever", True),
    ("fullwidth", "\uff46\uff52\uff45\uff45 parking forever", "free parking forever", True),
    ("math_bold", "\U0001d41f\U0001d42b\U0001d41e\U0001d41e parking forever",
     "free parking forever", True),
    ("braille_split", "free\u2800parking forever", "free parking forever", True),
    ("cyrillic", "free p\u0430rking forever", "free parking forever", True),
    ("whole_script", "\u0435\u0430\u0455\u0443 \u0441\u0430\u0455\u04bb", "easy cash", True),
    # Recorded limitations, the same as before item 39 (a reader sees a different text):
    ("missing_space", "freeparking forever", "free parking forever", False),
    ("zero_width_for_space", "free\u200bparking forever", "free parking forever", False),
    ("hyphen_split", "free-parking forever", "free parking forever", False),
    ("dotted_letters", "f.r.e.e parking forever", "free parking forever", False),
    ("spaced_letters", "f r e e parking forever", "free parking forever", False),
]


@pytest.mark.parametrize("text, claim, detected", [b[1:] for b in BATTERY],
                         ids=[b[0] for b in BATTERY])
def test_the_detection_battery(text, claim, detected):
    assert contains_claim(text, claim) is detected
    if contains_phrase(fold(text), fold(claim)):
        assert detected  # never weaker than the rule before item 39


def test_a_word_boundary_would_newly_let_battery_forms_pass_so_detection_keeps_substring():
    """The evidence for keeping the substring rule: the old rule found every form below, and
    a word-boundary rule on the same canonical form (the presence rule) would let each pass."""
    newly_passing = [
        name for name, text, claim, _ in BATTERY
        if contains_phrase(fold(text), fold(claim)) and not carries_phrase(text, claim)
    ]
    assert newly_passing == ["glued_prefix", "glued_suffix", "glued_inside", "inflected",
                             "inflected_past"]


_ALPHABET = list("abcdeimnoprstuvxyzABCEHIKMNOPTXY01 ") + [
    "\u0430", "\u0435", "\u043e", "\u0440", "\u0441", "\u0443", "\u0445", "\u0410", "\u0415",
    "\u041c", "\u039d", "\u039f", "\u03a1", "\u03bf", "\u03b9", "\u2800", "\u3164", "\u200b",
    "\u00ad", "\uff41", "\u00e9", "e\u0301", "\u3002", ".", "-", "\n", "\u00a0", "\u0399",
]


#: Pairs only the canonical form matches: NFD reorders the combining marks at the claim's edge
#: (review W21-REV-DN439 r1 C-5; a small capital R and e with circumflex and dot below).
CANONICAL_ONLY = [("\u0280\u1ec7\u1ec7\u0323", "\u0280\u1ec7\u1ec7")]


@pytest.mark.parametrize("text", ["Free parking FOREVER", "\u0280\u1ec7\u1ec7\u0323",
                                  "Try fr\u0435\u0435\u2800parking", "  Terms\u00a0apply\u3002 "])
def test_the_first_form_is_the_canonical_form(text):
    assert claim_forms(text)[0] == fold(text)


def test_detection_is_never_weaker_than_the_rule_before_item_39():
    """Property, seeded: whenever the old rule (the canonical-form substring) finds a claim,
    :func:`contains_claim` does, for claims cut from the text and for random ones, and for the
    pairs that only the canonical form matches."""
    for text, claim in CANONICAL_ONLY:
        assert contains_phrase(fold(text), fold(claim))
        assert not any(c in t for c, t in zip(claim_forms(claim)[1:], claim_forms(text)[1:],
                                               strict=True))
        assert contains_claim(text, claim)
    rng = random.Random(39)
    checked = 0
    for _ in range(3000):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 30)))
        if rng.random() < 0.7:
            i = rng.randrange(len(text))
            claim = text[i:i + rng.randint(1, 12)]
        else:
            claim = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(1, 6)))
        if contains_phrase(fold(text), fold(claim)):
            checked += 1
            assert contains_claim(text, claim), (text, claim)
    assert checked > 1500


# --------------------------------------------------------------------------- #
# Legitimate text and the false positives of stricter detection
# --------------------------------------------------------------------------- #
LIBRARY = [
    LibraryEntry(f"b{i}", claim, "blocked")
    for i, claim in enumerate([
        "cure", "guaranteed results", "free parking forever", "no tax", "easy cash",
        "100% safe", "risk free", "#1", "clinically proven", "lose weight fast", "top",
        "burn fat",
    ])
] + [LibraryEntry("d1", VOID, "required_disclaimer")]

LEGITIMATE = [
    "Zo\u00eb, Bj\u00f6rk, Jos\u00e9 and Nguy\u1ec5n welcome you to our caf\u00e9.",
    "S\u00f8ren, \u0141ukasz, \u015eebnem and \u00d0or\u0111e run the weekend market.",
    "\u0391\u03b8\u03ae\u03bd\u03b1 and \u041c\u043e\u0441\u043a\u0432\u0430 editions are here.",
    "\u6771\u4eac\u30bf\u30ef\u30fc and iPhone\u5145\u96fb\u5668 deals this week.",
    "\uc0bc\uc131Galaxy owners and Pok\u00e9mon\u30ab\u30fc\u30c9 fans are welcome.",
    "7-Eleven, 3M, Web3, B2B and 4K fans: open 24/7.",
    "Celebrate \U0001f389\U0001f468\u200d\U0001f469\u200d\U0001f467 with us "
    "\U0001f1ec\U0001f1e7 \U0001f44d\U0001f3fd 1\ufe0f\u20e3",
]


@pytest.mark.parametrize("text", LEGITIMATE)
def test_legitimate_names_and_scripts_hit_no_blocked_claim(text):
    review = review_against_library(f"{text} Void where prohibited.", None, LIBRARY)
    assert review.blocked_claim_ids == [] and review.passed, review.safety.warnings


@pytest.mark.parametrize(
    "text, claim, why",
    [
        ("A secure lot.", "cure", "substring inside a longer word (as before item 39)"),
        ("A carefree day.", "free", "substring inside a longer word (as before item 39)"),
        ("Stop by today.", "top", "substring inside a longer word (as before item 39)"),
        ("Visit the bum fat-free bakery.", "burn fat", "skeleton: m reads as rn"),
        ("Visit the Com Ranch.", "corn ranch", "skeleton: m reads as rn"),
        ("\u0422\u043e\u0440 Shop opens.", "top shop",
         "whole-script: Russian \u0422\u043e\u0440 reads as Latin Top"),
    ],
)
def test_false_positives_of_detection_are_deliberate_stated(text, claim, why):
    """STATED, the price of failing closed: these legitimate texts are blocked by the claim."""
    assert contains_claim(text, claim), why


def test_matching_reads_and_never_changes_the_texts():
    """Original text preserved: the review returns every disclaimer as written and leaves
    its inputs alone; the forms are for matching only."""
    text = "R\u0399SK FREE \u2800 Offer v\u043eid where prohibited."
    entries = [LibraryEntry("b1", "risk free", "blocked"),
               LibraryEntry("d1", "  Void where prohibited. ", "required_disclaimer")]
    before = (text, [(e.text, e.kind) for e in entries])
    review = review_against_library(text, None, entries, ["Terms\u00a0apply\u3002"])
    assert (text, [(e.text, e.kind) for e in entries]) == before
    assert review.required_disclaimers == ["Void where prohibited.", "Terms\u00a0apply\u3002"]
    assert review.missing_disclaimers == review.required_disclaimers
