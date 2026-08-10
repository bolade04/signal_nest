"""Gate 4N-I28D — pins that actually discriminate.

Gate 4N-I28C found three tests that passed with AND without the mutation they existed to catch. A
test that cannot fail is not a pin, and all three were mine:

  I28C-01  the scan-domain pin wrote probe files into `tmp_path` and only ever exercised the
           BASENAME, so narrowing `is_scannable()` on a DIRECTORY component was invisible. With
           `scripts/` excluded, a hosted-zone literal AND an unapproved account id in
           `scripts/gen_operator_policies.py` both escaped, `leak_scan` exited 0, and the whole
           suite stayed green.
  I28C-02  the case-sensitivity probe was `Z` + 25 characters, which neither the case-sensitive
           production rule nor a case-insensitive variant matches.
  I28C-03  the trap-replacement test asserted what BASH does and never what the ANALYSER does, so
           deleting the replacement clause from the state machine changed nothing.

Every expectation below is stated here as literal data. None is read from a production constant,
because a table generated from the thing under test can only agree with it.

Identifiers are assembled at runtime: writing a real-shaped hosted-zone literal or an unapproved
account id into a tracked file is precisely what these controls refuse.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import failure_propagation as fp   # noqa: E402
import leak_scan                   # noqa: E402

FAILING = 'python3 -c "import sys; sys.exit(7)"'

# Assembled, never written whole. Each is a valid hosted-zone SHAPE: Z + 20 alphanumerics.
ZONE_UPPER = "Z" + "ABCDEFGHIJKLMNOPQRST"
ZONE_LOWER = "z" + "abcdefghijklmnopqrst"
ZONE_MIXED = "Z" + "aBcDeFgHiJkLmNoPqRsT"
# GATE 4N-I28I RC-4. These two used to be labelled "too short" / "too long" against a rule fitted
# to exactly 21 characters, which meant this file PINNED the narrow grammar as correct — a
# remediation of the pattern alone would have broken it. The band is now 13..32 total characters,
# justified by the public identifier shape and NOT by the protected value, so the boundary probes
# move to just outside that band.
ZONE_BELOW_BAND = "Z" + "ABCDEFGHIJ"               # 11 chars — below the supported candidate band
ZONE_ABOVE_BAND = "Z" + "A" * 32                   # 33 chars — above the supported candidate band
# A hyphen ends the token. With the Gate 4N-I28I band the SHORT prefix below is under 13
# characters, so no candidate survives; a longer prefix would legitimately BE a candidate,
# because the detector overmatches on purpose.
ZONE_BAD_ALPHABET = "Z" + "ABC-DEFGHIJKLMNOPQR"   # hyphen at position 4: prefix too short
SYNTHETIC_ZONE = "ZSYNTH" + "0" * 15
UNAPPROVED_ACCOUNT = "3141" + "59265358"
CREDENTIAL = "AKIA" + "ABCDEFGHIJKLMNOP"


# =====================================================================================
# I28C-01 — the scan domain, addressed by REPOSITORY-RELATIVE PATH, not by basename.
# =====================================================================================

# Stated here, not derived from leak_scan. Each entry is a commitment about the DOMAIN.
INCLUDED_PATHS = [
    ("scripts/example.py", "a script — the directory the I28C finding was about"),
    ("scripts/security/example.py", "a nested script directory"),
    ("apps/api/scripts/example.py", "a script nested under an application"),
    ("infra/example.tf", "infrastructure source"),
    ("scripts/EXTENSIONLESS", "an extensionless file under scripts/"),
    ("scripts/example.tfvars.example", "a compound suffix under scripts/"),
    ("brand/new/security/area/example.py", "a directory that does not exist yet"),
    # Deliberately NOT written as `tests/fixtures/...`: `commit_package_coherence` reads such a
    # string as a real fixture reference and refuses a commit whose fixture is missing. The
    # directory component under test is `tests`, which this still exercises. The guard is right,
    # and a test must not earn an exemption from it.
    ("tests/probes/example.json", "a JSON file under the tests tree"),
    ("docs/example.md", "documentation"),
]

EXCLUDED_PATHS = [
    ("vendor/pkg/example.js", "vendored third-party source"),
    ("node_modules/pkg/example.js", "installed dependencies"),
    (".terraform/modules/example.json", "provider-generated state directory"),
    ("__pycache__/example.pyc", "compiled bytecode"),
    (".pytest_cache/v/example.json", "test-runner cache"),
]


def _materialise(root: Path, relative: str, text: str = "placeholder\n") -> Path:
    """Create the file AT ITS REPOSITORY-RELATIVE PATH, so path.parts carries the directories.

    The I28C defect lived exactly here: a probe written to `tmp_path / "example.py"` has no
    `scripts` component, so an exclusion keyed on one could never be observed.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("relative,why", INCLUDED_PATHS)
