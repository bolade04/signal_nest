#!/usr/bin/env python3
"""GATE 4N-I27R — the six-reviewer rejection findings, pinned so they cannot return.

Gate 4N-I27Q froze a candidate and three of six independent reviewers rejected it. Every defect
below was REPRODUCED before it was fixed, and each test here replays the exact form that
defeated the rejected candidate.

THE CORPUS IS DERIVED FROM SEMANTICS, NOT FROM THE DETECTORS. The adversarial lane's decisive
observation was that I27P's five masking forms were exactly the five forms the detectors
recognised — a self-authored oracle, which is why 5/5 proved nothing. The cases below come from
bash's own status rules (backgrounding, `set +o errexit`, subshells, grouping, pipelines),
CPython's documented option handling (`--version`, `-c`, `-m`, value-taking flags), the
repository's tracked-file taxonomy (compound and absent suffixes), and digest widths — sources
none of these modules own.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ci_invocation_model as inv  # noqa: E402
import failure_propagation as fp  # noqa: E402
import leak_scan  # noqa: E402
import package_requirements as pkg  # noqa: E402
import protected_inventory as pi  # noqa: E402

# Assembled, never spelled: leak_scan scans this file, and an unapproved identifier written
# here would be a real finding. Approving it to silence that would be the widening the
# containment exists to refuse.
UNAPPROVED_ACCOUNT = "".join(str(d) for d in (9, 3, 4, 8, 5, 7, 2, 9, 1, 0, 4, 3))


# =====================================================================================
# AGENDA A — failure propagation must FAIL CLOSED
# =====================================================================================

def _verdict(line, *, pipefail=True, disabled=False):
    return fp.classify_line(line, pipefail=pipefail, set_e_disabled=disabled)["verdict"]


def test_a_plain_command_still_propagates():
    assert _verdict("python3 scripts/allow_model.py") == fp.PROPAGATES


def test_the_default_for_an_unprovable_construct_is_not_propagates():
    """THE ROOT CAUSE. The old final branch returned PROPAGATES for anything it did not
    recognise, so every unenumerated construct was certified safe."""
    source = Path(fp.__file__).read_text(encoding="utf-8")
    tail = source.split("def classify_line", 1)[1].split("\ndef ", 1)[0].rstrip().splitlines()
    assert "UNKNOWN" in "\n".join(tail[-8:]), (
        "classify_line no longer ends in a fail-closed UNKNOWN branch")


@pytest.mark.parametrize("line", [
    "python3 x.py &",                       # backgrounded: list status is 0
    "python3 x.py & echo done",
])
def test_backgrounded_commands_are_masked(line):
    """bash -euo pipefail -c 'false & echo done' exits 0. Executed, not assumed."""
    assert _verdict(line) == fp.MASKED


def test_bash_really_does_swallow_a_backgrounded_failure():
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c",
                           "false & echo done"], capture_output=True, text=True)
    assert proc.returncode == 0, "the premise of the backgrounding finding has changed"


@pytest.mark.parametrize("spelling", ["set +e", "set +o errexit", "set +ex"])
def test_every_spelling_of_disabling_errexit_is_recognised(spelling):
    assert fp.ERRMODE_OFF.match(spelling), f"{spelling!r} evades error-mode detection"


@pytest.mark.parametrize("spelling", ["set -e", "set -o errexit"])
def test_re_enabling_errexit_is_recognised(spelling):
    assert fp.ERRMODE_ON.match(spelling)


def test_bash_really_does_swallow_a_failure_after_set_o_errexit():
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c",
                           "set +o errexit; false; echo done"], capture_output=True, text=True)
    assert proc.returncode == 0


@pytest.mark.parametrize("line", [
    "( python3 x.py )",                     # subshell
    "{ python3 x.py; }",                    # group
    "python3 x.py > out 2>&1 && echo ok",   # compound
])
def test_constructs_that_can_displace_status_are_unknown(line):
    assert _verdict(line) == fp.UNKNOWN


# GATE 4N-I27W. `python3 x.py; true` was listed here as MASKED, and that expectation was
# WRONG — it encoded the defect Gate 4N-I27V found by checking the stored expectation against
# bash instead of against the analyser. Bash settles it: `false; true` exits 1 under `set -e`,
# because `;` separates two commands and errexit fires on the first one, so the trailing `true`
# is never reached. The `||` forms below are unaffected: `||` genuinely does catch the failure.
# The corrected case and its errexit-disabled twin live in tests/test_i27w_sequence_semantics.py.
@pytest.mark.parametrize("line,expected", [
    ("python3 x.py || true", fp.MASKED),
    ("python3 x.py || echo skip", fp.MASKED),
    ("python3 x.py; true", fp.PROPAGATES),
])
def test_the_previously_recognised_forms_still_refuse(line, expected):
    assert _verdict(line) == expected


def test_errexit_disabled_masks_everything_after_it():
    assert _verdict("python3 x.py", disabled=True) == fp.MASKED


def test_a_heredoc_opener_is_a_simple_command():
    """Its status is the interpreter's; a heredoc feeds stdin and displaces nothing."""
    assert _verdict("python3 - \"$work\" <<'PY'") == fp.PROPAGATES


