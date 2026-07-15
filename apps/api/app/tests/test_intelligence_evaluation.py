"""Regression tests over the labeled intelligence evaluation dataset.

Every :class:`EvalCase` in ``app.intelligence.evaluation`` pins an expected
accept/reject outcome (and reason) that the deterministic core must reproduce. If
extraction, relevance, scoring or rejection drifts, one of these fails — which is
exactly the guardrail an explainable scoring product needs.
"""

from __future__ import annotations

import pytest

from app.intelligence.analyze import analyze_signal
from app.intelligence.evaluation import EVALUATION_CASES
from app.intelligence.evaluation.dataset import EVAL_CONTEXT


@pytest.mark.parametrize("case", EVALUATION_CASES, ids=lambda c: c.name)
def test_evaluation_case_outcome(case):
    # Fresh dedupe set per case so cases are independent.
    cand = analyze_signal(case.signal, EVAL_CONTEXT, seen_fingerprints=set())

    assert cand.accepted is case.expect_accepted, (
        f"{case.name}: accepted={cand.accepted} expected {case.expect_accepted} "
        f"(rejection={cand.rejection}, score={cand.score.total}, rel={cand.relevance.score})"
    )
    assert cand.rejection == case.expect_rejection, (
        f"{case.name}: rejection={cand.rejection} expected {case.expect_rejection}"
    )

    if case.expect_signal_type is not None:
        st = cand.intelligence.signal_type.value if cand.intelligence.signal_type else None
        assert st == case.expect_signal_type, f"{case.name}: signal_type={st}"
    if case.expect_pain_point is not None:
        pp = cand.intelligence.pain_point_dna.value if cand.intelligence.pain_point_dna else None
        assert pp == case.expect_pain_point, f"{case.name}: pain_point={pp}"
    if case.expect_buying_intent is not None:
        assert cand.intelligence.has_buying_intent is case.expect_buying_intent


def test_dataset_is_nonempty_and_covers_accept_and_reject():
    outcomes = {c.expect_accepted for c in EVALUATION_CASES}
    assert outcomes == {True, False}
    reasons = {c.expect_rejection for c in EVALUATION_CASES if not c.expect_accepted}
    # The dataset exercises a spread of rejection reasons, not just one.
    assert len(reasons) >= 4


def test_analysis_is_deterministic_across_repeats():
    for case in EVALUATION_CASES:
        first = analyze_signal(case.signal, EVAL_CONTEXT, seen_fingerprints=set()).as_dict()
        second = analyze_signal(case.signal, EVAL_CONTEXT, seen_fingerprints=set()).as_dict()
        assert first == second, f"{case.name} not deterministic"