def test_security_relevant_paths_are_scanned(relative, why, tmp_path):
    included, reason = leak_scan.is_scannable(_materialise(tmp_path, relative))
    assert included, f"{why} ({relative}) would be skipped: {reason}"


@pytest.mark.parametrize("relative,why", EXCLUDED_PATHS)
def test_justified_exclusions_still_apply(relative, why, tmp_path):
    """The inversion must not become 'scan everything', or the exclusions stop being stated."""
    text = "placeholder\n"
    path = _materialise(tmp_path, relative, text)
    if relative.endswith(".pyc"):
        path.write_bytes(b"\x00\x01binary")
    included, _ = leak_scan.is_scannable(path)
    assert not included, f"{why} ({relative}) should be excluded"


# The expected membership, stated. Adding `scripts`, `infra` or `apps` here — or dropping
# `vendor` — must fail this test rather than silently changing the scan domain.
EXPECTED_EXCLUDED_PATH_PARTS = {
    "node_modules", "vendor", ".terraform", "__pycache__", ".pytest_cache", ".mypy_cache",
}

SECURITY_RELEVANT_DIRECTORIES = {"scripts", "tests", "infra", "apps", "docs", ".github"}


def test_the_exclusion_set_is_exactly_what_it_is_supposed_to_be():
    """I28C-01: nothing pinned this membership, so `scripts` could simply be added to it."""
    assert set(leak_scan.EXCLUDED_PATH_PARTS) == EXPECTED_EXCLUDED_PATH_PARTS, (
        "the scan-domain exclusion set changed; every entry must be an independently justified "
        "vendored, generated or cache path, never a directory of tracked source")


def test_no_security_relevant_directory_is_excluded():
    """Stated as a property as well as a set, so a rename cannot smuggle one in."""
    overlap = SECURITY_RELEVANT_DIRECTORIES & set(leak_scan.EXCLUDED_PATH_PARTS)
    assert not overlap, f"tracked source directories are being skipped: {sorted(overlap)}"


# =====================================================================================
# Multi-token consequence: what the domain is FOR.
# =====================================================================================

PROTECTED_TOKENS = [
    ("hosted-zone identifier", f'ZONE = "{ZONE_UPPER}"'),
    ("unapproved 12-digit account id", f'ACCOUNT = "{UNAPPROVED_ACCOUNT}"'),
    ("credential material", f'KEY = "{CREDENTIAL}"'),
]


@pytest.mark.parametrize("relative,_why", [(r, w) for r, w in INCLUDED_PATHS])
@pytest.mark.parametrize("token,payload", PROTECTED_TOKENS)
def test_no_protected_token_can_hide_in_an_included_path(relative, _why, token, payload, tmp_path):
    """A path exclusion must not be able to hide ANY protected class, not just the newest one.

    With `scripts/` excluded, Gate 4N-I28C put both a hosted-zone literal and an unapproved
    account id into `scripts/gen_operator_policies.py` and `leak_scan` exited 0.
    """
    path = _materialise(tmp_path, relative, payload + "\n")
    included, reason = leak_scan.is_scannable(path)
    assert included, f"{relative} is outside the scan domain ({reason}), so {token} cannot be seen"
    assert leak_scan.scan_text(path.read_text(encoding="utf-8")), (
        f"{token} was not reported in {relative}")


