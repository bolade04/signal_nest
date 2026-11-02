"""Project Phase 5C-W4 adversarial battery, item 4: the output-side content review.

The 5C-W4 lane (Phase 5C plan §5) attacks the review that decides whether a generated draft
can be approved (:func:`app.claims.engine.review_against_library`, stored as a
``content_reviews`` row) and the approval-time re-check
(:func:`app.creative.service.draft_approval_refusal`, which re-reads the library "now",
W21 review r1 O-4). Everything runs through the real ``creative.generate`` job on the mock,
with the model's text fixed by the test, so each case exercises exactly the persisted text the
review and the re-check read.

* **Normalisation attacks** on blocked claims and required disclaimers — full-width forms,
  ZWJ/ZWNJ and the other invisibles, variation selectors, the soft hyphen, bidi controls,
  NFKC-foldable look-alikes, case and whitespace, and the 。/．/｡ stops — on the text side
  (the draft carries the attacked form, the library the plain one) and on the entry side (the
  library holds the attacked form, the draft the plain one), in the stored review and in the
  re-check alike.
* **Stated semantics, pinned.** A library entry matches as a case-insensitive *substring* of
  the folded text (:func:`app.claims.engine.contains_phrase`): a blocked claim inside a longer
  word blocks ("cure" blocks "secure"), the three text fields are matched as one text joined by
  a line break, punctuation is significant, an entry that folds to nothing matches nothing,
  and — the disclaimer side of the same rule — a required disclaimer is resolved by any text
  that contains it, even inside a longer word. Cross-script confusables (Cyrillic "е" for
  Latin "e") and non-whitespace blanks (U+2800) are outside the canonical form.
* **Retired claims** never block or require; retiring and re-adding a text blocks again.
* **Claims and disclaimers changed after a review** (the O-4 re-read) through the stewards'
  ``PATCH /claims`` route: a later blocking kind or wording refuses approval through the 5B
  route, a changed required disclaimer the draft lacks refuses it, and the stored review of a
  blocked draft governs even after its entry is changed.
* **One form on both sides**: for a seeded corpus of attacked texts, the review's input and
  the re-check's input fold to the same string.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.approvals.models import Approval
from app.campaign_context.models import ClaimsLibraryEntry
from app.claims.engine import LibraryEntry, contains_phrase, fold, review_against_library
from app.core.config import get_settings
from app.creative.models import ContentReview, CreativeDraft
from app.creative.schemas import CREATIVE_BOUNDS
from app.creative.service import TEXT_LIMITS, _draft_text, draft_approval_refusal
from app.llm.mock import MockLLMProvider
from app.llm.screening import screen_creative
from app.tests._creative_support import CEILINGS, CreativeWorld, creative_world

GENEROUS = {**CEILINGS, "llm_max_calls_per_workspace_per_day": 10_000}


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("w4-review") / "review.db", "w4rev") as w:
        yield w


@contextmanager
def _ceilings_set() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        for name, value in GENEROUS.items():
            mp.setattr(get_settings(), name, value)
        yield


@pytest.fixture(autouse=True)
def ceilings() -> Iterator[None]:
    with _ceilings_set():
        yield


@contextmanager
def _model_says(headline: str, body: str, cta: str = "") -> Iterator[None]:
    """The mock's ``generate_creative`` answer, fixed for the duration (offline, schema-valid)."""
    output = {"headline": headline, "body": body, "call_to_action": cta}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(MockLLMProvider, "_creative", lambda self, inputs, rng: dict(output))
        yield


def _generate(world: CreativeWorld, ws: str, *, headline: str = "", body: str,
              cta: str = "") -> tuple[dict, dict]:
    """One draft through the route and the real worker; returns (draft row, review row)."""
    brief_id = world.seed_brief(ws)
    with _model_says(headline, body, cta):
        job = world.generated(brief_id, ws=ws)
    assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
    [draft] = world.rows(CreativeDraft, brief_id=brief_id)
    [review] = world.rows(ContentReview, draft_id=draft["id"])
    return draft, review


def _refusal(world: CreativeWorld, draft_id: str) -> str | None:
    with world.session() as s:
        return draft_approval_refusal(s, s.get(CreativeDraft, draft_id))


