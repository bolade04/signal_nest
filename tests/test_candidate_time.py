"""Timezone-aware candidate time arithmetic (Gate 4N-I11, Defect 18).

THE DEFECT. `minutes_remaining_at_freeze: 169` was recorded against a real delta of 229.6
minutes — a ~60-minute error from mixing `time.mktime` (which reads a struct as LOCAL time)
with a UTC `gmtime` struct. The direction was conservative, so no reviewer ran on a shorter
runway than believed, and no policy byte was affected. It was still wrong, in the one number
that describes the whole temporary-permission model.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import candidate_time as ct  # noqa: E402
import iam_eval  # noqa: E402

UTC = datetime.timezone.utc


def test_the_exact_gate_4n_i10_discrepancy_is_now_computed_correctly():
    """The real numbers from the defective candidate."""
    at = datetime.datetime(2026, 7, 31, 17, 55, 46, tzinfo=UTC)
    result = ct.remaining("2026-07-31T21:45:20Z", at=at)
    assert result["minutes_remaining"] == 229, result
    assert result["seconds_remaining"] == 13774, result
    assert result["minutes_remaining"] != 169, "the local/UTC mixing bug is back"


@pytest.mark.parametrize("tzname,offset", [
    ("UTC", 0), ("US/Eastern-like", -5), ("US/Pacific-like", -8),
    ("CET-like", 1), ("IST-like", 5.5), ("NZDT-like", 13),
])
def test_the_same_instant_gives_the_same_answer_from_any_timezone(tzname, offset):
    """An offset-carrying timestamp is the same INSTANT; the window must not move."""
    base = datetime.datetime(2026, 7, 31, 17, 55, 46, tzinfo=UTC)
    shifted = base.astimezone(datetime.timezone(datetime.timedelta(hours=offset)))
    assert ct.remaining("2026-07-31T21:45:20Z", at=shifted)["seconds_remaining"] == \
        ct.remaining("2026-07-31T21:45:20Z", at=base)["seconds_remaining"], tzname


def test_an_offset_qualified_expiry_is_the_same_instant():
    at = datetime.datetime(2026, 7, 31, 17, 55, 46, tzinfo=UTC)
    utc_form = ct.remaining("2026-07-31T21:45:20Z", at=at)
    offset_form = ct.remaining("2026-07-31T17:45:20-04:00", at=at)
    assert utc_form["seconds_remaining"] == offset_form["seconds_remaining"]


def test_a_naive_moment_is_refused():
    with pytest.raises(ValueError):
        ct.remaining("2026-07-31T21:45:20Z", at=datetime.datetime(2026, 7, 31, 17, 55, 46))


def test_a_naive_expiry_is_refused():
    with pytest.raises(iam_eval.UnsupportedPolicyFeature):
        ct.remaining("2026-07-31T21:45:20", at=ct.now_utc())


def test_a_placeholder_expiry_is_refused():
    with pytest.raises(iam_eval.UnsupportedPolicyFeature):
        ct.remaining("<EXPIRY-ISO8601>", at=ct.now_utc())


def test_across_a_daylight_saving_transition_the_window_is_unchanged():
    """DST is where local-time arithmetic silently gains or loses an hour."""
    before = datetime.datetime(2026, 3, 8, 6, 0, 0, tzinfo=UTC)   # US DST start
    after = datetime.datetime(2026, 3, 8, 8, 0, 0, tzinfo=UTC)
    expiry = "2026-03-08T12:00:00Z"
    assert ct.remaining(expiry, at=before)["seconds_remaining"] == 6 * 3600
    assert ct.remaining(expiry, at=after)["seconds_remaining"] == 4 * 3600


def test_an_expired_window_is_reported_expired():
    at = datetime.datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    result = ct.remaining("2026-07-31T21:45:20Z", at=at)
    assert result["expired"] is True
    assert result["seconds_remaining"] < 0


def test_now_utc_is_timezone_aware():
    assert ct.now_utc().tzinfo is not None
    assert ct.now_utc().utcoffset().total_seconds() == 0


def test_the_window_uses_the_same_parser_as_the_policy_expiry_tests():
    """Metadata and enforcement must be computed from one instant, not two.

    The forbidden-symbol scan runs over CODE only. The module docstring names the offending
    function in order to explain the defect, and an explanation of a rule must not break it —
    a lesson this gate chain has now learned three times in three different scanners.
    """
    import ast

    path = REPO_ROOT / "scripts" / "candidate_time.py"
    source = path.read_text(encoding="utf-8")
    assert "iam_eval.parse_iam_date" in source

    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    assert "mktime" not in names, "the local-time conversion that caused the defect is back"
    assert "utcnow" not in names, "utcnow() returns a NAIVE datetime"