# =====================================================================================
# I28C-02 — a case oracle that actually distinguishes the two rules.
# =====================================================================================

# Reference patterns written HERE. The production rule is compared against these; importing
# `leak_scan._HOSTED_ZONE` would make the comparison circular.
# GATE 4N-I28I RC-4: the reference band is written here independently, matching the PUBLIC
# identifier shape (uppercase Z + 12..31 uppercase alphanumerics). It is stated, not imported.
REFERENCE_SENSITIVE = re.compile(r"(?<![A-Za-z0-9])Z[A-Z0-9]{12,31}(?![A-Za-z0-9])")
REFERENCE_INSENSITIVE = re.compile(r"(?<![A-Za-z0-9])Z[A-Za-z0-9]{12,31}(?![A-Za-z0-9])")

# (probe, matches under the case-SENSITIVE policy, matches under a case-INSENSITIVE policy)
CASE_PROBES = [
    (ZONE_UPPER, True, True, "valid uppercase — the real grammar"),
    (ZONE_MIXED, False, True, "mixed case — THE discriminating case"),
    ("Z" + "2FDTNDATAQYW2", True, True, "a real-shaped 14-character id, inside the band"),
    (ZONE_LOWER, False, False, "lowercase — the leading Z is wrong too"),
    (ZONE_BELOW_BAND, False, False, "below the supported candidate band"),
    (ZONE_ABOVE_BAND, False, False, "above the supported candidate band"),
    (ZONE_BAD_ALPHABET, False, False, "hyphen is outside the alphabet"),
]


@pytest.mark.parametrize("probe,sensitive,insensitive,why", CASE_PROBES)
def test_the_reference_patterns_disagree_where_they_should(probe, sensitive, insensitive, why):
    """AXIS ONE: the probes are valid for the grammar and separate the two policies.

    I28C-02: the previous probe was Z + 25 characters, so BOTH policies rejected it and the test
    could not tell them apart.
    """
    assert bool(REFERENCE_SENSITIVE.search(probe)) is sensitive, why
    assert bool(REFERENCE_INSENSITIVE.search(probe)) is insensitive, why


@pytest.mark.parametrize("probe,sensitive,_insensitive,why", CASE_PROBES)
def test_production_detection_follows_the_case_sensitive_policy(probe, sensitive, _insensitive, why):
    """AXIS TWO: the shipped scanner, reached through its real entry point."""
    findings = leak_scan.scan_text(f'zone = "{probe}"')
    assert bool(findings) is sensitive, f"{why}: {probe} -> {findings}"


def test_at_least_one_probe_would_change_the_verdict_under_a_case_insensitive_rule():
    """The guard that makes this file a pin: if no probe discriminates, nothing is being tested."""
    discriminating = [p for p, s, i, _ in CASE_PROBES if s != i]
    assert discriminating, "no probe distinguishes the case-sensitive rule from an insensitive one"


def test_the_boundary_is_enforced_on_both_sides():
    """A candidate embedded in a run that leaves the supported band is not a candidate.

    GATE 4N-I28I RC-4. The band is 13..32 characters and the detector deliberately overmatches
    inside it, so appending a few characters still yields a candidate — correctly, since that IS
    a hosted-zone-shaped token. What the boundaries must still prevent is matching a fragment of
    a run that is far outside the band, which is where base64 lives.
    """
    assert leak_scan.scan_text(f'x = "{ZONE_UPPER}"')
    long_run = "Z" + "A" * 60
    assert leak_scan.scan_text(f'x = "{long_run}"') == [], (
        "a 61-character run is outside the supported band and must not be reported")
    assert leak_scan.scan_text(f'x = "PREFIX{long_run}"') == []


def test_synthetic_and_placeholder_remain_accepted():
    assert leak_scan.scan_text(f'{{"hosted_zone_id": "{SYNTHETIC_ZONE}"}}') == []
    assert leak_scan.scan_text('zone_id = "${var.hosted_zone_id}"') == []


# =====================================================================================
# I28C-03 — trap replacement, asserted against the ANALYSER's state machine.
# =====================================================================================