def _retire(world: CreativeWorld, entry_id: str, when: datetime | None = None) -> None:
    table = ClaimsLibraryEntry.__table__
    with world.witness.begin() as conn:
        conn.execute(update(table).where(table.c.id == entry_id)
                     .values(retired_at=when or datetime.now(UTC)))


# --------------------------------------------------------------------------- #
# The attacks
# --------------------------------------------------------------------------- #
def _inside(char: str) -> Callable[[str], str]:
    """``char`` inserted inside the second word (after its third letter)."""

    def attack(text: str) -> str:
        words = text.split(" ")
        words[1] = words[1][:3] + char + words[1][3:]
        return " ".join(words)

    return attack


def _wrap(open_: str, close: str) -> Callable[[str], str]:
    """The second word wrapped in a pair of controls (bidi embeddings, overrides, isolates)."""

    def attack(text: str) -> str:
        words = text.split(" ")
        words[1] = open_ + words[1] + close
        return " ".join(words)

    return attack


def _spaces(space: str) -> Callable[[str], str]:
    return lambda text: text.replace(" ", space)


def _map_letters(base_lower: int, base_upper: int | None = None) -> Callable[[str], str]:
    """Each ASCII letter mapped into a compatibility alphabet that NFKC folds back."""

    def attack(text: str) -> str:
        out = []
        for ch in text:
            if "a" <= ch <= "z":
                out.append(chr(base_lower + ord(ch) - ord("a")))
            elif "A" <= ch <= "Z" and base_upper is not None:
                out.append(chr(base_upper + ord(ch) - ord("A")))
            else:
                out.append(ch)
        return "".join(out)

    return attack


def _fullwidth(text: str) -> str:
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text)


def _variation_selector(selector: str) -> Callable[[str], str]:
    """A variation selector after the first letter of the second word."""

    def attack(text: str) -> str:
        words = text.split(" ")
        words[1] = words[1][:1] + selector + words[1][1:]
        return " ".join(words)

    return attack


#: (id, attack, plain claim) — every claim distinct and no claim a substring of another, so
#: one draft can carry all of them and each hit is attributable to its own entry. Neutral
#: words only: nothing here trips the claim-safety patterns or the output screen.
ATTACKS: list[tuple[str, Callable[[str], str], str]] = [
    ("fullwidth", _fullwidth, "free zorblax parking"),
    ("zwj", _inside("‍"), "free quintor parking"),
    ("zwnj", _inside("‌"), "free vexlamp parking"),
    ("zero_width_space", _inside("​"), "free brannoc parking"),
    ("word_joiner", _inside("⁠"), "free stoltig parking"),
    ("bom", _inside("﻿"), "free plimver parking"),
    ("soft_hyphen", _inside("­"), "free gandrel parking"),
    ("combining_grapheme_joiner", _inside("͏"), "free morvish parking"),
    ("mongolian_vowel_separator", _inside("᠎"), "free tavrell parking"),
    ("hangul_filler", _inside("ㅤ"), "free dumbrel parking"),
    ("halfwidth_hangul_filler", _inside("ﾠ"), "free krostin parking"),
    ("tag_character", _inside("\U000e0041"), "free yelmond parking"),
    ("lrm", _inside("‎"), "free cobwick parking"),
    ("rlm", _inside("‏"), "free harloot parking"),
    ("arabic_letter_mark", _inside("؜"), "free nimbrel parking"),
    ("vs16", _variation_selector("️"), "free pestwin parking"),
    ("vs1", _variation_selector("︀"), "free ludgate parking"),
    ("vs17_supplement", _variation_selector("\U000e0100"), "free fornick parking"),
    ("mongolian_fvs", _variation_selector("᠋"), "free jaspern parking"),
    ("rlo_pdf", _wrap("‮", "‬"), "free wendrix parking"),
    ("lre_pdf", _wrap("‪", "‬"), "free orbleck parking"),
    ("lro_pdf", _wrap("‭", "‬"), "free sparvin parking"),
    ("rli_pdi", _wrap("⁧", "⁩"), "free tumbril parking"),
    ("fsi_pdi", _wrap("⁨", "⁩"), "free glassen parking"),
    ("upper", str.upper, "free hollick parking"),
    ("title", str.title, "free drosket parking"),
    ("swapcase_mixed", lambda t: "".join(c.upper() if i % 2 else c for i, c in enumerate(t)),
     "free mantrel parking"),
    ("nbsp", _spaces(" "), "free cravent parking"),
    ("narrow_nbsp", _spaces(" "), "free pellish parking"),
    ("ideographic_space", _spaces("　"), "free wortlen parking"),
    ("em_space", _spaces(" "), "free blissom parking"),
    ("figure_space", _spaces(" "), "free gruntle parking"),
    ("ogham_space", _spaces(" "), "free zephlin parking"),
    ("tab", _spaces("\t"), "free cartmel parking"),
    ("newline", _spaces("\n"), "free fendrel parking"),
    ("crlf_runs", _spaces(" \r\n  "), "free skirvin parking"),
    ("line_separator", _spaces(" "), "free palster parking"),
    ("math_bold", _map_letters(0x1D41A, 0x1D400), "free trondle parking"),
    ("math_sans_bold", _map_letters(0x1D5EE, 0x1D5D4), "free whimbel parking"),
    ("math_monospace", _map_letters(0x1D68A, 0x1D670), "free carvell parking"),
    ("circled", _map_letters(0x24D0, 0x24B6), "free lompsey parking"),
    ("ligatures", lambda t: t.replace("ffl", "ﬄ").replace("fi", "ﬁ"),
     "free snifflet parking"),
    ("decomposed_accent", lambda t: t.replace("é", "é"), "free café bolvin parking"),
    ("accent_split_by_cgj", lambda t: t.replace("é", "e͏́"),
     "free café drumlet parking"),
    ("ideographic_stop", lambda t: t.replace(".", "。"), "必ず治ります zorvan."),
    ("fullwidth_stop", lambda t: t.replace(".", "．"), "必ず効きます quellin."),
    ("halfwidth_ideographic_stop", lambda t: t.replace(".", "｡"), "必ず痩せます hestrel."),
    ("sharp_s_casefold", lambda t: t.replace("ss", "ß"), "free grossel parking"),
]
ATTACK_IDS = [a[0] for a in ATTACKS]


