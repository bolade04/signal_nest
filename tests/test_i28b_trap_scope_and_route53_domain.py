"""Gate 4N-I28B — trap scope, Route53 audit domain, and reviewer retrieval states.

Gate 4N-I28A rejected the package on two findings, both of which I introduced or overstated at
Gate 4N-I27Z:

  I28A-01  `trap_effect()` called four non-absorbing forms ABSORBS, and the I27Z test that was
           supposed to catch that asserted the wrong answers against the module itself.
  I28A-02  Route53 reintroduction was detected only inside a five-entry `POLICY_GENERATORS`
           list, so a literal in `scripts/gen_readonly_verifier_policy.py` was caught by nothing.

Widening the trap matrix during I28B then turned up a case I28A had missed outright, and it was
FAIL-OPEN rather than conservative: `{ trap 'exit 0' ERR; }`. A brace group runs in the current
shell, so bash exits 0 — while the module reported that no trap existed at all.

The identifier used below is CONSTRUCTED at runtime and was never a live hosted-zone id. Writing
a real-shaped literal into a tracked file is the very thing `leak_scan` now refuses.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import failure_propagation as fp            # noqa: E402
import leak_scan                            # noqa: E402
import reviewer_retrieval_state as rrs      # noqa: E402

FAILING = 'python3 -c "import sys; sys.exit(7)"'
# Never a live identifier, and never written whole into this file.
UNEXPLAINED_ZONE = "Z" + "ABCDEFGHIJKLMNOPQRST"
SYNTHETIC_ZONE = "ZSYNTH" + "0" * 15


def bash_ends_successfully(body: str, tmp_path) -> bool:
    script = tmp_path / "probe.sh"
    script.write_text(body.rstrip("\n") + "\necho AFTER\n", encoding="utf-8")
    return subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", str(script)],
                          capture_output=True, text=True).returncode == 0


# =====================================================================================
# I28A-01 — trap scope and absorption.
# =====================================================================================

def test_a_brace_group_trap_binds_the_parent_shell(tmp_path):
    """THE FAIL-OPEN Gate 4N-I28A missed. `{ …; }` is not a new shell."""
    assert bash_ends_successfully(f"{{ trap 'exit 0' ERR; }}\n{FAILING}", tmp_path) is True
    assert fp.trap_effect("{ trap 'exit 0' ERR; }") == fp.TRAP_ABSORBS


def test_a_subshell_trap_does_not_bind_the_parent_shell(tmp_path):
    assert bash_ends_successfully(f"( trap 'exit 0' ERR )\n{FAILING}", tmp_path) is False
    assert fp.trap_effect("( trap 'exit 0' ERR )") == fp.TRAP_NONABSORBING


@pytest.mark.parametrize("body,absorbs", [
    ("exit 0", True), ("exit 00", True),
    ("exit 1", False), ("exit 7", False), ("exit", False), (":", False), ("true", False),
])
def test_only_an_explicit_zero_exit_absorbs(body, absorbs, tmp_path):
    """Running successfully and TERMINATING the shell successfully are different events."""
    line = f"trap '{body}' ERR"
    assert bash_ends_successfully(f"{line}\n{FAILING}", tmp_path) is absorbs
    assert (fp.trap_effect(line) == fp.TRAP_ABSORBS) is absorbs


def test_installing_a_trap_replaces_the_previous_one(tmp_path):
    """Bash: an absorbing trap followed by a failing one exits 1, so state must not stick.

    GATE 4N-I28D, FINDING I28C-03. This is the BASH axis only, and on its own it pinned nothing
    about the module — deleting the analyser's replacement clause left it green. The analyser-state
    axis is now asserted in
    tests/test_i28d_scan_domain_and_state_pins.py::test_the_analyser_state_machine_tracks_the_replacement.
    """
    assert bash_ends_successfully(
        f"trap 'exit 0' ERR\ntrap 'exit 1' ERR\n{FAILING}", tmp_path) is False


def test_a_signal_list_stops_at_the_next_command():
    """`trap … ERR; echo hi` — `echo` and `hi` are not signals."""
    assert fp.trap_effect("trap 'exit 0' ERR; echo hi") == fp.TRAP_ABSORBS
    assert fp.trap_effect("trap 'exit 0' USR1; echo ERR") == fp.TRAP_NONE


def test_a_parenthesis_inside_quotes_opens_no_scope():
    assert fp.trap_scope('echo "( trap x ERR )"') == fp.TRAP_SCOPE_PARENT


# =====================================================================================
# I28A-02 — the Route53 audit domain.
# =====================================================================================

def test_an_unexplained_hosted_zone_identifier_is_a_finding():
    assert leak_scan.scan_text(f'ZONE = "{UNEXPLAINED_ZONE}"')


def test_an_explicitly_synthetic_identifier_is_accepted():
    assert leak_scan.scan_text(f'{{"hosted_zone_id": "{SYNTHETIC_ZONE}"}}') == []


def test_a_protected_input_placeholder_is_accepted():
    assert leak_scan.scan_text('zone_id = "${var.hosted_zone_id}"') == []
    assert leak_scan.scan_text('hosted_zone_id = "<supplied via protected inventory>"') == []


def test_the_arn_form_is_caught_at_other_lengths():
    """A shorter real id would never match the bare Z+20 shape.

    Assembled at runtime: writing the ARN whole would put a provenance-free identifier into a
    tracked file, which is exactly what this rule refuses. The test must not violate the
    property it tests.
    """
    arn = "arn:aws:route53:::hostedzone/" + "ZPROD" + "ZONE12345"
    assert leak_scan.scan_text(f'"{arn}"')


def test_a_base64_fragment_is_not_a_hosted_zone_identifier():
    """Base64 text must not be read as an identifier — the Gate 4N-I27R lesson, when an `re.I`
    key-id rule read certificate material as a credential.

    GATE 4N-I28D, FINDING I28C-02. This test used to be named
    `test_the_rule_is_case_sensitive` and claimed to pin case sensitivity. It did not: the sample
    is `Z` followed by TWENTY-FIVE characters, so neither the case-sensitive production rule nor a
    case-insensitive variant matches it, and the test passed under both. It is kept — a base64
    fragment genuinely should not be flagged — but renamed to what it actually checks. The
    discriminating case oracle lives in
    tests/test_i28d_scan_domain_and_state_pins.py::CASE_PROBES, where a true `Z` + 20 mixed-case
    sample separates the two policies.
    """
    assert leak_scan.scan_text("token = 'Zm9vYmFyYmF6cXV1eGNvcmdlZA'") == []


# A content rule is only as wide as the inclusion rule beneath it. Gate 4N-I28B's own
# falsification proved the point: with `scan_text` correct, three separate narrowings of
# `is_scannable` — skip one named file, skip extensionless files, restore the suffix allow-list —
# each collapsed coverage and NOTHING noticed, because the first version of this test read
# EXCLUDED_PATH_PARTS and BINARY_SUFFIXES rather than calling the function that actually decides.
# That is the I28A-02 defect one layer down. These cases call `is_scannable` itself.
INCLUSION_CASES = [
    ("scripts/gen_readonly_verifier_policy.py", "the generator the I28A finding was about"),
    ("scripts/gen_brand_new_policy.py", "a generator nobody has listed anywhere"),
    ("scripts/unrelated_helper.py", "an unrelated security script"),
    ("ZONENOTES", "an extensionless tracked text file"),
    ("infra/aws/zone.tfvars.example", "a compound suffix"),
    ("docs/zone-runbook.md", "documentation"),
    # Named WITHOUT a `tests/fixtures/` prefix on purpose: `commit_package_coherence` reads such
    # a string as a real fixture reference and refuses a commit whose fixture is missing. Only
    # the basename is used below, so the property is unchanged and the guard keeps its teeth —
    # the guard is right, and a test must not earn an exemption from it.
    ("zone-unmarked.json", "a fixture-shaped JSON file"),
]


@pytest.mark.parametrize("name,why", INCLUSION_CASES)
def test_the_scan_includes_every_tracked_text_shape(name, why, tmp_path):
    """THE I28A-02 DEFECT, as a property: inclusion must be universal, not enumerated."""
    path = tmp_path / Path(name).name
    path.write_text("zone notes\n", encoding="utf-8")
    included, reason = leak_scan.is_scannable(path)
    assert included, f"{why} ({name}) would be skipped: {reason}"


def test_the_suffix_list_is_not_an_inclusion_gate(tmp_path):
    """Gate 4N-I27R inverted this once already — recognising good suffixes is what let a
    `.tfvars.example` file through. It must not come back as a gate."""
    path = tmp_path / "Dockerfile.probe"
    path.write_text("zone notes\n", encoding="utf-8")
    assert path.suffix not in leak_scan.SCAN_SUFFIXES
    assert leak_scan.is_scannable(path)[0], (
        "an unlisted suffix is being excluded again; inclusion must be by exclusion only")


def test_binaries_and_vendored_paths_are_still_excluded(tmp_path):
    """The inversion must not become 'scan everything', or the exclusions stop being stated."""
    binary = tmp_path / "zone.png"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert leak_scan.is_scannable(binary)[0] is False
    vendored = tmp_path / "node_modules" / "pkg" / "index.js"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("zone\n", encoding="utf-8")
    assert leak_scan.is_scannable(vendored)[0] is False


def test_the_existing_account_and_credential_rules_still_work():
    """The new rule must not have displaced what the scan already caught."""
    unapproved = "3141" + "59265358"          # assembled, for the same reason as the ARN above
    assert leak_scan.scan_text(f"account: {unapproved}")
    assert leak_scan.scan_text("AKIA" + "ABCDEFGHIJKLMNOP")
    assert leak_scan.scan_text("account: 999988887777") == []    # an approved one stays clean


# =====================================================================================
# I28A-04 — reviewer retrieval states.
# =====================================================================================

def test_the_contract_defines_all_five_states():
    assert set(rrs.STATES) == {"RUNNING", "COMPLETED_WITH_ARTIFACT", "COMPLETED_NO_ARTIFACT",
                               "TRANSPORT_UNDELIVERED", "FAILED"}


def test_an_artifact_outranks_a_failed_process():
    """A lane that died AFTER writing a complete verdict has still delivered one."""
    assert rrs.classify(process_alive=False, process_exit=1, artifact_valid=True,
                        transport_delivered=False) == rrs.COMPLETED_WITH_ARTIFACT


def test_a_dead_process_with_no_artifact_is_failed_not_completed():
    """THE I28A-04 DEFECT: this used to collapse into COMPLETED_NO_ARTIFACT, which reads as a
    reviewer decision rather than an infrastructure fault."""
    assert rrs.classify(process_alive=False, process_exit=1, artifact_valid=False,
                        transport_delivered=False) == rrs.FAILED


def test_transport_failure_is_not_execution_failure():
    assert rrs.classify(process_alive=False, process_exit=0, artifact_valid=False,
                        transport_delivered=False) == rrs.TRANSPORT_UNDELIVERED
    assert rrs.classify(process_alive=False, process_exit=0, artifact_valid=False,
                        transport_delivered=True) == rrs.COMPLETED_NO_ARTIFACT


def test_only_one_state_may_be_called_a_lost_verdict():
    lost = [s for s in rrs.STATES if rrs.is_lost_verdict(s)]
    assert lost == [rrs.COMPLETED_NO_ARTIFACT]


def test_a_completed_or_running_lane_is_never_relaunched():
    assert rrs.may_relaunch(rrs.COMPLETED_WITH_ARTIFACT) is False
    assert rrs.may_relaunch(rrs.RUNNING) is False
    assert rrs.may_relaunch(rrs.FAILED) is True


def test_a_lane_with_no_exit_status_fails_closed():
    with pytest.raises(rrs.RetrievalStateError):
        rrs.classify(process_alive=False, process_exit=None, artifact_valid=False,
                     transport_delivered=True)


def test_a_late_artifact_can_supersede_every_terminal_state():
    for state in (rrs.COMPLETED_NO_ARTIFACT, rrs.TRANSPORT_UNDELIVERED, rrs.FAILED):
        assert rrs.COMPLETED_WITH_ARTIFACT in rrs.TRANSITIONS[state]