def bash_ends_successfully(body: str, tmp_path) -> bool:
    script = tmp_path / "probe.sh"
    script.write_text("T='exit 0'\n" + body.rstrip("\n") + "\necho AFTER\n", encoding="utf-8")
    return subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", str(script)],
                          capture_output=True, text=True).returncode == 0


def analyse_step(body: str, tmp_path) -> dict:
    """Run the real analyser over a synthetic graded step and hand back its verdicts."""
    workflow = tmp_path / "wf.yml"
    workflow.write_text(
        "name: probe\non: [push]\n"
        "defaults:\n  run:\n    shell: bash --noprofile --norc -euo pipefail {0}\n"
        "jobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: probe\n        id: probe\n        run: |\n"
        + textwrap.indent(body.rstrip("\n"), " " * 10) + "\n"
        '      - name: agg\n        run: echo "probe=${{ steps.probe.outcome }}"\n',
        encoding="utf-8")
    original = fp.WORKFLOW
    try:
        fp.WORKFLOW = workflow
        result = fp.analyse()
    finally:
        fp.WORKFLOW = original
    return next(s for s in result["steps"] if s["id"] == "probe")


# (id, sequence, expected FINAL effective state, does bash end successfully?)
# "final effective state" is what governs the guard that follows: ABSORBING masks it,
# NOT_ABSORBING lets it through, UNPROVABLE refuses to say.
TRAP_SEQUENCES = [
    ("absorbing replaced by non-absorbing", "trap 'exit 0' ERR\ntrap 'exit 1' ERR",
     "NOT_ABSORBING", False),
    ("non-absorbing replaced by absorbing", "trap 'exit 1' ERR\ntrap 'exit 0' ERR",
     "ABSORBING", True),
    ("absorbing then removed", "trap 'exit 0' ERR\ntrap - ERR", "NOT_ABSORBING", False),
    ("absorbing, subshell replacement attempt", "trap 'exit 0' ERR\n( trap 'exit 1' ERR )",
     "ABSORBING", True),
    ("absorbing replaced dynamically", "trap 'exit 0' ERR\ntrap \"$T\" ERR",
     "UNPROVABLE", True),
    ("no trap at all", "echo hello", "NOT_ABSORBING", False),
]


@pytest.mark.parametrize("label,sequence,expected,bash_succeeds", TRAP_SEQUENCES)
def test_bash_confirms_the_replacement_sequence(label, sequence, expected, bash_succeeds, tmp_path):
    """AXIS ONE: expectation versus a real shell."""
    assert bash_ends_successfully(f"{sequence}\n{FAILING}", tmp_path) is bash_succeeds, label


@pytest.mark.parametrize("label,sequence,expected,_bash", TRAP_SEQUENCES)
def test_the_analyser_state_machine_tracks_the_replacement(label, sequence, expected, _bash,
                                                           tmp_path):
    """AXIS TWO — THE I28C-03 DEFECT. Asserting only bash left the analyser untested, so deleting
    the clause that clears `trap_absorbing` on a non-absorbing trap changed nothing."""
    step = analyse_step(f"{sequence}\n{FAILING}", tmp_path)
    guard = step["lines"][-1]
    if expected == "ABSORBING":
        assert guard["verdict"] == fp.MASKED, (
            f"{label}: the guard after an absorbing trap must be reported masked, got "
            f"{guard['verdict']}")
    elif expected == "NOT_ABSORBING":
        assert guard["verdict"] == fp.PROPAGATES, (
            f"{label}: the replacement/removal must clear the absorbing state, so the guard "
            f"propagates; got {guard['verdict']}")
    else:
        assert guard["verdict"] == fp.UNKNOWN, (
            f"{label}: an unprovable replacement must fail closed, got {guard['verdict']}")


def test_a_dynamic_replacement_fails_closed(tmp_path):
    step = analyse_step(f'trap \'exit 0\' ERR\ntrap "$T" ERR\n{FAILING}', tmp_path)
    assert step["unknown"], "a dynamic trap replacement must produce a finding"