def test_the_attack_table_is_well_formed():
    claims = [claim for _, _, claim in ATTACKS]
    assert len(ATTACKS) <= CREATIVE_BOUNDS["review_items"]  # every hit fits the stored list
    assert len(set(ATTACK_IDS)) == len(ATTACKS)
    for i, a in enumerate(claims):
        for j, b in enumerate(claims):
            assert i == j or fold(a) not in fold(b), (a, b)
    for name, attack, claim in ATTACKS:
        assert attack(claim) != claim, name  # every attack changes the text it is given


def _carrying(texts: list[str]) -> str:
    return " ".join(f"Note: {t}." if not t.endswith((".", "。", "．", "｡")) else
                    f"Note: {t}" for t in texts)


@pytest.fixture(scope="module")
def blocked_matrix(world: CreativeWorld) -> dict[str, dict]:
    """Every attack, both sides, for blocked claims — one draft per side carries them all.

    * text side: the library holds each plain claim, the draft carries each attacked form;
    * entry side: the library holds each attacked form, the draft carries each plain claim;
    * re-check, both sides: a draft reviewed with an empty library (passed), then each entry
      added (the claim is "active now"), the re-check read, and the entry retired again.
    """
    out: dict[str, dict] = {name: {} for name in ATTACK_IDS}
    with _ceilings_set():
        for side in ("text", "entry"):
            ws = world.workspace(f"matrix blocked {side}")
            ids = {}
            for name, attack, claim in ATTACKS:
                ids[name] = world.claim(claim if side == "text" else attack(claim), ws=ws)
            texts = [attack(c) if side == "text" else c for _, attack, c in ATTACKS]
            draft, review = _generate(world, ws, body=_carrying(texts))
            for name in ATTACK_IDS:
                out[name][f"{side}_review_hit"] = ids[name] in review["blocked_claim_ids"]
            out["_reviews"] = out.get("_reviews", []) + [review]
            out["_refusals"] = out.get("_refusals", []) + [_refusal(world, draft["id"])]

            later = world.workspace(f"matrix recheck {side}")
            draft, review = _generate(world, later, body=_carrying(texts))
            assert review["passed"] is True and review["blocked_claim_ids"] == []
            for name, attack, claim in ATTACKS:
                entry = world.claim(claim if side == "text" else attack(claim), ws=later)
                out[name][f"{side}_recheck"] = _refusal(world, draft["id"])
                _retire(world, entry)
                out[name][f"{side}_after_retire"] = _refusal(world, draft["id"])
    return out


