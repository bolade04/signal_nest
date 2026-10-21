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


# --- P6-CI-6 (2026-10-18): KMS keys beyond the secrets CMK are PENDING apply closures ---------
# verify_closure checks closure per resource TYPE and probes KMS with the secrets CMK, so its
# "clean" cannot see a second or third aws_kms_key. The two keys P6-CI-6 adds (the SPA and audit
# keys) can be neither created nor read by any reviewed principal until a separately authorized
# apply and a read-grant re-pin (review FREEZE-1, permissions lane F1; recorded in
# docs/operations/staging-window.md and infra/aws/README.md). These tests pin that state in
# BOTH directions: another key fails the first test until its closure is decided, and a read
# grant for the pending keys fails the second until the record is updated with it.

def _declared_w0_kms_keys(graph) -> set:
    return {(r["unit"], r["name"]) for r in graph["resources"]
            if r["type"] == "aws_kms_key" and r["unit"] not in verify_closure.W0_UNITS_EXCLUDED}


def test_every_declared_kms_key_is_the_covered_secrets_cmk_or_a_recorded_pending_key(graph):
    # P6-INF-4 (branch feat/p6-alerting-iac-01, 2026-10-20): the observability alerts key that
    # encrypts the alarm topic joins the pending set on the same terms as the P6-CI-6 keys.
    pending = {("edge", "spa"), ("observability", "audit"), ("observability", "alerts")}
    assert _declared_w0_kms_keys(graph) == {("secrets", "secrets")} | pending, (
        "an aws_kms_key was added or removed: decide its apply/read closure and update the pending "
        "record (staging-window.md, infra/aws/README.md) before changing this set")


def _reviewed_principal_policies() -> dict:
    import expiry_authorization as ea
    return {
        "permanent W0": gen.permanent_w0_policy(),
        "temporary operator": gen.bootstrap_temp_policy(ea.ACTIVE_EXPIRY_UTC),
        "window principal": gen.window_transition_effective_policy(ea.ACTIVE_EXPIRY_UTC, "api.staging.example.com"),
    }


def test_the_pending_keys_are_still_unreadable_to_every_reviewed_principal():
    import expiry_authorization as ea
    new_key = f"arn:aws:kms:{gen.REGION}:{gen.ACCOUNT}:key/22222222-2222-2222-2222-222222222222"
    policies = _reviewed_principal_policies()
    # Review FREEZE-2, permissions lane R2-3: the probe carries the tag and request keys a tag-conditioned grant would test,
    # and a time inside every principal's validity, so a CONDITIONED grant for the new keys is seen as an allow too.
    tags = {"aws:ResourceTag/Name": f"{gen.PREFIX}-spa-cmk", "aws:RequestTag/Name": f"{gen.PREFIX}-spa-cmk", "aws:TagKeys": ["Name"]}
    ctx = {"aws:RequestedRegion": gen.REGION, "aws:CurrentTime": ea.ACTIVE_ISSUANCE_UTC, **tags}
    for who, policy in policies.items():
        # positive instance, per principal: the probe sees the read grant each one holds on the secrets key
        assert iam_eval.decide(policy, "kms:DescribeKey", gen.ARN["cmk_secrets"], ctx).decision \
            is iam_eval.Decision.EXPLICIT_ALLOW, f"{who}: the probe cannot see an existing grant, so its silence proves nothing"
        for action in ("kms:DescribeKey", "kms:GetKeyPolicy", "kms:GetKeyRotationStatus", "kms:ListResourceTags",
                       "kms:CreateKey", "kms:CreateAlias", "kms:TagResource", "kms:EnableKeyRotation"):
            for resource in (new_key, "*"):
                got = iam_eval.decide(policy, action, resource, ctx).decision
                assert got is not iam_eval.Decision.EXPLICIT_ALLOW, (
                    f"{who} now allows {action} on {resource}: the P6-CI-6 apply closure is no longer pending — "
                    "update docs/operations/staging-window.md, infra/aws/README.md and this test")


def test_every_kms_allow_of_every_reviewed_principal_names_only_the_two_existing_keys():
    """Structural form of the tripwire (review FREEZE-2, permissions lane R2-3), independent of any condition: an Allow
    carrying a KMS action other than kms:ListAliases names only the state or secrets key, and no Allow carries a
    key-creation action, however it is conditioned or wildcarded."""
    import fnmatch
    allowed = {gen.ARN["cmk_state"], gen.ARN["cmk_secrets"]}
    creating = ("kms:CreateKey", "kms:CreateAlias", "kms:TagResource", "kms:EnableKeyRotation", "kms:PutKeyPolicy")
    def as_list(v):
        return v if isinstance(v, list) else [v]
    seen = 0
    for who, policy in _reviewed_principal_policies().items():
        for st in policy["Statement"]:
            if st.get("Effect") != "Allow":
                continue
            assert "NotAction" not in st and "NotResource" not in st, f"{who} {st.get('Sid')}: an Allow with NotAction/NotResource"
            actions = [str(a) for a in as_list(st.get("Action"))]
            kms = [a for a in actions if a == "*" or a.lower().startswith("kms:")]
            if not kms:
                continue
            seen += 1
            for a in kms:
                assert not any(fnmatch.fnmatchcase(c.lower(), a.lower()) for c in creating), f"{who} {st.get('Sid')}: {a} covers key creation"
            if [a for a in kms if a != "kms:ListAliases"]:
                assert set(as_list(st.get("Resource"))) <= allowed, (
                    f"{who} {st.get('Sid')}: a KMS grant on {st.get('Resource')} — the P6-CI-6 apply closure is no longer pending")
    assert seen >= 6, f"only {seen} KMS-bearing Allow statements were examined; the structure under test changed"


def test_the_pending_state_is_recorded_where_an_operator_reads_it():
    runbook = (REPO_ROOT / "docs" / "operations" / "staging-window.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "infra" / "aws" / "README.md").read_text(encoding="utf-8")
    assert "Merge and apply constraint — P6-CI-6 customer-managed keys" in runbook
    assert "pending apply closures" in readme


def test_the_audit_bucket_default_moves_to_the_audit_key_only_after_the_trail():
    """Review FREEZE-1, permissions lane F2: ordered so a failed UpdateTrail leaves SSE-S3 in place."""
    text = (REPO_ROOT / "infra" / "aws" / "modules" / "observability" / "main.tf").read_text(encoding="utf-8")
    import re
    block = text[text.index('resource "aws_s3_bucket_server_side_encryption_configuration" "audit"'):]
    block = block[:block.index("\n}\n") + 2]
    # anchored on a non-comment line (review FREEZE-2, permissions lane R2-4: a commented-out line passed a substring test)
    assert re.search(r"(?m)^\s*depends_on\s*=\s*\[\s*aws_cloudtrail\.audit\s*\]\s*$", block)
