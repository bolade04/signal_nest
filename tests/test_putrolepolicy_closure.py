"""iam:PutRolePolicy closure classification and negative controls (Gate 4N-I16, Phases J/K).

THE DEFECT. `verify_closure.py` excluded iam:PutRolePolicy from every principal's check on
the premise that it "is applied against roles that ALREADY exist" and so "is not required of
any principal at create time". Creating an `aws_iam_role_policy` resource calls PutRolePolicy
regardless of the role's prior existence — the repository's own provider-operation map says
so — and with the exclusion in place the closure was green while no principal held it. An
ordinary Stage-A apply would have failed with AccessDenied AFTER the ECR resources existed.

The tests below check two different things, and the distinction matters:
  * that the CLASSIFICATION is derived from primary evidence and is satisfied by the
    shipped policy set;
  * that each way of getting it wrong is DETECTED — which is what makes the first claim
    worth anything.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_operator_policies as gen  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402
import putrolepolicy_classification as prp  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

EXPIRY = _ea.ACTIVE_EXPIRY_UTC


def _after_expiry() -> str:
    """One hour past the reviewed expiry, in canonical UTC.

    Anchored to the pin so a restamp cannot leave an "expired" fixture sitting inside the new
    window. The offset is literal: deriving it from the pair's own duration would move the
    fixture with the thing it is meant to fall outside of.
    """
    import datetime as _dt
    base = _dt.datetime.strptime(_ea.ACTIVE_EXPIRY_UTC, "%Y-%m-%dT%H:%M:%SZ")
    return (base + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


IN_WINDOW = {"aws:CurrentTime": "2026-07-31T12:00:00Z",
             "aws:RequestedRegion": identity.REGION,
             "iam:PermissionsBoundary": identity.BOUNDARY_POLICY_ARN}
ROLE = identity.iam_role_arn(identity.REVISION_READER_ROLE_NAMES[0])


def temp():
    return gen.bootstrap_temp_policy(EXPIRY)


# =====================================================================================
# PHASE J — the classification itself
# =====================================================================================


def test_the_classification_is_required_temporarily_not_excluded():
    result = prp.classify()
    assert result["classification"] == prp.REQUIRED_TEMPORARILY
    assert result["classification"] != prp.UNRESOLVED


def test_the_classification_is_derived_from_declared_resources_not_a_constant():
    """If the .tf files stopped declaring inline policies, the answer must change by itself."""
    declared = prp.declared_inline_policy_resources()
    assert len(declared) == 6, declared
    assert {d["name"] for d in declared} >= {"reader_publisher", "reader_execution",
                                             "reader_runner"}


def test_the_provider_operation_map_is_the_second_independent_source():
    provider = prp.provider_requires_action()
    assert provider["requires_put_role_policy"] is True
    assert prp.ACTION in provider["create_actions"]


def test_the_classification_is_satisfied_by_the_shipped_policy_set():
    result = prp.run()
    assert result["clean"], result["policy_satisfaction"]["findings"]
    assert result["policy_satisfaction"]["stage_a"] == "EXPLICIT_ALLOW"
    assert result["policy_satisfaction"]["permanent_w0"] == "EXPLICIT_DENY"


def test_the_unscopable_policy_name_is_stated_not_claimed():
    """The grant cannot be scoped to an inline-policy NAME; saying so is the honest form."""
    containment = prp.classify()["containment"]
    assert "NOT ACHIEVABLE" in containment["policy_name_scope"]


def test_no_closure_exclusion_list_names_the_action():
    """The regression guard for the defect itself."""
    text = (REPO_ROOT / "scripts" / "verify_closure.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue                       # narrative about the removed exclusion is fine
        if "iam:PutRolePolicy" in line and ("not in" in line or "!=" in line):
            # An exclusion is only acceptable if it routes to the classifier instead.
            assert "ROLE_BOOTSTRAP_NOT_AT_CREATE" in text and "prp.run()" in text, stripped


# =====================================================================================
# PHASE K — negative controls. Each is a way of getting it wrong; each must be DETECTED.
# =====================================================================================


def test_k1_operation_present_but_no_principal_allows_it_is_detected(monkeypatch):
    """The exact shipped state of Gate 4N-I15."""
    original = gen.bootstrap_temp_policy          # bind BEFORE patching, or the patched

    def denied_everywhere(expiry):                # name recurses into itself
        policy = original(expiry)
        policy["Statement"] = [s for s in policy["Statement"]
                               if s.get("Sid") != "TempInlineRolePolicyBounded"]
        return policy
    monkeypatch.setattr(gen, "bootstrap_temp_policy", denied_everywhere)
    result = prp.run()
    assert not result["clean"]
    assert any("EXPLICIT_DENY" in f or "returns" in f
               for f in result["policy_satisfaction"]["findings"])


def test_k2_the_action_removed_while_the_repository_still_declares_the_resources():
    """Evidence still says required; policy says no. The mismatch must surface."""
    declared = prp.declared_inline_policy_resources()
    assert declared, "precondition: the repository declares inline-policy resources"
    stripped = copy.deepcopy(temp())
    stripped["Statement"] = [s for s in stripped["Statement"]
                            if s.get("Sid") != "TempInlineRolePolicyBounded"]
    assert iam_eval.decide(stripped, prp.ACTION, ROLE, IN_WINDOW).decision \
        is not Decision.EXPLICIT_ALLOW


def test_k3_a_wrong_role_arn_is_not_covered_by_the_grant():
    """Scoping is to enumerated roles, so an unrelated role must not be writable."""
    outsider = f"arn:aws:iam::{identity.ACCOUNT}:role/some-unrelated-role"
    assert iam_eval.decide(temp(), prp.ACTION, outsider, IN_WINDOW).decision \
        is not Decision.EXPLICIT_ALLOW


def test_k4_a_role_outside_the_enumerated_set_but_inside_the_prefix_is_not_covered():
    """The write grant enumerates roles; it must NOT inherit the read grant's prefix."""
    prefixed = f"arn:aws:iam::{identity.ACCOUNT}:role/{identity.PREFIX}-not-a-real-role"
    assert iam_eval.decide(temp(), prp.ACTION, prefixed, IN_WINDOW).decision \
        is not Decision.EXPLICIT_ALLOW