@pytest.mark.parametrize("attack", ATTACK_IDS)
def test_an_attacked_blocked_claim_blocks_the_stored_review_on_either_side(blocked_matrix, attack):
    result = blocked_matrix[attack]
    assert result["text_review_hit"], "the draft's attacked form hid the claim from the review"
    assert result["entry_review_hit"], "the entry's attacked form never blocks the plain text"


@pytest.mark.parametrize("attack", ATTACK_IDS)
def test_an_attacked_blocked_claim_added_after_the_review_refuses_approval(blocked_matrix, attack):
    result = blocked_matrix[attack]
    assert result["text_recheck"] == "blocked_claim"
    assert result["entry_recheck"] == "blocked_claim"
    assert result["text_after_retire"] is None and result["entry_after_retire"] is None


def test_the_matrix_drafts_are_stored_blocked_and_unapprovable(blocked_matrix):
    for review in blocked_matrix["_reviews"]:
        assert (review["is_blocked"], review["risk_level"], review["passed"]) == (
            True, "blocked", False
        )
        assert len(review["blocked_claim_ids"]) == len(ATTACKS)
    assert blocked_matrix["_refusals"] == ["review_blocked", "review_blocked"]


#: Disclaimers: each attack applied to a distinct required disclaimer.
DISCLAIMERS = [
    (name, attack, (claim.replace("free ", "Offer ").replace(" parking", " terms apply")
                    if not claim.endswith(".") else claim))
    for name, attack, claim in ATTACKS
]


@pytest.fixture(scope="module")
def disclaimer_matrix(world: CreativeWorld) -> dict[str, dict]:
    """Every attack, both sides, for required disclaimers (library entries and live offers),
    in the stored review and in the re-check."""
    out: dict[str, dict] = {name: {} for name in ATTACK_IDS}
    with _ceilings_set():
        for side in ("text", "entry", "offer"):
            ws = world.workspace(f"matrix disclaimers {side}")
            for _, attack, text in DISCLAIMERS:
                stored = text if side == "text" else attack(text)
                if side == "offer":
                    world.offer(stored, ws=ws)
                else:
                    world.claim(stored, kind="required_disclaimer", ws=ws)
            texts = [attack(t) if side == "text" else t for _, attack, t in DISCLAIMERS]
            draft, review = _generate(world, ws, body=_carrying(texts))
            out["_" + side] = review
            out["_refusal_" + side] = _refusal(world, draft["id"])

        later = world.workspace("matrix disclaimers recheck")
        texts = [attack(t) for _, attack, t in DISCLAIMERS]
        draft, review = _generate(world, later, body=_carrying(texts))
        assert review["passed"] is True
        for name, _attack, text in DISCLAIMERS:
            entry = world.claim(text, kind="required_disclaimer", ws=later)
            out[name]["recheck_plain_entry"] = _refusal(world, draft["id"])
            _retire(world, entry)
        plain = world.workspace("matrix disclaimers recheck entry")
        draft, _ = _generate(world, plain, body=_carrying([t for _, _, t in DISCLAIMERS]))
        for name, attack, text in DISCLAIMERS:
            entry = world.claim(attack(text), kind="required_disclaimer", ws=plain)
            out[name]["recheck_attacked_entry"] = _refusal(world, draft["id"])
            _retire(world, entry)
        world.claim("This exact disclaimer is carried nowhere.", kind="required_disclaimer",
                    ws=plain)
        out["_uncarried"] = _refusal(world, draft["id"])
    return out


@pytest.mark.parametrize("side", ["text", "entry", "offer"])
def test_every_attacked_required_disclaimer_resolves_on_either_side(disclaimer_matrix, side):
    review = disclaimer_matrix["_" + side]
    assert (review["disclaimers_resolved"], review["passed"]) == (True, True), side
    assert len(review["required_disclaimers"]) == len(DISCLAIMERS)
    assert disclaimer_matrix["_refusal_" + side] is None


@pytest.mark.parametrize("attack", ATTACK_IDS)
def test_an_attacked_disclaimer_required_after_the_review_still_resolves(disclaimer_matrix, attack):
    result = disclaimer_matrix[attack]
    assert result["recheck_plain_entry"] is None
    assert result["recheck_attacked_entry"] is None