# =====================================================================================
# Guards on THIS file. Every pin above can be defeated by editing the oracle rather than the
# implementation — rewriting an expectation table as a read of the production constant, or
# deleting an axis outright. Gate 4N-I28D's own falsification proved all three go unnoticed
# without these. They cannot defend against deleting themselves; what they close is the quieter
# failure, where the table still looks like a table and agrees with whatever ships.
# =====================================================================================

FORBIDDEN_PRODUCTION_CONSTANTS = {
    "EXCLUDED_PATH_PARTS", "SCAN_SUFFIXES", "BINARY_SUFFIXES", "ALLOWED_ACCOUNTS",
    "_HOSTED_ZONE", "_HOSTED_ZONE_IN_ARN", "_SYNTHETIC_ZONE_PREFIX", "_ACCOUNT", "_CREDENTIAL",
    "_TRAP_EXPLICIT_SUCCESS", "_TRAP_EXPLICIT_FAILURE", "_TRAP_STATUS_PRESERVING",
    "_TRAP_SIGNALS", "_TRAP_PREFIXES", "_TRAP_WORD", "_COMPOUND_KEYWORDS",
}

REQUIRED_AXES = (
    "test_the_reference_patterns_disagree_where_they_should",      # expectation vs grammar
    "test_production_detection_follows_the_case_sensitive_policy",  # analyser vs expectation
    "test_bash_confirms_the_replacement_sequence",                 # expectation vs bash
    "test_the_analyser_state_machine_tracks_the_replacement",      # analyser vs expectation
    "test_the_exclusion_set_is_exactly_what_it_is_supposed_to_be",
    "test_no_protected_token_can_hide_in_an_included_path",
)


def _this_module_ast():
    import ast
    return ast.parse(Path(__file__).read_text(encoding="utf-8"))


EXPECTATION_TABLES = {
    "INCLUDED_PATHS", "EXCLUDED_PATHS", "CASE_PROBES", "TRAP_SEQUENCES",
    "EXPECTED_EXCLUDED_PATH_PARTS", "SECURITY_RELEVANT_DIRECTORIES",
    "REFERENCE_SENSITIVE", "REFERENCE_INSENSITIVE", "PROTECTED_TOKENS",
}


def test_no_expectation_is_read_from_a_production_constant():
    """THE F09/F15 DEFECT: `EXPECTED_… = set(leak_scan.EXCLUDED_PATH_PARTS)` and
    `REFERENCE_SENSITIVE = leak_scan._HOSTED_ZONE` both make the check circular.

    Scoped to the DEFINITIONS. Reading a production constant inside an assertion is the whole
    point — `set(leak_scan.EXCLUDED_PATH_PARTS) == EXPECTED_EXCLUDED_PATH_PARTS` is the
    comparison. What must never happen is the expectation being *derived* from it.
    """
    import ast
    leaked = {}
    for node in _this_module_ast().body:
        if not isinstance(node, ast.Assign):
            continue
        name = getattr(node.targets[0], "id", "")
        if name not in EXPECTATION_TABLES:
            continue
        found = {n.attr for n in ast.walk(node.value)
                 if isinstance(n, ast.Attribute)} & FORBIDDEN_PRODUCTION_CONSTANTS
        if found:
            leaked[name] = sorted(found)
    assert not leaked, (
        f"these expectations are read from production constants: {leaked}; an expectation taken "
        "from the thing under test can only ever agree with it")


@pytest.mark.parametrize("name", REQUIRED_AXES)
def test_every_required_axis_is_still_present(name):
    """THE F14 DEFECT: renaming or deleting an axis silently removes half the comparison."""
    import ast
    defined = {n.name for n in ast.walk(_this_module_ast())
               if isinstance(n, ast.FunctionDef)}
    assert name in defined, f"the {name} axis is gone; the remaining half pins nothing on its own"


def test_the_expectation_tables_are_literal_data():
    """A table built by calling into the implementation is not an independent expectation."""
    import ast
    seen = set()
    for node in _this_module_ast().body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in EXPECTATION_TABLES:
            seen.add(node.targets[0].id)
    assert seen == EXPECTATION_TABLES, (
        f"expectation tables missing: {sorted(EXPECTATION_TABLES - seen)}")
