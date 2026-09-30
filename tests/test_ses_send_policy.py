"""6B-4C (P6-AUTH-2, founder decision FD-1) — the API task role's SES send grant.

WHAT IS UNDER TEST. `infra/aws/modules/iam/main.tf` declares `aws_iam_role_policy.api_ses_send`
on the API task role: `ses:SendEmail` on exactly ONE identity ARN (the sending domain),
conditioned on the ONE bare From address and the SES v2 API. OpenTofu renders that policy
offline in `ses_send_scope.tftest.hcl`, which asserts the rendering equals the tracked
fixture `infra/aws/modules/iam/fixtures/api-ses-send-policy.expected.json` as decoded-JSON equality (the
fixture's formatting is not compared). This module takes
THAT fixture and evaluates its semantics with the repository's independent IAM evaluator
(`scripts/iam_eval.py`), so the semantics below are proven against what OpenTofu really
renders, not against a hand-copied restatement.

WHY BOTH SIDES. A test that only asserts the Allow fires proves nothing about scope: the
load-bearing direction is that every widening — another action, another identity, another
From address, a display-name form, the v1 API, a missing context key — is NOT allowed. The
binding is proven a third way, through the `.tf` parser in `scripts/terraform_role_inventory.py`
(the same parser the widening-ceiling gate trusts): the policy binds the API task role and
nothing else, because a mocked provider cannot distinguish role ids.

All values are synthetic documentation values (placeholder account, example domain); no real
identifier appears here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import iam_eval
import terraform_role_inventory as tf_roles

REPO_ROOT = Path(__file__).resolve().parents[1]
# The rendering pin lives beside the tftest (NOT under tests/fixtures/: the site-coverage gate
# treats every JSON key there as a requirement-key site of the CI guard suite, which this
# file is not — it is the OpenTofu rendering pin).
FIXTURE = REPO_ROOT / "infra" / "aws" / "modules" / "iam" / "fixtures" / "api-ses-send-policy.expected.json"

IDENTITY = "arn:aws:ses:us-east-1:111122223333:identity/mail.staging.example.com"
OTHER_IDENTITY = "arn:aws:ses:us-east-1:111122223333:identity/other.example.com"
APEX_IDENTITY = "arn:aws:ses:us-east-1:111122223333:identity/example.com"
FROM = "no-reply@mail.staging.example.com"
CONTEXT = {"ses:FromAddress": FROM, "ses:ApiVersion": "2"}


def policy() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# =====================================================================================
# Structure — the fixture is exactly the reviewed shape
# =====================================================================================

def test_fixture_is_one_allow_with_one_action_one_resource_two_condition_keys():
    doc = policy()
    assert doc["Version"] == "2012-10-17"
    assert len(doc["Statement"]) == 1
    stmt = doc["Statement"][0]
    assert stmt["Sid"] == "SesV2SendFromApprovedIdentity"
    assert stmt["Effect"] == "Allow"
    assert stmt["Action"] == ["ses:SendEmail"]
    assert stmt["Resource"] == IDENTITY
    assert "*" not in stmt["Resource"]
    assert list(stmt["Condition"]) == ["StringEquals"]
    assert stmt["Condition"]["StringEquals"] == {"ses:FromAddress": FROM, "ses:ApiVersion": "2"}
    assert "ses:FromDisplayName" not in stmt["Condition"]["StringEquals"], (
        "the display name is a separate decision and must not be conditioned here")


def test_from_address_is_at_the_identity_domain():
    """A From address outside the identity domain could never satisfy both the Resource and
    the ses:FromAddress condition; the module's precondition refuses it and the fixture must
    agree with itself."""
    stmt = policy()["Statement"][0]
    domain = stmt["Resource"].rsplit(":identity/", 1)[1]
    assert stmt["Condition"]["StringEquals"]["ses:FromAddress"].endswith("@" + domain)


def test_fixture_has_no_structural_defect_under_the_repository_validator():
    assert iam_eval.validate_policy(policy(), "identity") == []


# =====================================================================================
# Semantics — the Allow fires for exactly the intended request and for nothing wider
# =====================================================================================

def test_send_from_the_approved_identity_and_from_address_over_v2_is_explicitly_allowed():
    result = iam_eval.decide(policy(), "ses:SendEmail", IDENTITY, CONTEXT)
    assert result.decision == iam_eval.Decision.EXPLICIT_ALLOW, result
    assert result.matching_allow_sids == ("SesV2SendFromApprovedIdentity",)


@pytest.mark.parametrize(
    "action, resource, context, why",
    [
        ("ses:SendRawEmail", IDENTITY, CONTEXT, "SendRawEmail is unused by the client and not granted"),
        ("ses:SendBulkEmail", IDENTITY, CONTEXT, "bulk sending is not granted"),
        ("ses:CreateEmailIdentity", IDENTITY, CONTEXT, "no administrative SES action is granted"),
        ("ses:SendEmail", OTHER_IDENTITY, CONTEXT, "another verified identity is not granted"),
        ("ses:SendEmail", APEX_IDENTITY, CONTEXT, "the apex domain identity is not granted"),
        ("ses:SendEmail", IDENTITY, {**CONTEXT, "ses:FromAddress": "security@mail.staging.example.com"},
         "another local-part at the sending domain is refused by ses:FromAddress"),
        ("ses:SendEmail", IDENTITY, {**CONTEXT, "ses:FromAddress": "no-reply@other.example.com"},
         "the same local-part at another domain is refused"),
        ("ses:SendEmail", IDENTITY, {**CONTEXT, "ses:FromAddress": "SignalNest <no-reply@mail.staging.example.com>"},
         "a display-name form is not the bare address the condition names (evaluator semantics; "
         "that SES itself populates ses:FromAddress with the bare address is confirmed only by the "
         "authorized live smoke send — E8 plan P6/S7)"),
        ("ses:SendEmail", IDENTITY, {**CONTEXT, "ses:FromAddress": "No-Reply@mail.staging.example.com"},
         "StringEquals is case-sensitive; the application sends the exact lowercase address"),
        ("ses:SendEmail", IDENTITY, {**CONTEXT, "ses:ApiVersion": "1"},
         "the SES v1 API is not granted"),
    ],
)
def test_every_widening_is_not_allowed(action, resource, context, why):
    result = iam_eval.decide(policy(), action, resource, context)
    assert result.decision != iam_eval.Decision.EXPLICIT_ALLOW, (why, result)
    assert result.matching_allow_sids == (), (why, result)
    assert result.decision == iam_eval.Decision.IMPLICIT_DENY, (why, result)


@pytest.mark.parametrize("missing", ["ses:FromAddress", "ses:ApiVersion"])
def test_a_request_missing_a_conditioned_key_is_not_allowed(missing):
    context = {k: v for k, v in CONTEXT.items() if k != missing}
    result = iam_eval.decide(policy(), "ses:SendEmail", IDENTITY, context)
    # The evaluator fails CLOSED on an absent single-valued key: depending on which key is
    # absent it reports MISSING_CONTEXT or IMPLICIT_DENY. Either way the Allow never fires.
    assert result.decision in (iam_eval.Decision.MISSING_CONTEXT, iam_eval.Decision.IMPLICIT_DENY), result
    assert result.decision != iam_eval.Decision.EXPLICIT_ALLOW
    assert result.matching_allow_sids == ()


# =====================================================================================
# Mutation controls — the validator and evaluator would notice the widenings a review
# might miss
# =====================================================================================

# NOTE (deferred, disclosed): `iam_eval.ACTION_CONDITION_KEYS` carries no `ses:SendEmail` row, so
# `validate_policy` makes "no claim either way" about the two condition keys; the keys are
# justified against the AWS SES documentation in the module comment and pinned by the
# fixture-equality tftest run. Adding the row is a change to a review-pinned SECURITY-CRITICAL
# collection (tests/fixtures/review-pin-registry.json) and needs its own review record under
# that control; it is deliberately not smuggled into this change.


def test_validator_refuses_a_bare_wildcard_action():
    doc = policy()
    doc["Statement"][0]["Action"] = ["*"]
    assert any("wildcard" in p for p in iam_eval.validate_policy(doc, "identity"))


def test_a_wildcard_identity_resource_would_widen_to_every_identity():
    """Positive control for the Resource assertion: with identity/* the Allow would fire for
    the other identity too, which is exactly the scope the reviewed policy refuses."""
    doc = copy.deepcopy(policy())
    doc["Statement"][0]["Resource"] = "arn:aws:ses:us-east-1:111122223333:identity/*"
    widened = iam_eval.decide(doc, "ses:SendEmail", OTHER_IDENTITY, CONTEXT)
    assert widened.decision == iam_eval.Decision.EXPLICIT_ALLOW
    reviewed = iam_eval.decide(policy(), "ses:SendEmail", OTHER_IDENTITY, CONTEXT)
    assert reviewed.decision == iam_eval.Decision.IMPLICIT_DENY


def test_dropping_the_from_address_condition_would_allow_any_local_part():
    doc = copy.deepcopy(policy())
    del doc["Statement"][0]["Condition"]["StringEquals"]["ses:FromAddress"]
    widened = iam_eval.decide(doc, "ses:SendEmail", IDENTITY,
                              {"ses:FromAddress": "security@mail.staging.example.com", "ses:ApiVersion": "2"})
    assert widened.decision == iam_eval.Decision.EXPLICIT_ALLOW


# =====================================================================================
# Binding — the grant is on the API task role and on nothing else
# =====================================================================================

def test_the_ses_policy_binds_the_api_task_role_and_no_other():
    writable = tf_roles.writable_roles()
    bound = {role for role, policies in writable.items()
             if any(p["policy_label"] == "api_ses_send" for p in policies)}
    assert bound == {"api_task"}, writable
    modules = {p["module"] for policies in writable.values() for p in policies
               if p["policy_label"] == "api_ses_send"}
    assert modules == {"infra/aws/modules/iam/main.tf"}


def test_no_other_policy_in_the_module_carries_an_ses_action():
    """Every "ses:" occurrence in the whole iam module must sit inside the api_ses_send
    section (its explanatory comment plus the resource block) — never in the execution,
    app_s3 (api + worker), ci_publisher or any other policy. The section is delimited by its
    own header and the ci_publisher header that follows it; everything outside is scanned."""
    text = (REPO_ROOT / "infra/aws/modules/iam/main.tf").read_text(encoding="utf-8")
    start = text.index("# --- API task role: SES v2 send grant")
    end = text.index("# --- CI image-publisher role")
    assert start < end
    outside = text[:start] + text[end:]
    assert "ses:" not in outside, "an SES action appears outside the api_ses_send section"
    assert 'resource "aws_iam_role_policy" "api_ses_send"' in text[start:end]