def test_k5_an_expired_grant_does_not_authorize():
    # GATE 4N-I28R: derived from the reviewed expiry, not hand-written. As a literal this said
    # "after" while sitting BEFORE the restamped expiry, so the grant was still live and the
    # test asserted the opposite of its own name.
    after = dict(IN_WINDOW, **{"aws:CurrentTime": _after_expiry()})
    assert iam_eval.decide(temp(), prp.ACTION, ROLE, after).decision \
        is not Decision.EXPLICIT_ALLOW


def test_k6_a_request_without_the_boundary_condition_key_does_not_authorize():
    """The containment that actually holds: no boundary on the target, no write."""
    without = {k: v for k, v in IN_WINDOW.items() if k != "iam:PermissionsBoundary"}
    assert iam_eval.decide(temp(), prp.ACTION, ROLE, without).decision \
        is not Decision.EXPLICIT_ALLOW


def test_k7_a_wrong_boundary_on_the_target_role_does_not_authorize():
    wrong = dict(IN_WINDOW,
                 **{"iam:PermissionsBoundary":
                    f"arn:aws:iam::{identity.ACCOUNT}:policy/some-other-boundary"})
    assert iam_eval.decide(temp(), prp.ACTION, ROLE, wrong).decision \
        is not Decision.EXPLICIT_ALLOW


def test_k8_the_permanent_operator_must_never_hold_a_temporary_only_action():
    perm = gen.permanent_w0_policy()
    assert iam_eval.decide(perm, prp.ACTION, ROLE,
                           {"aws:RequestedRegion": identity.REGION}).decision \
        is Decision.EXPLICIT_DENY


def test_k9_the_role_bootstrap_principal_does_not_also_hold_it():
    """Exactly one owner. Two principals holding a temporary write is two windows."""
    boot = rb.role_bootstrap_policy(EXPIRY)
    assert iam_eval.decide(boot, prp.ACTION, ROLE, IN_WINDOW).decision \
        is not Decision.EXPLICIT_ALLOW


def test_k10_if_the_repository_stopped_declaring_the_resources_the_answer_becomes_obsolete(
        monkeypatch):
    """The classification must follow the evidence, not a hard-coded verdict."""
    monkeypatch.setattr(prp, "declared_inline_policy_resources", lambda: [])
    assert prp.classify()["classification"] == prp.OBSOLETE


def test_k11_disagreeing_primary_sources_produce_unknown_and_unknown_fails_the_gate(
        monkeypatch):
    """Resources declared but the provider map silent: refuse to guess."""
    monkeypatch.setattr(prp, "provider_requires_action",
                        lambda: {"entry": {}, "create_actions": [],
                                 "requires_put_role_policy": False})
    result = prp.classify()
    assert result["classification"] == prp.UNRESOLVED
    satisfaction = prp.check_policies(result)
    assert satisfaction["findings"], "UNKNOWN must be a gate failure, never an exclusion"


def test_k12_trust_bearing_authoring_is_still_denied_to_stage_a():
    """Narrowing the I9 category must not have widened it."""
    for action in ("iam:CreateRole", "iam:UpdateAssumeRolePolicy"):
        assert iam_eval.decide(temp(), action, ROLE, IN_WINDOW).decision \
            is Decision.EXPLICIT_DENY, action
