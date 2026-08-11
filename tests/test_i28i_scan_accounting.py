"""Gate 4N-I28I, root cause RC-2 — no discovered path leaves the scan without a reason.

Gate 4N-I28G findings ADV-01 and SEC-02. `candidate_files()` used to `continue` on `SKIP_DIRS`
before `is_scannable()` ran, so an outer-filtered file left no trace. The reviewer's reproduction:

    baseline                         681 files scanned, planted identifier detected
    SKIP_DIRS |= {"scripts"}         601 files, identifier gone, **0** skip-report entries
    EXCLUDED_PATH_PARTS |= {"..."}   601 files, identifier gone,   80  skip-report entries

The two filters differed in VISIBILITY, not only coverage — and the invisible one was the one no
test pinned. That defeated this module's own Gate 4N-I27R promise: "skipped as binary and REPORTED,
never silently."

The property below is an accounting identity, not a list of names:

    discovered == scanned + categorized skips + explicit errors

A file can leave the scan only by being counted somewhere else. Narrow any filter and the identity
still holds — but the skip becomes visible, which is the point.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import leak_scan as ls  # noqa: E402

ZONE = "Z" + "ABCDEFGHIJKLMNOPQRST"
ACCOUNT = "3141" + "59265358"

# Stated here, not imported. If the module's decision set changes, this must be updated
# deliberately rather than tracking it silently.
EXPECTED_DECISIONS = {
    "SCANNED", "SKIPPED_BINARY", "SKIPPED_VENDOR", "SKIPPED_GENERATED",
    "SKIPPED_CACHE", "SKIPPED_EXPLICIT_POLICY", "ERROR_OR_UNKNOWN",
}


def test_the_decision_set_is_closed_and_is_what_it_is_supposed_to_be():
    assert set(ls.DECISIONS) == EXPECTED_DECISIONS


def test_the_accounting_identity_holds():
    """THE RC-2 INVARIANT."""
    a = ls.scan_accounting()
    assert a["reconciles"], a
    assert a["discovered_candidate_paths"] == (
        a["scanned_paths"] + a["categorized_skipped_paths"] + a["explicit_error_paths"])
    assert a["accounted"] == a["discovered_candidate_paths"], (
        "the reported `accounted` total must equal the identity it claims to summarise; a field "
        "nothing asserts can drift away from the computation behind it")
    assert a["duplicates"] == 0
    assert a["omissions"] == 0
    assert a["unexplained"] == []
    assert a["every_skip_has_a_reason"]


def test_every_discovered_file_has_a_recorded_decision():
    a = ls.scan_accounting()
    assert a["recorded_decisions"] == a["discovered_candidate_paths"]


def test_the_yielded_set_matches_the_scanned_count():
    """If these drifted apart, files would be counted as scanned without being scanned."""
    a = ls.scan_accounting()
    assert a["yielded_for_scanning"] == a["scanned_paths"]


# =====================================================================================
# The outer filter can no longer skip silently.
# =====================================================================================

def _decide(rel: str, tmp_path: Path, text: str = "placeholder\n"):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ls.scan_decision(path, tmp_path)


@pytest.mark.parametrize("rel,expected", [
    ("node_modules/pkg/a.js", "SKIPPED_VENDOR"),
    ("vendor/pkg/a.js", "SKIPPED_VENDOR"),
    (".venv/lib/a.py", "SKIPPED_VENDOR"),
    (".terraform/modules/a.json", "SKIPPED_GENERATED"),
    ("dist/a.js", "SKIPPED_GENERATED"),
    ("__pycache__/a.pyc", "SKIPPED_CACHE"),
    (".pytest_cache/v/a.json", "SKIPPED_CACHE"),
    (".git/config", "SKIPPED_EXPLICIT_POLICY"),
])
def test_an_excluded_directory_yields_a_categorized_decision_not_silence(rel, expected, tmp_path):
    """THE ADV-01 DEFECT: these used to vanish with no record at all."""
    decision, reason = _decide(rel, tmp_path)
    assert decision == expected, f"{rel} -> {decision}"
    assert reason, "a skip with no reason is a silent skip"


@pytest.mark.parametrize("rel", [
    "scripts/example.py", "scripts/security/example.py", "apps/api/scripts/example.py",
    "infra/example.tf", "scripts/EXTENSIONLESS", "scripts/example.tfvars.example",
    "brand/new/area/example.py", "docs/example.md",
])
def test_security_relevant_paths_are_decided_SCANNED(rel, tmp_path):
    decision, _reason = _decide(rel, tmp_path)
    assert decision == "SCANNED", f"{rel} -> {decision}"


def test_an_uncategorized_exclusion_fails_closed(tmp_path, monkeypatch):
    """A directory excluded but not categorized must be ERROR_OR_UNKNOWN, never a quiet skip.

    This is the guard against 'add a name to the list and move on': an exclusion this module
    cannot explain is not a safe exclusion.
    """
    monkeypatch.setattr(ls, "SKIP_DIRS", set(ls.SKIP_DIRS) | {"mystery_dir"})
    decision, reason = _decide("mystery_dir/a.py", tmp_path)
    assert decision == "ERROR_OR_UNKNOWN", decision
    assert "no category" in reason


@pytest.mark.parametrize("token,payload", [
    ("hosted-zone identifier", f'ZONE = "{ZONE}"'),
    ("unapproved account id", f'ACCOUNT = "{ACCOUNT}"'),
    ("credential material", 'KEY = "AKIA' + 'ABCDEFGHIJKLMNOP"'),
])
def test_a_protected_token_in_a_scanned_path_is_still_reported(token, payload, tmp_path):
    decision, _ = _decide("scripts/probe.py", tmp_path, payload + "\n")
    assert decision == "SCANNED", f"{token} would not even be examined"
    assert ls.scan_text(payload), f"{token} was not reported"


def test_narrowing_the_outer_filter_is_visible_in_the_accounting(tmp_path, monkeypatch):
    """THE DISCRIMINATING CASE. Before RC-2 this produced zero skip entries."""
    for rel in ("scripts/a.py", "scripts/b.py", "docs/c.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(ls, "SKIP_DIRS", set(ls.SKIP_DIRS) | {"scripts"})
    monkeypatch.setitem(ls._DIRECTORY_CATEGORY, "scripts", ls.SKIPPED_EXPLICIT_POLICY)
    list(ls.candidate_files(tmp_path))
    skipped = [r for r, (d, _x) in ls.SCAN_DECISIONS.items() if d != "SCANNED"]
    assert len(skipped) >= 2, (
        "narrowing the outer filter removed files from the scan without recording a skip — "
        "the exact silence Gate 4N-I28G found")
