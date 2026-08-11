"""Boundary-ARN unification and cross-construction (Gate 4N-I7, Defect 1).

THE DEFECT. Gate 4N-I6 built the boundary ARN independently in the boundary generator and
in the temporary-operator generator. Falsifying one of them left 539/539 tests green,
because every test handed the falsified value straight back as its own request context —
the wrong construction validated itself. That is the Gate 4N-I2 impossible-ARN defect
reproduced by duplication.

Two separate obligations are discharged here.

  UNIFICATION   exactly one construction site exists, and every consumer reads it.
  CROSS-CHECK   the value that site produces is INDEPENDENTLY recomputed from sources
                that are not the module under test, and compared.

The independent recomputation deliberately takes nothing from scripts/signalnest_identity.py:

  partition, account   the live-captured CloudTrail ARN in the tier-resolved inventory
  boundary policy name the ARN literal declared in the module's own OpenTofu test fixture,
                       infra/aws/modules/revision_reader/reader_contract.tftest.hcl
  ARN assembly         the AWS managed-policy ARN rule, applied here

If any consumer drifts, or if the single source is falsified, the recomputation and the
consumers disagree and these tests fail. The mutation tests at the bottom prove that.

No AWS access, no network.
"""

from __future__ import annotations

import expiry_authorization as _ea  # noqa: E402

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_bootstrap_operator_policy as boot  # noqa: E402
import gen_boundary_policy as gb  # noqa: E402
import gen_boundary_rollout as rollout  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import resource_oracle  # noqa: E402
import signalnest_identity as identity  # noqa: E402

# GATE 4N-I18, SEC-1: SOURCE B is the tier-resolved inventory, never a repository path.
import protected_inventory  # noqa: E402
TFTEST = REPO_ROOT / "infra/aws/modules/revision_reader/reader_contract.tftest.hcl"


# --- the independent recomputation ---------------------------------------------------


def partition_and_account_from_live_capture() -> tuple[str, str]:
    """SOURCE B. Read them out of an ARN AWS itself returned."""
    inv = protected_inventory.load().data
    trail_arn = inv["trails"][0][1]
    parts = trail_arn.split(":")
    assert parts[0] == "arn" and len(parts) >= 6, f"malformed live ARN {trail_arn!r}"
    partition, account = parts[1], parts[4]
    assert re.fullmatch(r"\d{12}", account), f"not a 12-digit account: {account!r}"
    # Corroborate against a second, unrelated live ARN so a single bad row cannot set it.
    db_arn = inv["db"][0][1]
    assert db_arn.split(":")[4] == account, "live ARNs disagree on the account"
    assert db_arn.split(":")[1] == partition, "live ARNs disagree on the partition"
    return partition, account


def boundary_name_from_module_test_fixture() -> str:
    """SOURCE A. The module's own test fixture declares the boundary ARN literally.

    Its account segment is the AWS documentation placeholder 111122223333, so the fixture
    can supply the NAME without supplying the account — which is exactly the property that
    makes it usable as an independent source here.
    """
    text = TFTEST.read_text(encoding="utf-8")
    literals = set(re.findall(
        r'role_permissions_boundary_arn\s*=\s*"(arn:[^"]+)"', text))
    assert literals, "the module test fixture no longer declares a boundary ARN"
    assert len(literals) == 1, f"fixture declares conflicting boundary ARNs: {literals}"
    fixture_arn = literals.pop()
    assert ":111122223333:" in fixture_arn, (
        "the fixture must keep the documentation placeholder account — using the real "
        "account would make this source non-independent")
    return fixture_arn.split(":policy/", 1)[1]


def expected_boundary_arn() -> str:
    """Apply the AWS managed-policy ARN rule. Not imported from the source under test."""
    partition, account = partition_and_account_from_live_capture()
    name = boundary_name_from_module_test_fixture()
    # arn:<partition>:iam::<account>:policy/<path-without-leading-slash><name>
    # IAM is global, so the region segment is empty. The default path "/" contributes
    # nothing to the ARN.
    return f"arn:{partition}:iam::{account}:policy/{name}"


# --- consumers ------------------------------------------------------------------------


def _condition_values(doc: dict, key: str) -> list[str]:
    out = []
    for statement in doc["Statement"]:
        for operator, pairs in (statement.get("Condition") or {}).items():
            for context_key, value in pairs.items():
                if context_key.lower() == key.lower():
                    out.extend(value if isinstance(value, list) else [value])
    return out


def consumers() -> dict[str, str]:
    """Every place in the repository that must speak the same boundary ARN."""
    bootstrap = boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC)
    lifecycle = next(s for s in bootstrap["Statement"]
                     if s["Sid"] == "BoundaryPolicyLifecycle")
    oracle_rows = {row["key"]: row for row in resource_oracle.compare()["rows"]}
    return {
        "identity.BOUNDARY_POLICY_ARN": identity.BOUNDARY_POLICY_ARN,
        "identity.identity_summary()": identity.identity_summary()["boundary_policy_arn"],
        "gen_boundary_policy.POLICY_ARN": gb.POLICY_ARN,
        "gen_operator_policies.ARN['boundary']": gen.ARN["boundary"],
        # GATE 4N-I9: the Stage-A operator no longer carries an iam:PermissionsBoundary
        # condition, because it no longer creates roles at all. The role bootstrap operator
        # is the consumer of that condition now.
        "role_bootstrap_policy iam:PermissionsBoundary condition":
            _condition_values(rb.role_bootstrap_policy(_ea.ACTIVE_EXPIRY_UTC),
                              "iam:PermissionsBoundary")[0],
        "bootstrap_operator_policy lifecycle Resource": lifecycle["Resource"],
        "bootstrap_operator_policy iam:PermissionsBoundary condition":
            _condition_values(bootstrap, "iam:PermissionsBoundary")[0],
        "resource_oracle expected": oracle_rows["iam:boundary_policy"]["expected"],
        "gen_boundary_rollout graph": rollout.evaluate()["boundary_policy_arn"],
    }