def test_a_disclaimer_required_after_the_review_and_not_carried_refuses(disclaimer_matrix):
    assert disclaimer_matrix["_uncarried"] == "disclaimer_missing"


# --------------------------------------------------------------------------- #
# Homoglyph-adjacent: what the canonical form folds, and what it does not (stated)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "look_alike",
    [
        "ｆｒｅｅ ｐａｒｋｉｎｇ ｆｏｒｅｖｅｒ",  # fullwidth
        "𝐟𝐫𝐞𝐞 𝐩𝐚𝐫𝐤𝐢𝐧𝐠 𝐟𝐨𝐫𝐞𝐯𝐞𝐫",  # mathematical bold
        "𝖿𝗋𝖾𝖾 𝗉𝖺𝗋𝗄𝗂𝗇𝗀 𝖿𝗈𝗋𝖾𝗏𝖾𝗋",  # mathematical sans-serif
        "ⓕⓡⓔⓔ ⓟⓐⓡⓚⓘⓝⓖ ⓕⓞⓡⓔⓥⓔⓡ",  # circled
        "FREE PARKING FOREVER",
    ],
)
def test_nfkc_foldable_look_alikes_are_the_same_claim(look_alike):
    assert fold(look_alike) == fold("free parking forever")


@pytest.mark.parametrize(
    "outside, why",
    [
        ("frее parking forever", "Cyrillic ie for Latin e (a cross-script confusable)"),
        ("free pаrking forever", "Cyrillic a for Latin a"),
        ("free parking fοrever", "Greek omicron for Latin o"),
        ("free⠀parking forever", "braille pattern blank: renders as a space, is not one"),
    ],
)
def test_cross_script_confusables_are_outside_the_canonical_form_stated(outside, why):
    """STATED LIMIT, pinned so a change is deliberate: the matching form is NFKC + invisibles
    removed + the three stops + whitespace collapse + casefold (``app.claims.engine.fold``).
    NFKC does not map one script's letters to another's, and U+2800 is not whitespace, so
    these forms of a blocked claim are not matched. (The output screen treats look-alike
    *domains* specially — ``_has_bare_domain`` — but claims have no confusable skeleton; a
    UTS #39 skeleton is an R4 / operator question, recorded in the 5CW4 hand-off.)"""
    entry = LibraryEntry("b1", "free parking forever", "blocked")
    review = review_against_library(f"Try {outside} today.", None, [entry])
    assert review.blocked_claim_ids == [], why
    assert not contains_phrase(fold(outside), fold("free parking forever")), why


# --------------------------------------------------------------------------- #
# Substring semantics, stated and pinned (both the review and the re-check)
# --------------------------------------------------------------------------- #
def test_a_blocked_claim_inside_a_longer_word_blocks(world):
    """STATED: an entry matches as a substring of the folded text, with no word boundary. A
    blocked "cure" therefore blocks "secure" and "procure" (over-blocking: fail closed), even
    though the claim-safety pattern ``\\bcure\\b`` itself does not fire on those words."""
    ws = world.workspace("substring blocked")
    entry = world.claim("cure", ws=ws)
    draft, review = _generate(world, ws, body="A secure lot to procure your weekend spot.")
    assert review["blocked_claim_ids"] == [entry] and review["is_blocked"] is True
    assert not any(f["category"] == "health_claim" for f in review["findings"])
    assert _refusal(world, draft["id"]) == "review_blocked"

    later = world.workspace("substring recheck")
    draft, review = _generate(world, later, body="A secure lot to procure your weekend spot.")
    assert review["passed"] is True
    world.claim("cure", ws=later)
    assert _refusal(world, draft["id"]) == "blocked_claim"


def test_a_required_disclaimer_inside_a_longer_word_resolves_stated(world):
    """STATED, the disclaimer side of the same substring rule: "Void where prohibited." is
    resolved by a draft that says "Avoid where prohibited." — the required text occurs in it
    as a substring. Fail-open at a word edge; recorded as an observation in the 5CW4 hand-off
    (a word-boundary rule for disclaimers is an R4 / operator question)."""
    ws = world.workspace("substring disclaimer")
    world.claim("Void where prohibited.", kind="required_disclaimer", ws=ws)
    draft, review = _generate(world, ws, body="Spaces are limited. Avoid where prohibited.")
    assert (review["disclaimers_resolved"], review["passed"]) == (True, True)
    assert _refusal(world, draft["id"]) is None