def test_the_workflow_is_clean_and_every_step_is_accounted_for():
    result = fp.check()
    assert result["clean"], result["problems"]
    assert result["workflow_steps_total"] > result["graded_steps"], (
        "the analyser is still only looking at graded steps")
    assert result["steps_without_an_id"] > 0
    # Steps outside the graded set are REPORTED, not skipped.
    assert isinstance(result["non_graded_observations"], list)


def test_the_live_mask_outside_the_graded_set_is_reported_not_hidden():
    """ci.yml carries a legitimate `|| true` on a diagnostic step. Gate 4N-I27Q found the
    analyser reporting `masked 0` while that line existed, because id-less steps were skipped."""
    result = fp.check()
    reported = [o for o in result["non_graded_observations"] if o["masked"]]
    assert reported, "a non-graded masked line exists in the workflow but is not reported"


# =====================================================================================
# AGENDA B — an invocation must actually EXECUTE the target
# =====================================================================================

@pytest.mark.parametrize("command", [
    "python3 scripts/leak_scan.py",
    "python3 -I scripts/leak_scan.py",
    "python3 -X dev scripts/leak_scan.py",
    "python3 -W ignore scripts/leak_scan.py",
])
def test_real_invocations_are_recognised(command):
    assert "scripts/leak_scan.py" in inv.invoked_targets(command)


@pytest.mark.parametrize("command", [
    "python3 --version scripts/leak_scan.py",
    "python3 -V scripts/leak_scan.py",
    "python3 -h scripts/leak_scan.py",
    "python3 --help scripts/leak_scan.py",
    "python3 -c 'pass' scripts/leak_scan.py",
    "echo scripts/leak_scan.py",
    "printf '%s' scripts/leak_scan.py",
])
def test_non_executing_invocations_yield_nothing(command):
    assert "scripts/leak_scan.py" not in inv.invoked_targets(command)


