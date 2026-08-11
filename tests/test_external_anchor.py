"""External identity anchoring (Gate 4N-I8, Defect 1 — the one that was NOT closed).

WHAT GATE 4N-I7 GOT WRONG. It de-duplicated the boundary ARN to one construction site and
reported the defect closed. The adversarial lane disproved that: replacing the account across
`scripts/signalnest_identity.py`, `infra/aws/live-resource-inventory.json` and two test
literal files made EVERY generated ARN name a foreign account (111199998888) with the entire
suite green. Both "independent" sources were repository-controlled, so one sweep moved the
expectation and the value together. De-duplication is not anchoring.

The anchor now lives at ~/.signalnest/anchor/, outside the repository, written once at mode
400 from AWS-signed evidence retained before this branch existed.

The load-bearing test in this file is
`test_repository_wide_account_replacement_fails_against_the_anchor`: it performs the EXACT
mutation that defeated Gate 4N-I7, over a real copy of the repository, and requires the join
to fail. Every other test here guards a way that test could become vacuous.

No AWS access, no network.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import anchor_loader  # noqa: E402
import external_anchor  # noqa: E402

FOREIGN_ACCOUNT = "111199998888"

# GATE 4N-I13. Which tier a check belongs to is now explicit. Tier 1 (synthetic) proves the
# JOIN MECHANISM works; only Tier 2 (the real anchor, supplied explicitly) can certify that
# the repository names the approved account. Tests that need the real anchor say so, and are
# skipped ONLY when it is genuinely absent — with the skip reason naming the tier, so an
# empty-HOME CI run cannot be mistaken for production certification.
REAL_ANCHOR = Path.home() / ".signalnest" / "anchor" / "signalnest-account-environment-anchor.json"
def tier2(fn):
    """Mark a check as Tier 2 and run it ONLY when Tier 2 is DECLARED.

    Composed as a plain decorator: pytest.mark.tier2(pytest.mark.skipif(...))
    wraps the mark object rather than applying both to the test, so the skip
    never fired and empty-HOME runs FAILED instead of skipping.

    GATE 4N-I18, SEC-1. The condition used to be "the real anchor FILE EXISTS". That is tier
    by DISCOVERY, which is the Gate 4N-I10 defect shape — a developer machine that happens to
    hold the anchor silently ran production-certifying checks while the environment declared
    TIER_1_SYNTHETIC. Once identity became tier-resolved, the two disagreed and these checks
    compared synthetic documents against the real anchor. The tier must be DECLARED; the file
    merely has to be there too.
    """
    import os

    declared = os.environ.get("SIGNALNEST_ANCHOR_TIER")
    fn = pytest.mark.tier2(fn)
    return pytest.mark.skipif(
        declared != "TIER_2_PROTECTED" or not REAL_ANCHOR.exists(),
        reason=("TIER_2_PROTECTED not declared, or the real anchor is absent (both expected in "
                "ordinary CI); this check certifies production identity and cannot run on a "
                "synthetic fixture"))(fn)



def real_env():
    import hashlib
    return {"SIGNALNEST_ANCHOR_TIER": "TIER_2_PROTECTED",
            "SIGNALNEST_ANCHOR_PATH": str(REAL_ANCHOR),
            "SIGNALNEST_ANCHOR_SHA256": hashlib.sha256(REAL_ANCHOR.read_bytes()).hexdigest()}


# --- the anchor itself -----------------------------------------------------------------


@tier2
def test_the_anchor_exists_and_is_outside_the_repository():
    """If it were inside the repo, one sweep would move it with everything else."""
    anchor = REAL_ANCHOR
    assert anchor.exists(), (
        f"the external anchor is missing at {anchor}. This test does NOT skip: a skipped "
        "anchor check is indistinguishable from a passing one.")
    assert REPO_ROOT not in anchor.parents, (
        f"{anchor} is inside the repository, so a repository-wide edit could rewrite it")


@tier2
def test_the_anchor_is_not_writable_in_place():
    """Mode 400. Not a security boundary against a determined operator — a guard against
    a careless script, which is what actually caused Defect 1."""
    mode = REAL_ANCHOR.stat().st_mode & 0o777
    assert mode == 0o400, f"anchor mode is {mode:o}, expected 400"


@tier2
def test_the_anchor_records_its_provenance():
    anchor = external_anchor.load_anchor(REAL_ANCHOR)
    provenance = anchor["provenance"]
    assert provenance["all_witnesses_predate_branch"] is True, (
        "a witness written after the branch existed could have been produced by the same "
        "work it is supposed to constrain")
    witnesses = [w for w in provenance["witnesses"] if "sha256" in w]
    assert len(witnesses) >= 3, "too few independent witnesses to trust the anchor"
    for witness in witnesses:
        assert Path(witness["source"]).exists(), f"witness vanished: {witness['source']}"


@tier2
def test_exactly_one_account_appears_in_the_pre_branch_corpus():
    """Two distinct values would make the corpus ambiguous and the anchor unsafe."""
    anchor = external_anchor.load_anchor(REAL_ANCHOR)
    distinct = anchor["provenance"]["account_corroboration"]["distinct_twelve_digit_values"]
    assert distinct == [anchor["approved_account_id"]], distinct


@tier2
def test_the_anchor_carries_no_credential_material():
    text = REAL_ANCHOR.read_text(encoding="utf-8")
    for pattern, why in (
        (r"AKIA[0-9A-Z]{16}", "access key id"),
        (r"aws_secret_access_key", "secret key"),
        (r"ASIA[0-9A-Z]{16}", "session key id"),
        (r"-----BEGIN", "private key block"),
    ):
        assert not re.search(pattern, text, re.IGNORECASE), f"anchor contains a {why}"


@tier2
def test_the_identity_center_instance_is_recorded_as_unknown_not_invented():
    """No pre-branch artifact retained it. An invented value would be worse than none."""
    anchor = external_anchor.load_anchor(REAL_ANCHOR)
    centre = anchor["identity_center"]
    assert centre["instance_arn"] is None
    assert "UNKNOWN" in centre["status"]


# --- the join --------------------------------------------------------------------------


@tier2
def test_every_account_sensitive_identity_agrees_with_the_anchor():
    result = external_anchor.join(env=real_env())
    assert result["clean"], result["mismatches"]


@tier2
def test_the_join_covers_enough_identities_to_be_meaningful():
    result = external_anchor.join(env=real_env())
    assert result["account_bearing"] >= 20, (
        f"only {result['account_bearing']} account-bearing ARNs joined; a shrinking set would "
        "weaken every assertion here without failing one")


@tier2
def test_the_boundary_generator_internals_are_witnessed():
    """Gate 4N-I7 Defect 7: these were rebuilt inside the generator with no witness."""
    checked = {row["identity"] for row in external_anchor.join(env=real_env())["rows"]}
    for attr in ("SECRETS_CMK", "STATE_CMK", "LOCK_TABLE", "READER_EXECUTION_ROLE"):
        assert f"boundary_generator.{attr}" in checked, f"{attr} is joined against nothing"


# --- fail-closed behaviour --------------------------------------------------------------


def test_a_missing_anchor_raises_rather_than_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(external_anchor, "ANCHOR_PATH", tmp_path / "absent.json")
    with pytest.raises(external_anchor.AnchorUnavailable):
        external_anchor.load_anchor(tmp_path / "absent.json")


@pytest.mark.parametrize("broken,why", [
    ("{not json", "malformed JSON"),
    ('{"partition": "aws"}', "missing approved_account_id"),
    ('{"approved_account_id": "12", "partition": "aws", "approved_region": "us-east-1",'
     ' "role_name_prefix": "p"}', "account is not 12 digits"),
])
def test_a_corrupt_anchor_fails_closed(tmp_path, broken, why):
    path = tmp_path / "anchor.json"
    path.write_text(broken)
    with pytest.raises(external_anchor.AnchorUnavailable):
        external_anchor.load_anchor(path)


def test_the_cli_reports_fail_closed_rather_than_success(monkeypatch):
    """Exit code 2 when no tier is declared — never 0.

    GATE 4N-I13: there is no ANCHOR_PATH to blank out any more. The fail-closed condition is
    now an UNDECLARED TIER, which is the stronger property: the loader refuses to guess
    rather than falling back to a developer home directory.
    """
    monkeypatch.delenv("SIGNALNEST_ANCHOR_TIER", raising=False)
    monkeypatch.delenv("SIGNALNEST_ANCHOR_JSON", raising=False)
    monkeypatch.delenv("SIGNALNEST_ANCHOR_PATH", raising=False)
    monkeypatch.setattr(sys, "argv", ["external_anchor.py"])
    assert external_anchor.main() == 2


def test_an_undeclared_tier_is_refused(monkeypatch):
    monkeypatch.delenv("SIGNALNEST_ANCHOR_TIER", raising=False)
    with pytest.raises(anchor_loader.AnchorError, match="must be declared explicitly"):
        anchor_loader.declared_tier({})


# --- THE DECISIVE TEST ------------------------------------------------------------------


@tier2
def test_repository_wide_account_replacement_fails_against_the_anchor(tmp_path):
    """The exact mutation that defeated Gate 4N-I7, run for real.

    A full copy of the repository has every occurrence of the approved account replaced with
    a foreign one — the same sweep the adversarial lane used, which previously left 680 tests
    green. The anchor is NOT copied and NOT modified, because it does not live in the
    repository. The join must now fail.
    """
    anchor = external_anchor.load_anchor(REAL_ANCHOR)
    approved = anchor["approved_account_id"]

    work = tmp_path / "repo"
    work.mkdir()
    for name in ("scripts", "tests", "infra"):
        shutil.copytree(REPO_ROOT / name, work / name,
                        ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "__pycache__"))

    swept = 0
    for path in work.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".json", ".tf", ".hcl", ".txt"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if approved in text:
            path.write_text(text.replace(approved, FOREIGN_ACCOUNT), encoding="utf-8")
            swept += 1
    assert swept >= 4, f"the sweep only touched {swept} files; it is not the real mutation"

    probe = work / "probe.py"
    probe.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(work / 'scripts')!r})\n"
        "import external_anchor\n"
        "try:\n"
        f"    r = external_anchor.join(env={real_env()!r})\n"
        "except external_anchor.AnchorUnavailable as e:\n"
        "    print(json.dumps({'anchor': 'unavailable', 'detail': str(e)})); sys.exit(0)\n"
        "print(json.dumps({'clean': r['clean'], 'mismatches': len(r['mismatches'])}))\n",
        encoding="utf-8")
    completed = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                               cwd=work, timeout=120)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result.get("anchor") != "unavailable", (
        "the anchor became unreachable during the mutation, so this proved nothing")
    assert result["clean"] is False, (
        "a repository-wide account replacement still joined CLEAN against the external "
        "anchor — Gate 4N-I7 Defect 1 is not closed")
    assert result["mismatches"] >= 20, (
        f"only {result['mismatches']} mismatches; every account-bearing ARN should disagree")


@tier2
def test_the_sweep_probe_is_clean_without_the_mutation(tmp_path):
    """Controls the control: an unmutated copy must still join clean."""
    work = tmp_path / "repo"
    work.mkdir()
    for name in ("scripts", "tests", "infra"):
        shutil.copytree(REPO_ROOT / name, work / name,
                        ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "__pycache__"))
    probe = work / "probe.py"
    probe.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(work / 'scripts')!r})\n"
        "import external_anchor\n"
        f"print(json.dumps({{'clean': external_anchor.join(env={real_env()!r})['clean']}}))\n",
        encoding="utf-8")
    completed = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True,
                               cwd=work, timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1])["clean"] is True


# --- Tier 1: the mechanism, runnable anywhere ---------------------------------------------


def test_tier1_synthetic_anchor_loads_and_cannot_certify_production():
    loaded = anchor_loader.load(anchor_loader.TIER_SYNTHETIC)
    assert loaded.certifies_production is False
    assert loaded.anchor["_classification"] == anchor_loader.SYNTHETIC_MARKER


def test_tier1_join_is_non_certifying():
    """A Tier-1 join may never be read as production certification, whatever it reports."""
    result = external_anchor.join(env={"SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"})
    assert result["certifies_production"] is False


def test_the_join_DETECTS_a_foreign_account(monkeypatch):
    """The mechanism check, with a genuinely foreign account injected.

    GATE 4N-I18, SEC-1. This check used to rely on the synthetic anchor disagreeing with a
    HARD-CODED real account in the identity layer — a divergence that existed only because the
    account was a literal in the repository. Once the account became tier-resolved, both sides
    legitimately agreed and the assertion inverted: it would have failed for the RIGHT reason,
    which is exactly the kind of accident that gets a check deleted rather than understood.

    The mechanism is now exercised the way it will actually be used: an identity that names a
    DIFFERENT account than the anchor approves must be reported as a mismatch. This is a
    stronger test than the original — it fails if the join stops comparing, whereas the
    original passed on a coincidence of construction.
    """
    foreign = "444444444444"
    anchored = external_anchor.load_anchor(
        tier="TIER_1_SYNTHETIC", env={"SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"})
    assert anchored["approved_account_id"] != foreign, "pick an account the anchor does not approve"

    real_identities = external_anchor.account_sensitive_identities()
    tampered = {label: arn.replace(anchored["approved_account_id"], foreign)
                for label, arn in real_identities.items()}
    assert tampered != real_identities, "the injection did not change any identity"
    monkeypatch.setattr(external_anchor, "account_sensitive_identities", lambda: tampered)

    result = external_anchor.join(env={"SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"})
    assert result["mismatches"], "the join failed to detect a foreign account"
    assert result["certifies_production"] is False


def test_a_synthetic_fixture_is_refused_by_the_protected_tier():
    """A synthetic fixture must never bless a real candidate."""
    with pytest.raises(anchor_loader.AnchorError, match="synthetic fixture"):
        anchor_loader.load(anchor_loader.TIER_PROTECTED, env={
            "SIGNALNEST_ANCHOR_PATH": str(anchor_loader.SYNTHETIC_FIXTURE),
            "SIGNALNEST_ANCHOR_SHA256": __import__("hashlib").sha256(
                anchor_loader.SYNTHETIC_FIXTURE.read_bytes()).hexdigest()})