@pytest.mark.parametrize(
    "body",
    ["Try free-parking forever today.", "Try freeparking forever today.",
     "Try free_parking forever today.", "Try free. parking forever today."],
)
def test_punctuation_and_spacing_inside_a_claim_are_significant_stated(world, body):
    """STATED: only whitespace runs are collapsed; a hyphen, underscore, full stop or a missing
    space is part of the text, so these do not carry "free parking forever"."""
    ws = world.workspace("punctuation")
    world.claim("free parking forever", ws=ws)
    draft, review = _generate(world, ws, body=body)
    assert review["blocked_claim_ids"] == [] and review["passed"] is True
    assert _refusal(world, draft["id"]) is None


@pytest.mark.parametrize(
    "headline, body, cta",
    [
        ("Weekend news: free", "parking forever at the market.", ""),
        ("", "Weekend news at the market. Free", "parking forever"),
    ],
)
def test_a_claim_split_across_two_fields_is_one_text_on_both_sides(world, headline, body, cta):
    """STATED: headline, body and call to action are matched as one text joined by a line
    break (which folds to a space), in the review and in the re-check alike."""
    ws = world.workspace("across fields")
    entry = world.claim("free parking forever", ws=ws)
    draft, review = _generate(world, ws, headline=headline, body=body, cta=cta)
    assert review["blocked_claim_ids"] == [entry]
    later = world.workspace("across fields recheck")
    draft, review = _generate(world, later, headline=headline, body=body, cta=cta)
    assert review["passed"] is True
    world.claim("free parking forever", ws=later)
    assert _refusal(world, draft["id"]) == "blocked_claim"


@pytest.mark.parametrize("field", ["headline", "cta"])
def test_a_claim_only_in_the_headline_or_call_to_action_is_reviewed_and_rechecked(world, field):
    texts = {"headline": "", "body": "Plenty of room this weekend.", "cta": ""}
    texts[field] = "Free parking forever"
    ws = world.workspace(f"only {field}")
    entry = world.claim("free parking forever", ws=ws)
    _, review = _generate(world, ws, **texts)
    assert review["blocked_claim_ids"] == [entry] and review["passed"] is False
    later = world.workspace(f"only {field} recheck")
    draft, review = _generate(world, later, **texts)
    assert review["passed"] is True
    world.claim("free parking forever", ws=later)
    assert _refusal(world, draft["id"]) == "blocked_claim"
    disclaimer = world.workspace(f"only {field} disclaimer")
    world.claim("Free parking forever", kind="required_disclaimer", ws=disclaimer)
    _, review = _generate(world, disclaimer, **texts)
    assert review["disclaimers_resolved"] is True


@pytest.mark.parametrize("empty", ["​­", "   ", "⁠️‍"])
def test_an_entry_that_folds_to_nothing_blocks_and_requires_nothing(world, empty):
    """STATED: an entry whose canonical form is empty can neither be carried nor matched, so
    it is skipped by the review and the re-check (it never blocks every draft, and never makes
    every draft unapprovable)."""
    ws = world.workspace("empty entry")
    world.claim(empty, ws=ws)
    world.claim(empty, kind="required_disclaimer", ws=ws)
    draft, review = _generate(world, ws, body="Plenty of room this weekend.")
    assert (review["blocked_claim_ids"], review["required_disclaimers"], review["passed"]) == (
        [], [], True
    )
    assert _refusal(world, draft["id"]) is None


def test_a_one_letter_blocked_claim_blocks_any_text_holding_it_stated(world):
    ws = world.workspace("one letter")
    entry = world.claim("Q", ws=ws)
    _, review = _generate(world, ws, body="Quiet streets this weekend.")
    assert review["blocked_claim_ids"] == [entry]


def test_disclaimers_that_fold_alike_are_required_once(world):
    ws = world.workspace("dedupe")
    world.claim("Terms apply.", kind="required_disclaimer", ws=ws)
    world.claim("TERMS  APPLY．", kind="required_disclaimer", ws=ws)
    world.offer("terms apply。", ws=ws)
    _, review = _generate(world, ws, body="See you there. Terms apply.")
    assert len(review["required_disclaimers"]) == 1
    assert fold(review["required_disclaimers"][0]) == "terms apply."
    assert review["passed"] is True


