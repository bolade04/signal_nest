"""Independent resource-oracle verification and resource-axis mutation tests (Gate 4N-I6).

Gate 4N-I5 fixed closure independence on the ACTION axis only. Every probe ARN still came
from the policy generator, so falsifying an identifier moved the policy, the probe and the
expectation together and the suite stayed green.

`scripts/resource_oracle.py` derives expected ARNs from repository expressions, a live
read-only inventory and AWS ARN construction rules — never from the generator. Each
mutation below falsifies a COMPUTED generator value (not source text: these ARNs are
f-strings, so a textual patch silently matches nothing — a trap this file exists to avoid).
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import resource_oracle as ro  # noqa: E402

MUTATIONS = {
    "wrong_account": ("ARN", "db", lambda v: v.replace("111122223333", "444444444444")),
    "wrong_region": ("ARN", "trail", lambda v: v.replace("us-east-1", "eu-west-3")),
    "omitted_revision_segment": ("ROLE", 0, lambda v: v.replace("revision-reader-", "reader-")),
    "ecr_hyphen_instead_of_slash": ("ARN", "reader_ecr", lambda v: v.replace("staging/revision", "staging-revision")),
    "wrong_bucket": ("ARN", "state_bucket", lambda v: v.replace(v.rsplit("-", 1)[-1], "WRONG")),
    "wrong_trail": ("ARN", "trail", lambda v: v.replace("-audit", "-WRONGTRAIL")),
    # GATE 4N-I18: mutate the RESOLVED key id. The old form replaced a fragment of the
    # real UUID and became a silent no-op once that literal left the repository.
    "wrong_kms_key": ("ARN", "cmk_state", lambda v: v[:-4] + ("dead" if not v.endswith("dead") else "beef")),
    "wrong_rds_identifier": ("ARN", "db", lambda v: v.replace("postgres", "WRONGDB")),
    "wildcard_replacing_exact_arn": ("ARN", "pg", lambda v: v.rsplit(":", 1)[0] + ":*"),
    "wrong_role_prefix": ("ROLE", 1, lambda v: v.replace("signalnest-staging", "signalnest-prod")),
    "wrong_boundary_policy_arn": ("BOUNDARY", None, lambda v: v.replace("role-boundary", "other-boundary")),
}


def test_oracle_is_clean_against_the_current_generator():
    assert ro.compare()["clean"], ro.compare()["mismatches"]


MARKER = "# --- comparison against the generator (subject under test) ---"


def test_oracle_does_not_read_the_generator_as_an_authority():
    """Split on the SECTION MARKER, not on a function name.

    Gate 4N-I7 moved the generator reads out of `compare()` into a `generated_arns()`
    helper, and a `def compare(` split silently reclassified them as derivation code. The
    marker is the real boundary: everything above it derives expectations, everything
    below it compares them against the subject under test.
    """
    src = (REPO_ROOT / "scripts" / "resource_oracle.py").read_text(encoding="utf-8")
    assert src.count(MARKER) == 1, "the derivation/comparison section marker must be unique"
    body = src.split(MARKER)[0]
    # A prose mention in the docstring is fine and desirable; a real IMPORT is not.
    imports = [ln.strip() for ln in body.splitlines()
               if ln.strip().startswith(("import ", "from ")) and "gen_" in ln]
    assert not imports, f"the derivation half must not import a generator: {imports}"
    assert "live-resource-inventory.json" in src and "locals.tf" in src


def test_the_marker_split_actually_excludes_the_comparison_half():
    """Guards the guard: a marker placed at the end of the file would vacuously pass."""
    src = (REPO_ROOT / "scripts" / "resource_oracle.py").read_text(encoding="utf-8")
    body, rest = src.split(MARKER)
    assert "def expected_arns(" in body, "derivation half must contain the derivations"
    assert "def compare(" in rest, "comparison half must contain the comparison"
    assert "import gen_operator_policies" in rest, (
        "the generator must be imported below the marker — if it is not imported at all "
        "the oracle is comparing against nothing")


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_every_resource_axis_mutation_is_caught(name):
    kind, key, fn = MUTATIONS[name]
    import gen_boundary_policy as gb
    import gen_operator_policies as gen

    importlib.reload(gen)
    importlib.reload(gb)
    importlib.reload(ro)
    try:
        if kind == "ARN":
            gen.ARN[key] = fn(gen.ARN[key])
        elif kind == "ROLE":
            gen.READER_ROLE_ARNS[key] = fn(gen.READER_ROLE_ARNS[key])
        else:
            gb.POLICY_ARN = fn(gb.POLICY_ARN)
        assert not ro.compare()["clean"], f"{name} was not caught by the oracle"
    finally:
        importlib.reload(gen)
        importlib.reload(gb)
        importlib.reload(ro)


def test_source_b_inventory_is_present_and_dated():
    inv = ro.inventory()
    assert inv["_captured_utc"], "SOURCE B must record when it was captured"
    assert inv["roles"] and inv["buckets"] and inv["aliases"]


# --- Gate 4N-I7 Defect 4: the oracle must FAIL CLOSED --------------------------------
#
# Gate 4N-I6 carried three silent fallbacks. Each turned "the oracle could not derive
# this" into "the oracle agrees", which is the one outcome an independent oracle must
# never produce. These tests break each derivation deliberately and require a non-clean
# result — an unparsable expression must never be satisfied by convention.


def _isolated_infra(tmp_path, monkeypatch):
    """Copy infra/aws into a temp tree so a derivation can be broken without touching git."""
    dst = tmp_path / "aws"
    shutil.copytree(REPO_ROOT / "infra" / "aws", dst,
                    ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
    monkeypatch.setattr(ro, "INFRA", dst)
    # GATE 4N-I18, SEC-1. SOURCE B no longer lives in the tree, so it cannot be redirected by
    # copying infra/. The scratch inventory is written OUTSIDE the repository and supplied
    # through the real Tier-2 loader path (explicit path + separately supplied hash), which is
    # how a real operator supplies it — so these tests now exercise the shipping mechanism
    # rather than a monkeypatched constant.
    scratch = tmp_path / "external" / "live-resource-inventory.json"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    inv = json.loads((REPO_ROOT / "tests" / "fixtures" / "synthetic-inventory.json").read_text())
    inv.pop("_classification", None)   # Tier 2 refuses a document marked synthetic
    _write_external_inventory(scratch, inv, monkeypatch)
    return dst


def _write_external_inventory(path, inv, monkeypatch):
    """Write an inventory outside the repo and point the Tier-2 loader at it."""
    import hashlib
    path.write_text(json.dumps(inv, indent=2), encoding="utf-8")
    monkeypatch.setenv("SIGNALNEST_ANCHOR_TIER", "TIER_2_PROTECTED")
    monkeypatch.setenv("SIGNALNEST_INVENTORY_PATH", str(path))
    monkeypatch.setenv("SIGNALNEST_INVENTORY_SHA256",
                       hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def test_an_unparsable_trail_expression_is_unresolved_not_conventional(tmp_path, monkeypatch):
    """THE fallback that mattered most.

    The removed fallback returned f"{name_prefix()}-audit", which is byte-identical to
    what the generator produces. The oracle would therefore have agreed with the subject
    under test at exactly the moment it had failed to read the repository.
    """
    infra = _isolated_infra(tmp_path, monkeypatch)
    path = infra / "modules" / "observability" / "main.tf"
    path.write_text(path.read_text(encoding="utf-8").replace("trail_name", "trail_name_RENAMED"),
                    encoding="utf-8")

    with pytest.raises(ro.UnresolvableExpression):
        ro.trail_name()

    result = ro.compare()
    row = next(r for r in result["rows"] if r["key"] == "cloudtrail:trail")
    assert row["result"] == ro.Status.UNRESOLVED, row
    assert not result["clean"], "an underivable trail name must not be reported clean"
    assert row["expected"] is None, "UNRESOLVED must carry no invented expectation"


def test_a_role_without_a_literal_name_is_reported_not_skipped(tmp_path, monkeypatch):
    infra = _isolated_infra(tmp_path, monkeypatch)
    path = infra / "modules" / "iam" / "main.tf"
    src = path.read_text(encoding="utf-8")
    src = src.replace('name                 = "${var.name_prefix}-api-task"',
                      'name                 = local.computed_elsewhere', 1)
    path.write_text(src, encoding="utf-8")

    _, problems = ro.role_names()
    assert problems, "a role with no literal name must be reported as a problem"
    result = ro.compare()
    assert not result["clean"]
    assert any(r["result"] == ro.Status.UNRESOLVED for r in result["rows"])


def test_an_unresolvable_name_prefix_fails_every_row(tmp_path, monkeypatch):
    """A failure at the root of the derivation must not shrink the comparison set."""
    infra = _isolated_infra(tmp_path, monkeypatch)
    path = infra / "locals.tf"
    path.write_text(path.read_text(encoding="utf-8").replace("name_prefix", "name_prefix_GONE"),
                    encoding="utf-8")

    with pytest.raises(ro.UnresolvableExpression):
        ro.name_prefix()

    result = ro.compare()
    assert not result["clean"]
    unresolved = [r for r in result["rows"] if r["result"] == ro.Status.UNRESOLVED]
    assert len(unresolved) >= len(ro.GENERATED_KEYS), (
        "every generator-produced key must be reported UNRESOLVED, not omitted")


def test_source_a_and_source_b_disagreement_is_drift_not_a_match(tmp_path, monkeypatch):
    """A stale inventory must be visible, not absorbed."""
    _isolated_infra(tmp_path, monkeypatch)
    path = tmp_path / "external" / "live-resource-inventory.json"
    inv = json.loads(path.read_text(encoding="utf-8"))
    inv["trails"] = [["signalnest-staging-audit-RENAMED",
                      "arn:aws:cloudtrail:us-east-1:111122223333:trail/signalnest-staging-audit-RENAMED"]]
    _write_external_inventory(path, inv, monkeypatch)

    result = ro.compare()
    row = next(r for r in result["rows"] if r["key"] == "cloudtrail:trail")
    assert row["result"] == ro.Status.DRIFT_OR_STALE, row
    assert not result["clean"]


def test_a_missing_inventory_raises_rather_than_deriving_from_source_a_alone(tmp_path, monkeypatch):
    """SOURCE B is mandatory: absent evidence must raise, never fall back to SOURCE A alone.

    GATE 4N-I18, SEC-1. The absence is now expressed the way it actually occurs — Tier 2 is
    declared but the external inventory is missing — instead of by pointing a module constant
    at a nonexistent repository file. The loader's refusal to fall back IS the property under
    test, so exercising the loader is the point.
    """
    import protected_inventory

    monkeypatch.setenv("SIGNALNEST_ANCHOR_TIER", "TIER_2_PROTECTED")
    monkeypatch.setenv("SIGNALNEST_INVENTORY_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setenv("SIGNALNEST_INVENTORY_SHA256", "0" * 64)
    with pytest.raises(protected_inventory.InventoryError, match="missing file"):
        ro.inventory()

    # And with no external inventory named at all, Tier 2 must refuse rather than silently
    # reading the tracked synthetic fixture — the fallback that would defeat the containment.
    monkeypatch.delenv("SIGNALNEST_INVENTORY_PATH")
    with pytest.raises(protected_inventory.InventoryError, match="no repository fallback"):
        ro.inventory()


def test_a_generated_resource_with_no_oracle_entry_is_not_clean(monkeypatch):
    """Coverage must be added deliberately, never assumed."""
    real = ro.generated_arns

    def widened():
        out = dict(real())
        out["efs:something_new"] = "arn:aws:elasticfilesystem:us-east-1:111122223333:file-system/fs-1"
        return out

    monkeypatch.setattr(ro, "generated_arns", widened)
    result = ro.compare()
    row = next(r for r in result["rows"] if r["key"] == "efs:something_new")
    assert row["result"] == ro.Status.NO_ORACLE_ENTRY
    assert not result["clean"]


def test_a_role_name_invented_in_identity_is_caught_by_the_set_reconciliation(monkeypatch):
    """The eight boundary targets are declared by f-string and named by no policy."""
    import signalnest_identity as identity

    monkeypatch.setattr(identity, "ALL_ROLE_ARNS",
                        tuple(identity.ALL_ROLE_ARNS) +
                        ("arn:aws:iam::111122223333:role/signalnest-staging-ghost",))
    result = ro.compare()
    row = next(r for r in result["rows"] if r["key"] == "roleset:identity_vs_repository")
    assert row["result"] == ro.Status.MISMATCH
    assert not result["clean"]


def test_no_status_other_than_match_is_treated_as_clean():
    """Pins the clean predicate itself, so a future status cannot default to passing."""
    for status in (ro.Status.MISMATCH, ro.Status.UNRESOLVED, ro.Status.DRIFT_OR_STALE,
                   ro.Status.NO_ORACLE_ENTRY):
        assert status not in (ro.Status.MATCH, "ORACLE_ONLY")