def test_python_version_really_does_run_nothing():
    proc = subprocess.run([sys.executable, "--version", "/nonexistent/definitely_missing.py"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, "the premise of the --version finding has changed"


def test_a_non_runner_module_argument_is_not_harvested():
    assert "scripts/leak_scan.py" not in inv.invoked_targets(
        "python3 -m json.tool scripts/leak_scan.py")


def test_every_graded_step_still_satisfies_the_invocation_contract():
    result = inv.check()
    assert result["clean"], result["problems"]


# =====================================================================================
# AGENDA D + G — file coverage and account tokenisation
# =====================================================================================

@pytest.mark.parametrize("name", [
    "terraform.tfvars.example", "config.example", ".env.sample", "Dockerfile",
    "settings.ini", "notes.rst", "data.toml",
])
def test_compound_and_unfamiliar_text_files_are_scanned(tmp_path, name):
    path = tmp_path / name
    path.write_text("nothing sensitive here\n", encoding="utf-8")
    scannable, reason = leak_scan.is_scannable(path)
    assert scannable, f"{name} would not be scanned: {reason}"


@pytest.mark.parametrize("name,content", [
    ("image.png", b"\x89PNG\r\n\x1a\n\x00binary"),
    ("blob.bin", b"\x00\x01\x02\x03"),
])
def test_binary_files_are_excluded_with_a_stated_reason(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    scannable, reason = leak_scan.is_scannable(path)
    assert not scannable and reason


def test_vendored_and_generated_paths_are_excluded(tmp_path):
    path = tmp_path / "node_modules" / "pkg" / "index.js"
    path.parent.mkdir(parents=True)
    path.write_text("x", encoding="utf-8")
    assert not leak_scan.is_scannable(path)[0]


def test_the_repository_selection_actually_includes_unusual_suffixes():
    """BEHAVIOURAL, not textual. A first version of this test grepped the source for one
    spelling of the old suffix filter; Gate 4N-I27R's own falsification sweep reverted the
    selection with a DIFFERENT spelling and the test did not notice. What must be pinned is
    that the real selection reaches these files, so this asks the selector itself.
    """
    selected = {str(p.relative_to(REPO_ROOT)) for p in leak_scan.candidate_files()}
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
                             text=True).stdout.split()
    unusual = [p for p in tracked
               if Path(p).suffix.lower() not in leak_scan.SCAN_SUFFIXES
               and leak_scan.is_scannable(REPO_ROOT / p)[0]]
    assert unusual, "no tracked file with an unusual suffix exists to prove the point"
    missed = [p for p in unusual if p not in selected]
    assert not missed, (
        "the repository scan does not reach tracked text files whose suffix is outside "
        f"SCAN_SUFFIXES: {missed[:5]}")


def test_the_example_suffix_that_defeated_the_rejected_candidate_is_scanned():
    """`infra/aws/terraform.tfvars.example` was a MODIFIED path in the rejected candidate and
    was never scanned, so an account id and a role ARN placed there passed cleanly."""
    target = REPO_ROOT / "infra/aws/terraform.tfvars.example"
    if not target.exists():
        pytest.skip("the example tfvars file is not present in this tree")
    selected = {str(p.relative_to(REPO_ROOT)) for p in leak_scan.candidate_files()}
    assert "infra/aws/terraform.tfvars.example" in selected


@pytest.mark.parametrize("text,expected", [
    (f"account {UNAPPROVED_ACCOUNT}", True),
    (f"{UNAPPROVED_ACCOUNT}abcdef", True),
    ("deadbeef" * 3 + UNAPPROVED_ACCOUNT, True),
    (UNAPPROVED_ACCOUNT + "abcdef" * 4, True),
    (f"arn:aws:iam::{UNAPPROVED_ACCOUNT}:role/x", True),
    ("f71d58ce895329d848631650004ebfe1f6227b9b", False),   # a real git sha
    ("a" * 64, False),                                      # a real sha256
    ("account 111122223333", False),                        # an approved placeholder
    ("deadbeef" * 3 + "111122223333", False),
])
def test_account_tokenisation_boundaries(text, expected):
    assert bool(leak_scan.scan_text(text)) is expected


def test_a_public_certificate_is_not_credential_material():
    """Widening the scan surfaced this: an AWS key id is UPPERCASE by specification, and
    matching it case-insensitively made random certificate base64 a 'credential'."""
    bundle = REPO_ROOT / "apps/revision-reader/assets/rds-global-bundle.pem"
    if not bundle.exists():
        pytest.skip("the CA bundle is not present in this tree")
    assert leak_scan.scan_text(bundle.read_text(encoding="utf-8")) == []


def test_a_real_access_key_id_is_still_credential_material():
    assert leak_scan.scan_text("AKIA" + "A1B2C3D4E5F6G7H8")


def test_the_graded_containment_command_is_clean():
    proc = subprocess.run([sys.executable, "scripts/leak_scan.py"], cwd=REPO_ROOT,
                          capture_output=True, text=True,
                          env={**__import__("os").environ,
                               "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


# =====================================================================================
# AGENDA E — regression pins, both directions
# =====================================================================================

def test_every_remediation_family_is_pinned_and_present():
    present = pkg.tree_paths(
        subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True,
                       text=True).stdout.strip())
    result = pkg.check_remediation_pins(present)
    assert not result["problems"], result["problems"]
    assert result["bidirectional"]


def test_deleting_a_pin_is_detected():
    present = pkg.tree_paths(
        subprocess.run(["git", "write-tree"], cwd=REPO_ROOT, capture_output=True,
                       text=True).stdout.strip())
    reduced = {p for p in present if p != "tests/test_i27o_blocker_remediations.py"}
    result = pkg.check_remediation_pins(reduced)
    assert any("ABSENT" in p for p in result["problems"]), (
        "deleting the sole regression pin for the I27O remediations went undetected")


def test_the_pin_registry_is_not_generated_from_the_tests_directory():
    import ast

    tree = ast.parse(Path(pkg.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "remediation_pins")
    calls = {getattr(c.func, "attr", getattr(c.func, "id", "")) for c in ast.walk(func)
             if isinstance(c, ast.Call)}
    assert "glob" not in calls and "rglob" not in calls, (
        "the registry enumerates the tree, so it agrees with the tree by construction")


def test_a_registry_entry_naming_a_missing_test_is_refused(monkeypatch, tmp_path):
    doc = {"remediations": [{"family": "X", "test_path": "tests/test_does_not_exist.py",
                             "source_paths": [], "canary": "c", "expected_control": "e"}]}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(pkg, "PIN_REGISTRY", path)
    result = pkg.check_remediation_pins({"tests/test_does_not_exist.py"})
    assert any("does not exist" in p for p in result["problems"])


# =====================================================================================
# AGENDA F — recursive self-attestation
# =====================================================================================

def _fixture():
    return json.loads(pi.SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))


def test_the_tracked_fixture_is_still_accepted():
    assert pi.self_attesting_fields(_fixture()) == []


def test_a_nested_integrity_shaped_name_is_refused():
    data = _fixture()
    data["trails"] = [{"sha256": "a" * 64}]
    assert pi.self_attesting_fields(data)


def test_a_deeply_nested_integrity_shaped_name_is_refused():
    data = _fixture()
    data["db"] = {"a": {"b": {"fingerprint": "b" * 64}}}
    assert pi.self_attesting_fields(data)


def test_a_nested_self_digest_is_refused():
    """The rename evasion moved one level down. Depth must not change the answer."""
    import hashlib

    data = _fixture()
    data["aliases"] = {"opaque": "PLACEHOLDER"}
    without = {**data, "aliases": {}}
    data["aliases"] = {"opaque": hashlib.sha256(pi.canonical_bytes(without)).hexdigest()}
    offenders = pi.self_attesting_fields(data)
    assert any("own canonical digest" in o for o in offenders), offenders


def test_an_unrelated_nested_hex_value_is_not_a_false_positive():
    data = _fixture()
    data["ecr"] = {"opaque_id": "c" * 64}
    assert pi.self_attesting_fields(data) == []


def test_tier_2_without_evidence_still_fails_closed():
    with pytest.raises(pi.InventoryError):
        pi.load({"SIGNALNEST_ANCHOR_TIER": pi.TIER_PROTECTED})


# =====================================================================================
# GATE 4N-I27S — a fixture may not cite a guard that does not exist
# =====================================================================================
#
# The approved-account registry claimed its independence was proven by a test module that had
# never been written. Two reviewer lanes found it; no control refused it, because nothing
# checked that a path named inside a fixture resolves. The guard existed under another name, so
# the security property was intact — but a provenance claim pointing at nothing is
# indistinguishable from one pointing at something, which is what makes it dangerous.

def test_every_repository_path_the_registry_names_exists():
    leak_scan.require_registry_references_resolve()


def test_the_registry_cites_a_guard_that_actually_exercises_its_independence():
    """The replacement must not be an arbitrary existing file."""
    text = leak_scan.APPROVED_ACCOUNT_REGISTRY.read_text(encoding="utf-8")
    cited = [r for r in
             __import__("re").findall(r"tests/[A-Za-z0-9_./-]+\.py", text)]
    assert cited, "the registry no longer cites any guard"
    for path in cited:
        body = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert "approved_accounts" in body and "ALLOWED_ACCOUNTS" in body, (
            f"{path} is cited as the independence guard but does not exercise the registry")


# The non-.py case deliberately names scripts/, NOT tests/fixtures/. commit_package_coherence
# requires every tests/fixtures/ path a committed file mentions to be IN the commit, and it is
# right to: an unresolvable fixture reference is the same class of defect this control closes.
# Naming a fixture here would have forced an exemption in that guard, and an exemption is how
# the blind spot comes back. scripts/ exercises the identical .json branch of the reference
# pattern without asking any other control to look away.
@pytest.mark.parametrize("replacement", [
    "tests/test_does_not_exist.py",       # nonexistent .py reference
    "scripts/does-not-exist.json",        # nonexistent non-.py reference
])
def test_a_nonexistent_reference_is_refused(monkeypatch, tmp_path, replacement):
    original = leak_scan.APPROVED_ACCOUNT_REGISTRY.read_text(encoding="utf-8")
    mutated = tmp_path / "registry.json"
    mutated.write_text(
        original.replace("tests/test_i27o_blocker_remediations.py", replacement, 1),
        encoding="utf-8")
    monkeypatch.setattr(leak_scan, "APPROVED_ACCOUNT_REGISTRY", mutated)
    with pytest.raises(leak_scan.AccountRegistryError, match="do not exist"):
        leak_scan.require_registry_references_resolve()


def test_the_reference_check_runs_before_any_scan():
    """It is wired into require_registered_allowed_accounts(), which scan_repository() calls
    first — so a dangling citation refuses instead of producing a clean scan."""
    import ast

    tree = ast.parse(Path(leak_scan.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "require_registered_allowed_accounts")
    called = {getattr(c.func, "id", getattr(c.func, "attr", "")) for c in ast.walk(func)
              if isinstance(c, ast.Call)}
    assert "require_registry_references_resolve" in called


def test_the_registry_still_refuses_an_arbitrary_account_addition(monkeypatch):
    monkeypatch.setattr(leak_scan, "ALLOWED_ACCOUNTS",
                        frozenset(leak_scan.ALLOWED_ACCOUNTS | {UNAPPROVED_ACCOUNT}))
    with pytest.raises(leak_scan.AccountRegistryError, match="no registry entry"):
        leak_scan.require_registered_allowed_accounts()


# =====================================================================================
#
# Gate 4N-I27S Phase R, falsification F14.
#
# Truncating mutation_discovery.discover_sites() to its first hundred entries was caught by
# NOTHING. The four strict guards did go red — but they are red on a clean tree too, for
# unrelated open findings, so their redness carries no information about this mutation. A
# detector that cannot be green is not a detector, and "the strict guards failed" would have
# been false credit of exactly the kind this chain keeps finding.
#
# The site universe is the generator for mutation testing: every assurance claim about
# collection coverage is computed over it. Silently shrinking it shrinks the assurance surface
# while every number still reads as a pass.
#
# So the reconciliation Gate 4N-I27S performed is pinned here as behaviour rather than left as
# narrative. The RAW count is deliberately NOT the invariant: adding a reviewed fixture
# legitimately adds a site, which is why the raw figure drifted 209 -> 210 -> 211 across I27N,
# I27Q and I27R while nothing about the production surface changed. The bound figure is the
# production/control tally, which held at 126 across all four trees.

_REMEDIATION_FIXTURES = frozenset({"approved-account-registry.json",
                                   "remediation-pin-registry.json"})

# Gate 4N-I27U moved this from 126 to 127, and the pin is what forced the move to be EXPLAINED
# rather than absorbed. The delta was established by IDENTITY, not by re-baselining a number:
# exactly one site was added, `failure_propagation.py::shell_contract`, and none was removed.
# That function is the Agenda A remediation itself — the model of the shell's real option state,
# which the analyser previously computed and threw away. A new control legitimately adds a
# control site; the pin exists so that the alternative (a control silently DISAPPEARING) cannot
# hide in the same number.
#
# GATE 4N-I28K moved it from 127 to 483, and the reason is that 127 was never a count of the
# controls — it was a count of the FUNCTION NAMES that happened to end in one of eleven words,
# inside the files ci.yml happens to mention literally. Gate 4N-I28J proved what that measured:
# renaming a live control removed it, a never-called `never_called_check` entered on its name,
# and deleting the word "check" took eleven controls out of the universe with no code change.
#
# The delta is established by IDENTITY in both directions, and the direction is what matters:
#
#     retained  83   every site the old rule found is still a site
#     removed    0   nothing left the universe
#     added    356   339 functions the name rule could not see, plus 17 in the new module itself
#
# The additions are not a re-baselining. `leak_scan.py` used to be represented by `main` ALONE
# while `scan_text` — the protected-token detector — `is_scannable`, `scan_decision`,
# `candidate_files`, `scan_repository` and `_CredentialRule.search` were all outside the
# universe; ten further modules reached only through imports, `iam_eval.py` among them, were
# invisible because the file filter read ci.yml for literal filenames. 439 function sites plus
# the 44 graded workflow steps is what the enforcement path actually contains.
#
# The pin keeps doing its original job. A control disappearing still moves this number, and the
# taxonomy mutation matrix in tests/test_i28k_site_taxonomy.py proves the number now responds to
# enforcement rather than to spelling.
# GATE 4N-I28M moved it from 483 to 474, and again the delta is established by IDENTITY. Gate
# 4N-I28L proved two sites had no independent enforcement consequence, and both had a CAUSE that
# reached further than the one symbol that exposed it:
#
#   17 removed, 8 added
#
#   iam_eval.py::Evaluation.allowed          NOT A SITE — a property referenced nowhere in the
#                                            repository, admitted only because constructing its
#                                            class used to admit every member
#   candidate_manifest.py::Candidate.redacted  NOT A SITE — same cause, same evidence
#   site_taxonomy.py::_class_members          NOT A SITE — nothing calls it after the fix
#   arn_model.py::Arn.differs_from            -> CI_RELEASE, its only caller is test-only
#   13 production_certification.py functions  -> CI_RELEASE. `ci.yml` runs `production_certification
#                                            .py state`, and these are reachable only through the
#                                            `eligibility`, `certify` and `verify` subcommands it
#                                            never invokes. The graded suite does exercise them,
#                                            so they are release controls — just not production
#                                            ones.
#   8 added                                   the helpers this gate added to site_taxonomy.py,
#                                            reachable through mutation_discovery
#
# Nothing that the workflow actually executes left the universe: every function traced from
# `production_certification.py state` is still a production site.
# GATE 4N-I28O moved it from 474 to 501, and again by IDENTITY. Gate 4N-I28N proved two false
# EXCLUSIONS by execution:
#
#   the graded `certification_gate` step invokes production_certification.py with `verify`,
#   `eligibility` and `certify` through `subprocess.run` inside a heredoc, which the root model
#   could not see, so thirteen functions a graded step really runs had been pruned out;
#
#   `_prune_dispatch.Pruner.visit_If` — a class defined inside a function, dispatched by
#   `ast.NodeTransformer.visit` — executes under a graded command and belonged to no category.
#
# Both are restorations, not re-baselining: nothing left the universe, and every addition is a
# function a graded CI command executes or reaches through a resolved root.
#
# GATE 4N-I28S moved it from 501 to 527, and the move is accounted for identity by identity. The
# gate replaced comment-derived command roots with executable-semantics derivation, which required
# new code; the universe grew by exactly that code and by nothing else:
#
#   +19  shell_command_model.py     the bounded shell parser this gate added. It is reached from
#                                   site_taxonomy.release_roots, which every graded guard command
#                                   reaches, so its functions are production sites on the same
#                                   rule as any other helper.
#    +8  site_taxonomy.py           _aggregator_steps (+ its cached producer), _release_role,
#                                   _jobs_without_graded_steps (+ producer), _resolved_roots
#                                   (+ its inner record), _shell_indirection
#    -1  site_taxonomy.py::release_roots.record   the inner helper moved into _resolved_roots and
#                                   is now site_taxonomy.py::_resolved_roots.record. A RENAME, not
#                                   a deletion.
#
#   457 - 1 + 27 = 483 function sites; 483 + 44 graded steps = 527.
#
# What did NOT happen is the point. No pre-existing behavioural site was added or removed, no root
# module entered or left the root set, and no argv changed — verified by diffing the full sorted
# site set and the full root set against the pre-remediation tree. smoke_http.py is still in the
# universe, now through `bash scripts/ci-smoke.sh` -> ci-smoke.sh:63 rather than through a comment.
# The two I28Q comment-only mutations now move nothing at all (they moved 457 -> 454 and
# 457 -> 465 before), while a real executable edit still moves the universe, so the harness that
# proves this can still report movement.
# GATE 4N-I28Y: 527 -> 530, and this is a control being ADDED, not a number being
# re-baselined. Three functions were added to scripts/ci_invocation_model.py to give the
# graded pytest command an OPTION contract: _pytest_options, _has_sequence and
# _option_problems. Gate 4N-I28X proved that `must_invoke` alone let `--deselect` remove
# every mandatory assurance control while the step still 'invoked pytest'. That module was
# already a graded root, so its new functions are production/control sites by the same rule
# that admits every other one. Command roots stay 41 and graded steps stay 44; exactly these
# three identities are new, and each is named in
# ~/.signalnest/generated/4n-i28y/control-category-reconciliation.json.
EXPECTED_PRODUCTION_CONTROL_SITES = 867  # INFRA-9-B3: +29 (28 root_wiring_check.py function sites + 1 root_wiring graded step; 0 removed)  # BH-C-E1: +1 (collection_completeness.completeness_applicable helper for registry-derived completeness applicability)  # BH-C: +1 (critical_list_inventory._collection_value helper added by the F8 discovery extension)  # E2: +9 (site_coverage kind-aware redesign helpers x6 + ci_env_dataflow _denotes_environ/_ambient_allow_list + ci_harness _require_adequate_interpreter; 0 removed)
# GATE 4N-I28BH-B (this gate): 790 -> 827. Exactly 37 sites added (35 + the SCAN_DECISIONS runtime-schema predicate _runtime_scan_decisions_schema, which replaced SCAN_DECISIONS review-pin after review found it a runtime accumulator), ZERO removed (established by
# identity against START_TREE 45eb4d72). CAUSE: the property-specific security-collection assurance
# machinery landed. 1 graded_step: the new mandatory ci.yml step `scripts/security_collection_
# assurance.py` (sibling validator whose outcome the aggregator reads, so it blocks release ->
# release_roots 41 -> 42). 34 functions: that module's 23 functions (incl. the _root_of_trust +
# _canonical_file_digest self-governance pair) + review_pin_control.py's 10
# (the reviewed-integrity control it delegates to, reached from the new graded root) + critical_
# list_inventory.py::assurance_registry. Corroborated by site_taxonomy's canonical count (746 -> 778,
# +32 functions; the +1 delta vs here is the graded_step, which the function-only derivation omits)
# corroborated by the canonical count 746 -> 780. An added control, explained — not a re-baseline.
# --- prior movement (retained for the audit trail) ---
# GATE 4N-I28BH-B0a-SLICE2: 538 -> 790. Exactly 252 sites added, zero removed. CAUSE: the signed
# closed-capability completeness verifier landed as scripts/completeness_framework.py and is reached
# from the already-graded release command root scripts/collection_completeness.py (which imports it to
# perform certificate-backed VAL-I28AX-01 completeness checking). By the same rule that admits every
# other function reachable from a release root, its functions are production/control sites. The +252
# is corroborated by a second independent derivation (site_taxonomy.production_control_function_sites
# 494 -> 746, identical +252; test_i28ar c35 re-pinned to 746), release_roots stay 41, graded steps
# stay 44, and 0 sites fall to UNKNOWN — so this is an established landing, not a re-baseline. The
# framework's call graph is statically resolvable (site_taxonomy unresolved_calls == 0) after the
# Option-2 re-cert, so every one of the 252 is a followed, categorised site, not an understatement.
# GATE 4N-I28AR: 536 -> 538. Exactly two sites added, zero removed, established by diffing the
# derived site set against tree 763b094e rather than by re-baselining:
#     cache_authority.py::deep_freeze
#     site_taxonomy.py::_store
#
# CAUSE. Closing ADV-I28AP-03 required every derived value entering site_taxonomy._DERIVED to enter
# FROZEN, so _store() became the single population point and it calls cache_authority.deep_freeze().
# Both are newly reachable from a release command root for the first time, which is the intended
# effect of putting the cache-authority layer ON the enforcement path rather than beside it.
#
# WHY cache_authority.verify() is NOT among them, checked rather than assumed: verify() is reached
# only from signalnest_bootstrap.establish() and reverify(), and signalnest_bootstrap contributes
# ZERO sites to this universe because it is a pytest plugin loaded by `-p`, not a script invoked
# from a command root. The same rule excludes every other layer's entry point.
# GATE 4N-I28AK: 530 -> 536. Exactly six sites added, zero removed, all in
# external_executable_trust.py: _digest, _hardened_env, load_policy, resolve, tar_invocation,
# validated_path.
#
# CAUSE, established by diffing the site set against the pre-I28AK tree rather than by
# re-baselining: closing ADV-I28AJ-01 required commit_package_coherence.materialize() to obtain a
# VALIDATED absolute tar instead of invoking a bare name. A release-reachable module therefore now
# imports the trust layer, and those six functions became reachable from a release command root for
# the first time. The trust layer moving ONTO the enforcement path is the intended effect of the
# fix. site_taxonomy's parallel universe moved by the identical six (486 -> 492), which is the
# cross-check that this is a real movement and not discovery drift.
# (_digest, _hardened_env, load_policy, resolve, tar_invocation, validated_path), zero removed.
# CAUSE, and it is a real movement rather than a re-baseline: closing ADV-I28AJ-01 required
# commit_package_coherence.materialize() to obtain a VALIDATED absolute tar instead of invoking a
# bare name, so a release-reachable module now imports the trust layer and those functions became
# reachable from a release command root for the first time. The trust layer moving ONTO the
# enforcement path is the intended effect of the fix, not a side effect of discovery drift.


def _site_category(site):
    module = str(site.get("module") or "")
    sid = str(site.get("id") or "")
    kind = site.get("kind")
    if module in _REMEDIATION_FIXTURES or any(f in sid for f in _REMEDIATION_FIXTURES):
        return "REMEDIATION_CANARY_SITE"
    if module.startswith("test_") or "/tests/" in sid or sid.startswith("tests/"):
        return "TEST_ASSERTION_SITE"
    if module.endswith(".json") and kind == "requirement_key":
        return "TEST_FIXTURE_SITE"
    if kind in ("function", "graded_step", "requirement_key"):
        return "PRODUCTION_CONTROL_SITE"
    return "UNKNOWN"


def test_the_production_control_site_universe_holds_at_its_reconciled_size():
    import mutation_discovery

    sites = mutation_discovery.discover_sites()
    production = [s for s in sites if _site_category(s) == "PRODUCTION_CONTROL_SITE"]
    assert len(production) == EXPECTED_PRODUCTION_CONTROL_SITES, (
        f"the production/control site universe moved to {len(production)} from "
        f"{EXPECTED_PRODUCTION_CONTROL_SITES}. This is not a number to re-baseline: either a "
        f"control was added or removed, or discovery itself regressed. Establish which before "
        f"changing this pin.")


def test_no_discovered_site_falls_outside_the_reconciled_taxonomy():
    """An UNKNOWN category means discovery's shape changed under the categoriser, which would
    let sites drift between buckets without moving any total."""
    import mutation_discovery

    unknown = [s for s in mutation_discovery.discover_sites()
               if _site_category(s) == "UNKNOWN"]
    assert not unknown, f"{len(unknown)} site(s) match no category: {unknown[:5]}"


def test_site_identifiers_are_unique():
    """Duplicate ids would let one site be counted twice and mask the loss of another."""
    import mutation_discovery

    ids = [s.get("id") for s in mutation_discovery.discover_sites()]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate site ids: {sorted(duplicates)[:5]}"