CONSUMERS = consumers()


def test_at_least_seven_independent_consumers_are_covered():
    """A shrinking consumer set would weaken every assertion below without failing one."""
    assert len(CONSUMERS) >= 7, sorted(CONSUMERS)


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_every_consumer_matches_the_independently_computed_arn(name):
    assert CONSUMERS[name] == expected_boundary_arn(), (
        f"{name} disagrees with the independent construction")


def test_all_consumers_agree_with_each_other():
    distinct = set(CONSUMERS.values())
    assert len(distinct) == 1, f"consumers disagree: {json.dumps(CONSUMERS, indent=2)}"


def test_the_independent_sources_are_not_the_module_under_test():
    """Guards the property that makes the cross-check meaningful."""
    source = Path(__file__).read_text(encoding="utf-8")
    body = source.split("# --- consumers", 1)[0]
    assert "identity.BOUNDARY_POLICY_ARN" not in body
    assert "identity.BOUNDARY_POLICY_NAME" not in body
    assert "identity.ACCOUNT" not in body
    assert "identity.PARTITION" not in body


def test_only_one_construction_site_exists_in_the_repository():
    """Defect 1 was duplication. This fails if a second construction reappears."""
    offenders = []
    pattern = re.compile(r'f?"[^"\n]*:policy/[^"\n]*role-boundary')
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        if path.name in ("signalnest_identity.py", "resource_oracle.py"):
            continue  # the single source, and the deliberately independent oracle
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line) or re.search(r'f"[^"\n]*-role-boundary"', line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "boundary ARN/name rebuilt outside the single source:\n" + \
        "\n".join(offenders)


def test_the_oracle_derives_the_name_without_importing_it():
    """The oracle must stay an independent witness, not a mirror."""
    source = (REPO_ROOT / "scripts/resource_oracle.py").read_text(encoding="utf-8")
    assert "BOUNDARY_POLICY_ARN" not in source
    assert "BOUNDARY_POLICY_NAME" not in source
    assert "role-boundary" in source, "the oracle must still derive the name itself"


def test_the_iam_policy_arn_rule_handles_a_non_default_path():
    """The path rule is the part of the ARN construction that is easy to get wrong."""
    assert identity.iam_policy_arn("p", path="/", account="0" * 12) == \
        "arn:aws:iam::000000000000:policy/p"
    assert identity.iam_policy_arn("p", path="/team/sub/", account="0" * 12) == \
        "arn:aws:iam::000000000000:policy/team/sub/p"
    for bad in ("team/", "/team", "team"):
        with pytest.raises(ValueError):
            identity.iam_policy_arn("p", path=bad)


# --- mutation proof: falsifying the single source must be CAUGHT ---------------------

MUTATIONS = {
    "wrong_account": lambda arn: arn.replace(identity.ACCOUNT, "999988887777"),
    "wrong_partition": lambda arn: arn.replace("arn:aws:", "arn:aws-us-gov:"),
    "wrong_name": lambda arn: arn.replace("role-boundary", "role-boundry"),
    "spurious_path": lambda arn: arn.replace(":policy/", ":policy/boundaries/"),
    "role_not_policy": lambda arn: arn.replace(":policy/", ":role/"),
    "region_populated": lambda arn: arn.replace("iam::", f"iam:{identity.REGION}:"),
    "truncated": lambda arn: arn.rsplit("-", 1)[0],
}


@pytest.mark.parametrize("label", sorted(MUTATIONS))
def test_falsifying_the_single_source_is_caught_by_the_cross_check(label, monkeypatch):
    """The Gate 4N-I6 failure mode, executed deliberately.

    The mutation is applied to the AUTHORITATIVE value and propagated to every consumer,
    exactly as a real edit to signalnest_identity.py would propagate. A suite that only
    compares consumers to each other stays green here; the independent recomputation is
    what fails.
    """
    falsified = MUTATIONS[label](identity.BOUNDARY_POLICY_ARN)
    assert falsified != identity.BOUNDARY_POLICY_ARN, f"mutation {label} was a no-op"

    monkeypatch.setattr(identity, "BOUNDARY_POLICY_ARN", falsified)
    monkeypatch.setattr(gb, "POLICY_ARN", falsified)
    monkeypatch.setitem(gen.ARN, "boundary", falsified)
    monkeypatch.setattr(boot, "BOUNDARY_POLICY_ARN", falsified)

    propagated = {
        "identity": identity.BOUNDARY_POLICY_ARN,
        "boundary_generator": gb.POLICY_ARN,
        "operator_generator": gen.ARN["boundary"],
        "bootstrap lifecycle Resource": next(
            s for s in boot.bootstrap_operator_policy(_ea.ACTIVE_EXPIRY_UTC)["Statement"]
            if s["Sid"] == "BoundaryPolicyLifecycle")["Resource"],
    }
    assert len(set(propagated.values())) == 1, "the mutation failed to propagate"

    expected = expected_boundary_arn()
    for label_, value in propagated.items():
        assert value != expected, (
            f"{label_} still matched the independent construction after mutation {label} "
            "— the cross-check is not load-bearing")


def test_the_recomputation_is_stable_across_calls():
    assert expected_boundary_arn() == expected_boundary_arn()