# --------------------------------------------------------------------------- #
# Retired claims
# --------------------------------------------------------------------------- #
def test_a_retired_claim_never_blocks_and_re_adding_it_blocks_again(world):
    ws = world.workspace("retire re-add")
    first = world.claim("free parking forever", ws=ws)
    _retire(world, first)
    _, review = _generate(world, ws, body="Free parking forever this weekend.")
    assert review["blocked_claim_ids"] == [] and review["passed"] is True
    again = world.claim("Free Parking Forever", ws=ws)
    draft, review = _generate(world, ws, body="Free parking forever this weekend.")
    assert review["blocked_claim_ids"] == [again]  # only the active entry is named
    assert _refusal(world, draft["id"]) == "review_blocked"


def test_a_retired_disclaimer_is_not_required_but_its_active_successor_is(world):
    ws = world.workspace("retire disclaimer")
    old = world.claim("Old terms apply.", kind="required_disclaimer", ws=ws)
    _retire(world, old)
    world.claim("New terms apply.", kind="required_disclaimer", ws=ws)
    draft, review = _generate(world, ws, body="See you there. NEW TERMS APPLY。")
    assert review["required_disclaimers"] == ["New terms apply."]
    assert review["passed"] is True and _refusal(world, draft["id"]) is None


def test_any_retirement_time_retires_the_entry_stated(world):
    """STATED: an entry with any ``retired_at`` is retired — the soft-retire route sets it to
    the database's now, and a future value (a hand-edited row) is not a scheduled
    retirement."""
    ws = world.workspace("future retirement")
    entry = world.claim("free parking forever", ws=ws)
    _retire(world, entry, when=datetime.now(UTC) + timedelta(days=30))
    draft, review = _generate(world, ws, body="Free parking forever this weekend.")
    assert review["blocked_claim_ids"] == [] and _refusal(world, draft["id"]) is None


# --------------------------------------------------------------------------- #
# Claims and disclaimers changed after the review: the O-4 re-read, through the routes
# --------------------------------------------------------------------------- #
def _approve(world: CreativeWorld, ws: str, draft_id: str):
    return world.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/approvals", "reviewer",
                      {"decision": "approved", "subject_version": 1})


def _patch_claim(world: CreativeWorld, ws: str, entry_id: str, **changes) -> None:
    r = world.send("PATCH", f"/workspaces/{ws}/claims/{entry_id}", "compliance_reviewer",
                   changes)
    assert r.status_code == 200, r.text


def _error(r) -> tuple[int, str, str]:
    return r.status_code, r.json()["error"]["code"], r.json()["error"]["message"]


def test_an_entry_whose_kind_becomes_blocked_after_the_review_refuses_approval(world):
    ws = world.workspace("patch kind blocked")
    entry = world.claim("Locally owned since 1998", kind="approved", ws=ws)
    draft, review = _generate(world, ws, body="Locally owned since 1998. See you soon.")
    assert review["passed"] is True
    _patch_claim(world, ws, entry, kind="blocked")
    r = _approve(world, ws, draft["id"])
    assert _error(r)[:2] == (409, "approval_review_blocked")
    assert "now blocks" in _error(r)[2]
    assert world.count(Approval, draft_id=draft["id"]) == 0
    _patch_claim(world, ws, entry, kind="approved")
    assert _approve(world, ws, draft["id"]).status_code == 201


def test_a_blocked_entry_reworded_after_the_review_to_the_drafts_words_refuses(world):
    ws = world.workspace("patch text blocked")
    entry = world.claim("free valet forever", ws=ws)
    draft, review = _generate(world, ws, body="Free parking forever this weekend.")
    assert review["passed"] is True
    _patch_claim(world, ws, entry, text="FREE PARKING forever")
    assert _error(_approve(world, ws, draft["id"]))[:2] == (409, "approval_review_blocked")


