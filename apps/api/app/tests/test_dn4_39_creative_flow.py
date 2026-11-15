"""D-N4 item 39 end to end: the findings through the real ``creative.generate`` job (mock
provider, the model's text fixed by the test), the stored review and the 5B approval route.

Each finding of W21-REV-R4 r1 F-1 passed review on main ``6a3afe3a`` (capture
``P6-W21-DN439-01/raw/003``) and is withheld or refused here:

* a Cyrillic letter inside a blocked claim, and any mixed-script word: withheld
  (``withheld:mixed_script``), nothing stored;
* a whole-script look-alike of a blocked claim (all-Cyrillic, all-Greek capitals): stored,
  review blocked, approval refused (409);
* a Braille blank inside a claim: withheld (``withheld:blank_character``), nothing stored;
* "Avoid where prohibited." for a required "Void where prohibited.": stored, review not
  passed, approval refused (409).

Legitimate text is stored exactly as the model wrote it and is approvable. The approval-time
re-check of entries added *after* the review applies the same rules. The PostgreSQL rows run
when ``TEST_POSTGRES_URL`` is set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.approvals.models import Approval
from app.campaign_context.models import ClaimsLibraryEntry
from app.claims.engine import contains_phrase, fold
from app.core.config import get_settings
from app.creative.models import ContentReview, CreativeDraft
from app.creative.service import draft_approval_refusal
from app.llm.mock import MockLLMProvider
from app.tests._creative_support import CEILINGS, CreativeWorld, creative_world

GENEROUS = {**CEILINGS, "llm_max_calls_per_workspace_per_day": 10_000}


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


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("dn439") / "dn439.db", "dn439") as w:
        yield w


@contextmanager
def _model_says(body: str, headline: str = "", cta: str = "") -> Iterator[None]:
    output = {"headline": headline, "body": body, "call_to_action": cta}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(MockLLMProvider, "_creative", lambda self, inputs, rng: dict(output))
        yield


def _run(world: CreativeWorld, ws: str, body: str, **fields) -> tuple[dict, str]:
    """One generation through the route and the real worker: (job row, brief id)."""
    brief_id = world.seed_brief(ws)
    with _model_says(body, **fields):
        job = world.generated(brief_id, ws=ws)
    return job, brief_id


def _approve(world: CreativeWorld, ws: str, draft_id: str):
    return world.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/approvals", "reviewer",
                      {"decision": "approved", "subject_version": 1})


def _refusal(world: CreativeWorld, draft_id: str) -> str | None:
    with world.session() as s:
        return draft_approval_refusal(s, s.get(CreativeDraft, draft_id))


CYRILLIC_IN_CLAIM = "Try free p\u0430rking forever today."
MIXED_WORD = "Visit our n\u0435w store this weekend."
BRAILLE_IN_CLAIM = "Try free\u2800parking forever today."
WHOLE_CYRILLIC = "Get \u0435\u0430\u0455\u0443 \u0441\u0430\u0455\u04bb today."
GREEK_CAPITALS = "Shop now: \u039d\u039f \u03a4\u0391\u03a7 this weekend."
AVOID = "Spaces are limited. Avoid where prohibited."
#: "TOP PICK" in Cyrillic capitals (review W21-REV-DN439 r1 C-1: a claim containing "i").
CYRILLIC_CAPITALS_WITH_I = "Shop the \u0422\u041e\u0420 \u0420\u0406\u0421\u041a today."


# --------------------------------------------------------------------------- #
# Withheld: nothing stored
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body, reason",
    [
        (CYRILLIC_IN_CLAIM, "mixed_script"),
        (MIXED_WORD, "mixed_script"),
        (BRAILLE_IN_CLAIM, "blank_character"),
    ],
    ids=["cyrillic_letter_in_claim", "mixed_script_word", "braille_blank_in_claim"],
)
def test_a_draft_with_a_mixed_script_word_or_a_blank_is_withheld_and_nothing_stored(
    world, body, reason
):
    ws = world.workspace(f"withheld {reason}")
    world.claim("free parking forever", ws=ws)
    job, brief_id = _run(world, ws, body)
    assert (job["status"], job["last_error_code"], job["last_error_summary"]) == (
        "failed", "non_retryable", f"withheld:{reason}"
    )
    assert world.count(CreativeDraft, brief_id=brief_id) == 0
    assert world.count(ContentReview, workspace_id=ws) == 0


# --------------------------------------------------------------------------- #
# Stored but blocked or not passed: approval refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body, kind, text",
    [
        (WHOLE_CYRILLIC, "blocked", "easy cash"),
        (GREEK_CAPITALS, "blocked", "no tax"),
        (AVOID, "required_disclaimer", "Void where prohibited."),
    ],
    ids=["whole_script_cyrillic", "whole_script_greek_capitals", "disclaimer_in_longer_word"],
)
def test_a_look_alike_claim_or_an_in_word_disclaimer_is_stored_unapprovable(
    world, body, kind, text
):
    ws = world.workspace(f"refused {kind} {text}")
    entry = world.claim(text, kind=kind, ws=ws)
    job, brief_id = _run(world, ws, body)
    assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
    [draft] = world.rows(CreativeDraft, brief_id=brief_id)
    [review] = world.rows(ContentReview, draft_id=draft["id"])
    assert draft["body"] == body  # stored exactly as the model wrote it
    assert review["passed"] is False
    if kind == "blocked":
        assert (review["is_blocked"], review["blocked_claim_ids"]) == (True, [entry])
        assert _refusal(world, draft["id"]) == "review_blocked"
    else:
        assert (review["disclaimers_resolved"], review["required_disclaimers"]) == (
            False, [text]
        )
        assert _refusal(world, draft["id"]) == "review_not_passed"
    r = _approve(world, ws, draft["id"])
    assert (r.status_code, r.json()["error"]["code"]) == (409, "approval_review_blocked")
    assert world.count(Approval, draft_id=draft["id"]) == 0


# --------------------------------------------------------------------------- #
# Legitimate text: stored as written, approvable
# --------------------------------------------------------------------------- #
LEGITIMATE = (
    "Zo\u00eb, Bj\u00f6rk, Jos\u00e9 and Nguy\u1ec5n welcome you. "
    "\u0391\u03b8\u03ae\u03bd\u03b1 and \u041c\u043e\u0441\u043a\u0432\u0430 editions, "
    "\u6771\u4eac\u30bf\u30ef\u30fc and iPhone\u5145\u96fb\u5668 deals, \uc0bc\uc131Galaxy "
    "fans, 7-Eleven and Web3 too \U0001f389\U0001f468\u200d\U0001f469\u200d\U0001f467 "
    "\U0001f1ec\U0001f1e7. Offer void where prohibited."
)
HEADLINE = "Caf\u00e9 weekend \u2615"
LIBRARY = [("free parking forever", "blocked"), ("easy cash", "blocked"), ("cure", "blocked"),
           ("Void where prohibited.", "required_disclaimer")]


def _legitimate_round_trip(world: CreativeWorld, ws: str) -> None:
    ids = [world.claim(text, kind=kind, ws=ws) for text, kind in LIBRARY]
    job, brief_id = _run(world, ws, LEGITIMATE, headline=HEADLINE, cta="Visit us")
    assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
    [draft] = world.rows(CreativeDraft, brief_id=brief_id)
    assert (draft["headline"], draft["body"], draft["call_to_action"]) == (
        HEADLINE, LEGITIMATE, "Visit us"
    )
    [review] = world.rows(ContentReview, draft_id=draft["id"])
    assert (review["passed"], review["blocked_claim_ids"]) == (True, [])
    assert review["required_disclaimers"] == ["Void where prohibited."]
    stored = {e["id"]: e["text"] for e in world.rows(ClaimsLibraryEntry, workspace_id=ws)}
    assert [stored[i] for i in ids] == [text for text, _ in LIBRARY]  # claims as written
    assert _refusal(world, draft["id"]) is None
    assert _approve(world, ws, draft["id"]).status_code == 201


def test_legitimate_names_and_scripts_are_stored_as_written_and_approvable(world):
    _legitimate_round_trip(world, world.workspace("legitimate"))


# --------------------------------------------------------------------------- #
# The approval re-check of entries added after the review uses the item-39 rules
# --------------------------------------------------------------------------- #
RECHECK = [
    # (draft body, kind of the entry added after the review, its text, the re-check's answer)
    (WHOLE_CYRILLIC, "blocked", "easy cash", "blocked_claim"),
    (GREEK_CAPITALS, "blocked", "no tax", "blocked_claim"),
    (CYRILLIC_CAPITALS_WITH_I, "blocked", "top pick", "blocked_claim"),
    (AVOID, "required_disclaimer", "Void where prohibited.", "disclaimer_missing"),
    ("Spaces are limited. Offer void where prohibited.", "required_disclaimer",
     "Void where prohibited.", None),
]


@pytest.mark.parametrize(
    "body, kind, text, refusal", RECHECK,
    ids=["whole_script_cyrillic", "whole_script_greek_capitals", "capitals_with_i",
         "disclaimer_in_longer_word", "disclaimer_carried"],
)
def test_the_recheck_of_entries_added_after_the_review_uses_the_item_39_rules(
    world, body, kind, text, refusal
):
    """The approval-time re-check (``app.creative.service.draft_approval_refusal``) reads the
    library *as it is now*, for an entry added or changed after the draft was reviewed, with
    the review's rules (D-N4 item 39) on the draft's text as written: a whole-script look-alike
    of a blocked claim added later refuses approval (in Greek capitals too, which folding the
    text first would lose, and in Cyrillic capitals for a claim containing "i"), a disclaimer
    added later is not carried by "Avoid where prohibited.", and one carried as whole words
    still is. On ``e4fb42fa`` the re-check used the rule before item 39 and approved the first
    four; on ``588d4086`` it still approved the capitals with "i" (review r1 C-1)."""
    ws = world.workspace(f"recheck {kind} {text} {refusal}")
    job, brief_id = _run(world, ws, body)
    [draft] = world.rows(CreativeDraft, brief_id=brief_id)
    [review] = world.rows(ContentReview, draft_id=draft["id"])
    assert review["passed"] is True  # reviewed with an empty library
    world.claim(text, kind=kind, ws=ws)
    old_rule = contains_phrase(fold(draft["body"]), fold(text))
    assert old_rule is (kind != "blocked")  # what the rule before item 39 answered
    assert _refusal(world, draft["id"]) == refusal
    r = _approve(world, ws, draft["id"])
    if refusal is None:
        assert r.status_code == 201, r.text
    else:
        assert (r.status_code, r.json()["error"]["code"]) == (409, "approval_review_blocked")
        assert world.count(Approval, draft_id=draft["id"]) == 0


# --------------------------------------------------------------------------- #
# PostgreSQL: the same outcomes, and the text round trip, on the production engine
# --------------------------------------------------------------------------- #
def test_postgres_findings_are_refused_and_legitimate_text_round_trips():
    from app.tests._creative_support import postgres_creative_world

    with postgres_creative_world("dn439") as pg:
        _legitimate_round_trip(pg, pg.workspace("pg legitimate"))
        ws = pg.workspace("pg withheld")
        job, brief_id = _run(pg, ws, BRAILLE_IN_CLAIM)
        assert job["last_error_summary"] == "withheld:blank_character"
        assert pg.count(CreativeDraft, brief_id=brief_id) == 0
        job, brief_id = _run(pg, ws, CYRILLIC_IN_CLAIM)
        assert job["last_error_summary"] == "withheld:mixed_script"
        ws = pg.workspace("pg look-alike")
        entry = pg.claim("easy cash", ws=ws)
        job, brief_id = _run(pg, ws, WHOLE_CYRILLIC)
        [draft] = pg.rows(CreativeDraft, brief_id=brief_id)
        [review] = pg.rows(ContentReview, draft_id=draft["id"])
        assert (draft["body"], review["blocked_claim_ids"]) == (WHOLE_CYRILLIC, [entry])
        assert _approve(pg, ws, draft["id"]).status_code == 409
        ws = pg.workspace("pg disclaimer")
        pg.claim("Void where prohibited.", kind="required_disclaimer", ws=ws)
        job, brief_id = _run(pg, ws, AVOID)
        [draft] = pg.rows(CreativeDraft, brief_id=brief_id)
        assert _approve(pg, ws, draft["id"]).status_code == 409
        ws = pg.workspace("pg recheck")
        job, brief_id = _run(pg, ws, GREEK_CAPITALS)
        [draft] = pg.rows(CreativeDraft, brief_id=brief_id)
        pg.claim("no tax", ws=ws)  # added after the review
        assert _refusal(pg, draft["id"]) == "blocked_claim"
        assert _approve(pg, ws, draft["id"]).status_code == 409
