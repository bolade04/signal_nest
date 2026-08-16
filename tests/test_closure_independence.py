"""Closure-independence and shared-omission tests (Gate 4N-I5).

Gate 4N-I4 stored the expected closure in a separate FILE, but that file and the policy
generator were set-identical hand-authored action lists sharing every resource ARN. A
SHARED OMISSION — both forgetting the same action — was structurally undetectable, and
that is exactly how `s3:ListTagsForResource` was lost.

The expectation is now COMPUTED by joining two sources that are independent by
construction:

  SOURCE 1  scripts/derive_repo_operation_graph.py   parsed from infra/aws/**/*.tf
  SOURCE 2  infra/aws/provider-api-operation-map.json  resource TYPE -> AWS action

SOURCE 2 contains no ARNs and no policy statements; SOURCE 1 contains no AWS actions at
all. Neither can express a policy, so neither can quietly agree with the generator.

Each mutation below must fail FOR ITS OWN REASON, not merely fail.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import derive_repo_operation_graph as repo_graph  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import iam_eval  # noqa: E402
import verify_closure  # noqa: E402

MAP_PATH = REPO_ROOT / "infra" / "aws" / "provider-api-operation-map.json"


@pytest.fixture(scope="module")
def graph() -> dict:
    return repo_graph.derive()


@pytest.fixture(scope="module")
def mappings() -> dict:
    return verify_closure.load_map()


# --- baseline ------------------------------------------------------------------------


def test_closure_is_clean_today():
    result = verify_closure.verify()
    assert result["clean"], result["findings"]


def test_the_two_sources_cannot_express_each_other(mappings):
    """Structural independence, not merely separate files."""
    map_text = MAP_PATH.read_text(encoding="utf-8")
    assert "arn:aws:" not in map_text, "SOURCE 2 must carry no ARNs"
    graph_src = (REPO_ROOT / "scripts" / "derive_repo_operation_graph.py").read_text(encoding="utf-8")
    for service in ("s3:", "iam:", "ec2:", "kms:"):
        assert service not in graph_src, "SOURCE 1 must carry no AWS actions"
    verifier = (REPO_ROOT / "scripts" / "verify_closure.py").read_text(encoding="utf-8")
    # INFRA-9 B-3 (architect-lane finding 1): the generator now carries TWO Allow-source
    # closures; the independence property must name both, and any future closure symbol
    # matching the generator's *_CLOSURE convention is caught by the pattern assertion.
    for closure_symbol in ("REFRESH_CLOSURE", "W0_APPLY_CLOSURE"):
        assert closure_symbol not in verifier, (
            "the verifier must never use the generator's action list as its authority"
        )
    import re as _re
    assert not _re.search(r"gen_operator_policies\.\w*_CLOSURE", verifier), (
        "no gen_operator_policies closure symbol may appear in the verifier"
    )


# --- Phase L: six mutation tests, each failing for its own reason --------------------


def test_M1_removing_a_repository_resource_operation_changes_the_requirement(graph, mappings):
    """1. remove one repository resource operation -> its reads leave the closure."""
    reduced = copy.deepcopy(graph)
    reduced["resources"] = [r for r in reduced["resources"] if r["type"] != "aws_budgets_budget"]
    reduced["distinct_resource_types"] = sorted({r["type"] for r in reduced["resources"]})
    joined = verify_closure.join(reduced, mappings)
    assert "budgets:ViewBudget" not in joined["required_read_actions"]
    assert "aws_budgets_budget" in joined["dead_mappings"], (
        "a mapping with no declared resource must surface as a dead mapping"
    )


def test_M2_removing_a_provider_api_mapping_is_detected_as_unmapped(graph, mappings):
    """2. remove one provider API mapping -> C1 unmapped-resource-type failure."""
    reduced = {k: v for k, v in mappings.items() if k != "aws_cloudtrail"}
    joined = verify_closure.join(graph, reduced)
    assert "aws_cloudtrail" in joined["unmapped_resource_types"]


def test_M3_removing_a_generated_policy_action_is_detected(graph, mappings):
    """3. remove one generated policy action -> C3 unauthorized failure."""
    joined = verify_closure.join(graph, mappings)
    assert "budgets:ViewBudget" in joined["required_read_actions"]
    broken = copy.deepcopy(gen.permanent_w0_policy())
    for stmt in broken["Statement"]:
        if stmt.get("Sid") == "BudgetsRead":
            stmt["Action"] = [a for a in stmt["Action"] if a != "budgets:ViewBudget"]
    res = f"arn:aws:budgets::{gen.ACCOUNT}:budget/{gen.PREFIX}-monthly"
    assert iam_eval.decide(broken, "budgets:ViewBudget", res,
                           {"aws:RequestedRegion": gen.REGION}).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


def test_M4_altering_a_generated_ARN_is_detected():
    """4. alter one generated ARN -> the probe, chosen from SOURCE 1/2, stops matching."""
    broken = copy.deepcopy(gen.permanent_w0_policy())
    for stmt in broken["Statement"]:
        if stmt.get("Sid") == "RdsReadExact":
            stmt["Resource"] = [a.replace("signalnest-staging-pg-params", "WRONG") for a in stmt["Resource"]]
    assert iam_eval.decide(broken, "rds:DescribeDBParameters", gen.ARN["pg"],
                           {"aws:RequestedRegion": gen.REGION}).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW


def test_M5_a_repository_resource_without_a_mapping_fails(graph, mappings):
    """5. add a repository resource without a mapping -> C1."""
    widened = copy.deepcopy(graph)
    widened["resources"].append({"type": "aws_sqs_queue", "name": "new", "unit": "ecs",
                                 "file": "infra/aws/modules/ecs/main.tf", "line": 1,
                                 "conditional": False, "count_expression": None,
                                 "declares_tags": True})
    widened["distinct_resource_types"] = sorted({r["type"] for r in widened["resources"]})
    joined = verify_closure.join(widened, mappings)
    assert "aws_sqs_queue" in joined["unmapped_resource_types"]


def test_M6_a_mapping_without_a_repository_resource_fails(graph, mappings):
    """6. add a mapping with no repository resource -> C2 dead mapping."""
    widened = dict(mappings)
    widened["aws_lambda_function"] = {"read": ["lambda:GetFunction"], "unit": "ecs"}
    joined = verify_closure.join(graph, widened)
    assert "aws_lambda_function" in joined["dead_mappings"]


# --- Phase M: shared-omission detection ---------------------------------------------


def test_shared_omission_is_detected_even_when_BOTH_policy_files_omit_it(graph, mappings):
    """THE Gate 4N-I4 FAILURE MODE.

    Simulate both hand-authored sides forgetting the same action. The join still
    requires it, because the requirement comes from the repository graph and the
    resource-type map — neither of which the policy author edits when writing a policy.
    """
    joined = verify_closure.join(graph, mappings)
    action = "s3:ListTagsForResource"
    assert action in joined["required_read_actions"], (
        "the join must require it independently of any policy file"
    )
    # Now remove it from the generated policy AND from the hand-authored contract.
    broken = copy.deepcopy(gen.permanent_w0_policy())
    for stmt in broken["Statement"]:
        if isinstance(stmt.get("Action"), list):
            stmt["Action"] = [a for a in stmt["Action"] if a != action]
    assert iam_eval.decide(broken, action, gen.ARN["audit_bucket"],
                           {"aws:RequestedRegion": gen.REGION}).decision \
        is not iam_eval.Decision.EXPLICIT_ALLOW, (
        "with both hand-authored sides silent, the JOIN is what still catches it"
    )


def test_every_declared_resource_type_has_a_mapping(graph, mappings):
    unmapped = sorted(set(graph["distinct_resource_types"]) - set(mappings))
    assert not unmapped, f"unmapped resource types: {unmapped}"


def test_every_mapping_has_a_declared_resource(graph, mappings):
    dead = sorted(set(mappings) - set(graph["distinct_resource_types"]))
    assert not dead, f"mappings with no declared resource: {dead}"


def test_dark_resources_are_classified_not_silently_dropped(mappings):
    """Gated-off stages must be recorded as deferred, so the omission stays visible."""
    dark = {k for k, v in mappings.items() if v.get("dark")}
    assert dark, "the dark workload/reader stages must be explicitly classified"
    joined = verify_closure.verify()
    assert joined["deferred_dark_actions"], "deferred actions must be reported, not dropped"
    for action in joined["deferred_dark_actions"]:
        assert action not in joined["required_read_actions"], (
            "a deferred action must not be silently counted as currently required"
        )


def test_historical_denials_are_joined_against_the_closure():
    result = verify_closure.verify()
    assert not [f for f in result["findings"] if f.startswith("C5")], result["findings"]


def test_verifier_exits_nonzero_on_divergence(monkeypatch, mappings):
    """The check must be usable as a fail-closed CI gate, not just a report."""
    reduced = {k: v for k, v in mappings.items() if k != "aws_cloudtrail"}
    monkeypatch.setattr(verify_closure, "load_map", lambda: reduced)
    result = verify_closure.verify()
    assert not result["clean"]
    assert any(f.startswith("C1") for f in result["findings"])