def test_a_required_disclaimer_reworded_after_the_review_is_re_read(world):
    ws = world.workspace("patch disclaimer")
    entry = world.claim("Terms apply.", kind="required_disclaimer", ws=ws)
    draft, review = _generate(world, ws, body="See you there. Terms apply.")
    assert review["passed"] is True
    _patch_claim(world, ws, entry, text="Terms and conditions apply.")
    r = _approve(world, ws, draft["id"])
    assert _error(r)[:2] == (409, "approval_review_blocked")
    assert "now requires" in _error(r)[2]
    _patch_claim(world, ws, entry, text="TERMS　APPLY。")  # the carried text, re-formed
    assert _approve(world, ws, draft["id"]).status_code == 201


def test_a_required_disclaimer_changed_to_another_kind_after_the_review_is_not_required(world):
    ws = world.workspace("patch disclaimer kind")
    draft, _ = _generate(world, ws, body="See you there.")
    entry = world.claim("Terms apply.", kind="required_disclaimer", ws=ws)
    assert _error(_approve(world, ws, draft["id"]))[:2] == (409, "approval_review_blocked")
    _patch_claim(world, ws, entry, kind="approved")
    assert _approve(world, ws, draft["id"]).status_code == 201


def test_a_blocked_drafts_stored_review_governs_after_its_entry_is_changed(world):
    """The stored review is immutable evidence (plan §2): unblocking the entry later — a new
    kind or new words — leaves that version unapprovable; a new version is reviewed afresh."""
    ws = world.workspace("patch stored governs")
    entry = world.claim("free parking forever", ws=ws)
    draft, review = _generate(world, ws, body="Free parking forever this weekend.")
    assert review["is_blocked"] is True
    _patch_claim(world, ws, entry, kind="approved")
    _patch_claim(world, ws, entry, text="something else entirely")
    r = _approve(world, ws, draft["id"])
    assert _error(r)[:2] == (409, "approval_review_blocked")
    assert "content review blocked it" in _error(r)[2]


# --------------------------------------------------------------------------- #
# One form on both sides: the review's input and the re-check's input fold alike
# --------------------------------------------------------------------------- #
_ALPHABET = (
    list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ  ")
    + ["​", "‌", "‍", "⁠", "﻿", "­", "͏", "️",
       "\U000e0100", "‮", "‬", "⁦", "⁩", "‎", "؜", "ㅤ",
       " ", "　", " ", "\t", "\n", "。", "．", "｡", "ａ",
       "\U0001d41a", "ⓐ", "ﬁ", "é", "é", "ß", "Σ"]
)


def test_the_review_and_the_recheck_read_one_folded_text_for_any_persisted_draft():
    """For a seeded corpus of attacked texts, the review's input (``screen_creative``'s
    screening copy of the persisted fields) and the re-check's input (the persisted fields of
    the stored draft, ``_draft_text``) fold to the same string — so no form can pass one and
    trip the other."""
    rng = random.Random(5_0_4)
    for _ in range(400):
        fields = {
            name: "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 40)))
            for name in ("headline", "body", "call_to_action")
        }
        fields["body"] = "body " + fields["body"]
        screened = screen_creative(fields, TEXT_LIMITS)
        if screened.output is None:
            continue
        stored = CreativeDraft(
            headline=screened.output["headline"] or None, body=screened.output["body"],
            call_to_action=screened.output["call_to_action"] or None,
        )
        assert fold(screened.screening_text) == fold(_draft_text(stored)), fields


# --------------------------------------------------------------------------- #
# The entry's kind, case and whitespace (DEFECT-5CW4-2)
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT-5CW4-2: ClaimIn.kind is free text, so a steward's 'Blocked', 'BLOCKED' or "
        "' blocked' entry is stored (201) and silently never blocks: the engine compares the "
        "kind exactly with 'blocked' (app/claims/engine.py BLOCKED_KIND). The protected "
        "safety control fails open on a case or whitespace slip."
    ),
)
@pytest.mark.parametrize("kind", ["Blocked", "BLOCKED", " blocked", "blocked "])
def test_a_blocked_entry_whose_kind_differs_only_in_case_or_space_blocks_or_is_refused(
    world, kind
):
    ws = world.workspace("kind case")
    r = world.send("POST", f"/workspaces/{ws}/claims", "compliance_reviewer",
                   {"text": "free parking forever", "kind": kind})
    if r.status_code == 422:  # refused as an unknown kind: fail closed, acceptable
        return
    assert r.status_code == 201, r.text
    _, review = _generate(world, ws, body="Free parking forever this weekend.")
    assert review["is_blocked"] is True, (kind, review["blocked_claim_ids"])
