#!/usr/bin/env python3
"""Explicit Allow-set model and both-sided ceiling proof (Gate 4N-I7, Defects 3 and 6).

THE DEFECT. Every mutation test up to Gate 4N-I6 worked on the DENY axis: remove a Deny,
observe a failure. Nothing tested the ALLOW axis. A statement that grants one action too
many — or a wildcard that quietly swallows a forbidden one — moved no test at all. The
Deny axis proves the ceiling is present; only the Allow axis proves the ceiling HOLDS.

The model classifies every action a policy grants:

  REQUIRED      derived from infra/aws/operator-closure-contract.json, itself derived from
                the 2026-07-28 reference refresh, the repository graph and the pinned
                provider — not from the policy under test
  OPTIONAL      soft-fail actions recorded in the same contract, plus the read-backs a
                principal needs to prove its own result
  FORBIDDEN     capabilities no principal in this design may hold, at all, ever
  UNCLASSIFIED  granted but justified nowhere — a finding, never a pass

and then proves the ceiling from BOTH sides:

  0 LOSSES   every REQUIRED action still evaluates EXPLICIT_ALLOW
  0 ESCAPES  every FORBIDDEN action still evaluates EXPLICIT_DENY *after* it has been
             injected into an Allow statement. This is the load-bearing direction: it is
             what makes the ceiling a ceiling rather than an assertion about the current
             text.

Usage:
    python3 scripts/allow_model.py [--json]
Exit: 0 iff 0 losses, 0 escapes and 0 unclassified grants.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "infra" / "aws" / "operator-closure-contract.json"

import gen_bootstrap_operator_policy as boot  # noqa: E402
import gen_boundary_policy as gb  # noqa: E402
import gen_boundary_rollout as rollout  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import iam_eval  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

# The FORBIDDEN set lives in its own module, derived from attacker consequence rather than
# from the policies under test — see scripts/must_not_contract.py.
from must_not_contract import FORBIDDEN_CAPABILITIES  # noqa: E402  the must-not contract

REQUIRED, OPTIONAL, FORBIDDEN, UNCLASSIFIED = (
    "REQUIRED", "OPTIONAL", "FORBIDDEN", "UNCLASSIFIED")


# --- scoped exemptions ----------------------------------------------------------------
#
# Three principals legitimately need a capability the must-not contract forbids globally.
# An exemption is NOT a hole: each one names the exact resources it covers, and the proof
# below requires the SAME capability to remain explicitly denied everywhere else. An
# exemption whose out-of-scope probe is also allowed is an escape, reported as such.
#
# This is the correction to the Gate 4N-I6 model, which had no notion of a scoped
# exemption and would have had to either forbid the temporary operator from taking the
# state lock or drop dynamodb:PutItem from the must-not contract entirely.

EXEMPTIONS = {
    "temporary_operator": {
        # GATE 4N-I16 DEFECT 3. iam:PutRolePolicy is a FORBIDDEN capability that this
        # principal genuinely needs, so it is registered here as an EXEMPTION rather than
        # quietly dropped from the contract. That distinction is the whole point of this
        # registry: an exemption must PROVE it is scoped — allowed on the named in-scope
        # resource AND explicitly denied on every out-of-scope resource — whereas removing
        # the action from FORBIDDEN_CAPABILITIES would simply stop asking the question.
        #
        # Why the capability is needed: the composition declares six aws_iam_role_policy
        # resources and creating an inline-policy resource calls PutRolePolicy, so an
        # ordinary Stage-A apply cannot complete without it. Gate 4N-I15 concealed this by
        # EXCLUDING the action from the closure verifier on a false premise.
        #
        # Why it is contained: the grant is conditioned on iam:PermissionsBoundary equalling
        # the reviewed ceiling, so it cannot reach a role that is not already bounded, and
        # the target's effective permissions remain identity AND boundary. The out-of-scope
        # probes below include a role INSIDE the signalnest-staging-* prefix precisely
        # because the read grant uses that prefix — a write grant that inherited it would
        # look correct and be wrong.
        "iam:PutRolePolicy": {
            "reason": "writes the inline policies the composition declares for its own "
                      "roles; Gate 4N-I16 Defect 3 classification REQUIRED_TEMPORARILY",
            "context": lambda: {"iam:PermissionsBoundary": gen.ARN["boundary"]},
            "in_scope": lambda: gen.INLINE_POLICY_ROLE_ARNS[0],
            "out_of_scope": lambda: [
                f"arn:aws:iam::{gen.ACCOUNT}:role/{gen.PREFIX}-not-a-declared-role",
                f"arn:aws:iam::{gen.ACCOUNT}:role/some-unrelated-role",
                f"arn:aws:iam::{gen.ACCOUNT}:role/aws-reserved/sso.amazonaws.com/"
                f"{gen.REGION}/AWSReservedSSO_AdministratorAccess_abc123",
            ],
        },
        "s3:PutObject": {
            "reason": "writes the new state object on apply; state_backend_closure."
                      "write_apply_only in the closure contract",
            "in_scope": lambda: f"{gen.ARN['state_bucket']}/{gen.LIVE_NAMES['state_key']}",
            "out_of_scope": lambda: [f"{gen.ARN['audit_bucket']}/AWSLogs/x",
                                     "arn:aws:s3:::some-other-bucket/x"],
        },
        "dynamodb:PutItem": {
            "reason": "acquires the Terraform state lock",
            "in_scope": lambda: gen.ARN["lock"],
            "out_of_scope": lambda: [
                f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"],
        },
        "dynamodb:DeleteItem": {
            "reason": "releases the Terraform state lock it acquired",
            "in_scope": lambda: gen.ARN["lock"],
            "out_of_scope": lambda: [
                f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"],
        },
        "s3:GetObject": {
            "reason": "reads the encrypted state object; state_backend_closure.read",
            "in_scope": lambda: f"{gen.ARN['state_bucket']}/{gen.LIVE_NAMES['state_key']}",
            "out_of_scope": lambda: [f"{gen.ARN['audit_bucket']}/AWSLogs/x",
                                     f"{identity.s3_bucket_arn(identity.APP_BUCKET_NAME)}/x"],
        },
        "dynamodb:GetItem": {
            "reason": "inspects the state lock before acquiring it",
            "in_scope": lambda: gen.ARN["lock"],
            "out_of_scope": lambda: [
                f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"],
        },
        "kms:Decrypt": {
            "reason": "decrypts the state object under the state CMK; the SECRETS CMK is "
                      "fenced off because it protects the database credential",
            "in_scope": lambda: gen.ARN["cmk_state"],
            "out_of_scope": lambda: [gen.ARN["cmk_secrets"]],
        },
        # GATE 4N-I9 DEFECT 1: iam:CreateRole and iam:PutRolePolicy are NO LONGER exempted
        # for this principal. Stage-A cannot author roles at all, because CreateRole accepts
        # the trust document and AWS cannot condition it. The capability moved to a separate
        # RoleBootstrapOperator.
        # iam:PassRole is deliberately NOT exempted here. The first draft assumed the
        # temporary operator needed it because the reader runner does — but the runner
        # needs it at RUNTIME and is a different principal. stage_a_create_closure requires
        # only CreateRole, PutRolePolicy and TagRole, so PassRole stays flatly forbidden.
    },
    # INFRA-9 B-3 (2026-08-16): permanent W0 is the APPLY IDENTITY. It holds the
    # state-backend closure and task-definition registration SCOPED — each capability
    # allowed on exactly the backend resource / composition families the closure contract
    # names, and explicitly denied everywhere else by its NotResource fence. The proof
    # below requires both directions for every entry, so a fence that silently widens or
    # an allow that silently narrows is an escape or a loss, never a pass.
    "permanent_w0": {
        "s3:GetObject": {
            "reason": "reads the encrypted state object; state_backend_closure.read "
                      "(B-3 apply-identity adjudication)",
            "in_scope": lambda: gen.ARN["state_object"],
            "out_of_scope": lambda: [f"{gen.ARN['audit_bucket']}/AWSLogs/x",
                                     f"{identity.s3_bucket_arn(identity.APP_BUCKET_NAME)}/x"],
        },
        "s3:PutObject": {
            "reason": "writes the new state object on apply; state_backend_closure."
                      "write_apply_only (B-3 apply-identity adjudication)",
            "in_scope": lambda: gen.ARN["state_object"],
            "out_of_scope": lambda: [f"{gen.ARN['audit_bucket']}/AWSLogs/x",
                                     f"{gen.ARN['state_bucket']}/other/object"],
        },
        "dynamodb:GetItem": {
            "reason": "inspects the state lock before acquiring it",
            "in_scope": lambda: gen.ARN["lock"],
            "out_of_scope": lambda: [
                f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"],
        },
        "dynamodb:PutItem": {
            "reason": "acquires the Terraform state lock",
            "in_scope": lambda: gen.ARN["lock"],
            "out_of_scope": lambda: [
                f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"],
        },
        "dynamodb:DeleteItem": {
            "reason": "releases the Terraform state lock it acquired",
            "in_scope": lambda: gen.ARN["lock"],
            "out_of_scope": lambda: [
                f"arn:aws:dynamodb:{gen.REGION}:{gen.ACCOUNT}:table/some-other-table"],
        },
        "kms:Decrypt": {
            "reason": "decrypts the state object under the state CMK, and ONLY via the S3 "
                      "and DynamoDB backend services (kms:ViaService); the SECRETS CMK is "
                      "fenced off because it protects the database credential",
            "context": lambda: {"kms:ViaService": f"s3.{gen.REGION}.amazonaws.com"},
            "in_scope": lambda: gen.ARN["cmk_state"],
            "out_of_scope": lambda: [gen.ARN["cmk_secrets"]],
        },
        "ecs:RegisterTaskDefinition": {
            "reason": "registers task-definition revisions for exactly the four composition "
                      "families (all Stage-B-gated); no PassRole is added — whether "
                      "registration performs a PassRole check is recorded DISPUTED "
                      "(contract _no_passrole_note), and zero surface is the fail-closed "
                      "direction pending the mandatory Part-B canary gate",
            # A CONCRETE revision, not the family:* pattern itself: AWS authorizes against
            # a revision-bearing ARN, and probing the pattern with the pattern succeeds by
            # string identity without exercising the match (architect-lane finding 10).
            "in_scope": lambda: gen.TASK_DEFINITION_FAMILY_ARNS[0][:-1] + "1",
            "out_of_scope": lambda: [
                f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/{gen.PREFIX}-evil:1",
                f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/anything:9"],
        },
    },
    "bootstrap_operator": {
        "iam:CreatePolicy": {
            "reason": "creates the reviewed boundary policy ONCE under Operating Model 1; it "
                      "holds no version or delete capability, so it cannot revise the ceiling",
            "in_scope": lambda: identity.BOUNDARY_POLICY_ARN,
            "out_of_scope": lambda: [f"arn:aws:iam::{gen.ACCOUNT}:policy/anything-else"],
        },
        "iam:PutRolePermissionsBoundary": {
            "reason": "THIS IS ITS ONLY JOB. Conditioned on iam:PermissionsBoundary equal "
                      "to the reviewed boundary, so it cannot attach a permissive one",
            "in_scope": lambda: identity.ALL_ROLE_ARNS[0],
            "out_of_scope": lambda: [f"arn:aws:iam::{gen.ACCOUNT}:role/unrelated"],
            "context": lambda: {"iam:PermissionsBoundary": gen.ARN["boundary"]},
        },
        "iam:DeleteRolePermissionsBoundary": {
            "reason": "rollback path; Gate 4N-I10 removed the DISPUTED "
                      "iam:PermissionsBoundary condition from it, because an unpopulated key "
                      "would make the grant dead exactly when rollback is needed",
            "in_scope": lambda: identity.ALL_ROLE_ARNS[0],
            "out_of_scope": lambda: [f"arn:aws:iam::{gen.ACCOUNT}:role/unrelated"],
            # Deliberately NO context: the grant must work WITHOUT the disputed key.
        },
    },
    "role_bootstrap_operator": {
        "iam:CreateRole": {
            "reason": "creating exactly the three reader roles is this principal's only "
                      "purpose; the trust bytes are controlled by read-back, not by IAM",
            "in_scope": lambda: rb.TARGET_ROLE_ARNS[0],
            "out_of_scope": lambda: [f"arn:aws:iam::{gen.ACCOUNT}:role/anything-else"],
            "context": lambda: {"iam:PermissionsBoundary": gen.ARN["boundary"]},
        },
        "iam:DeleteRole": {
            "reason": "reverts a role whose trust document fails the read-back comparison; "
                      "a control that detects without reverting is an alert, not a control",
            "in_scope": lambda: rb.TARGET_ROLE_ARNS[0],
            "out_of_scope": lambda: [f"arn:aws:iam::{gen.ACCOUNT}:role/anything-else"],
        },
    },
    "boundary": {
        "secretsmanager:GetSecretValue": {
            "reason": "the ecs-execution and reader-execution roles read the containers "
                      "referenced by their task definitions; a blanket deny breaks startup",
            "in_scope": lambda: (f"arn:aws:secretsmanager:{gen.REGION}:{gen.ACCOUNT}:secret:"
                                 f"{gen.PREFIX}/DATABASE_URL-AbCdEf"),
            "out_of_scope": lambda: [
                f"arn:aws:secretsmanager:{gen.REGION}:{gen.ACCOUNT}:secret:other/thing"],
        },
        "kms:Decrypt": {
            "reason": "the same roles decrypt those secrets under the SECRETS CMK; the "
                      "STATE CMK stays denied",
            "in_scope": lambda: gb.SECRETS_CMK,
            "out_of_scope": lambda: [gb.STATE_CMK],
        },
        "s3:GetObject": {
            "reason": "the api and worker task roles read the application bucket",
            "in_scope": lambda: f"{identity.s3_bucket_arn(identity.APP_BUCKET_NAME)}/x",
            "out_of_scope": lambda: [
                f"{gb.STATE_BUCKET}/{gen.PREFIX}/root.tfstate"],
        },
        "ecs:RunTask": {
            "reason": "the revision-reader runner's only job is to run exactly one task "
                      "definition family; a blanket deny would break it",
            "in_scope": lambda: (f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/"
                                 f"{gen.PREFIX}-revision-reader:1"),
            "out_of_scope": lambda: [
                f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/{gen.PREFIX}-api:1",
                f"arn:aws:ecs:{gen.REGION}:{gen.ACCOUNT}:task-definition/anything:9"],
        },
        "iam:PassRole": {
            "reason": "the runner passes exactly the reader execution role",
            "in_scope": lambda: gb.READER_EXECUTION_ROLE,
            "out_of_scope": lambda: [f"arn:aws:iam::{gen.ACCOUNT}:role/{gen.PREFIX}-ecs-execution"],
        },
    },
}

def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def required_actions() -> dict[str, str]:
    """REQUIRED, derived from the closure contract — never from the policy under test."""
    # INFRA-9 B-3: stage_b_task_definition_closure joined the requirement sections when
    # permanent W0 became the apply identity.
    return _sections_required(("refresh_closure", "stage_a_create_closure",
                               "state_backend_closure", "stage_b_task_definition_closure"))


def _sections_required(sections: tuple) -> dict[str, str]:
    doc = contract()
    out: dict[str, str] = {}
    for section in sections:
        for group, actions in doc[section].items():
            if group.startswith("_") or not isinstance(actions, list):
                continue
            for action in actions:
                out[action] = f"{section}.{group}"
    return out


def temporary_required_actions() -> dict[str, str]:
    """The temporary operator's OWN requirement set — unchanged by INFRA-9 B-3. The Stage-B
    task-definition closure belongs to permanent W0 alone; the temporary operator explicitly
    DENIES ecs:RegisterTaskDefinition, and demanding it here would report that deliberate
    ceiling as a loss."""
    return _sections_required(("refresh_closure", "stage_a_create_closure",
                               "state_backend_closure"))


def w0_required_actions() -> dict[str, str]:
    """Permanent W0's OWN requirement set (INFRA-9 B-3): the refresh closure plus the apply
    surface. stage_a_create_closure is deliberately excluded — ECR/IAM create-path reads
    belong to the temporary operator, and demanding them of W0 would report every flat
    ceiling hit as a loss."""
    return _sections_required(("refresh_closure", "state_backend_closure",
                               "stage_b_task_definition_closure"))


def optional_actions() -> dict[str, str]:
    doc = contract()
    out: dict[str, str] = {}
    for entry in doc.get("historical_denials_classified", []):
        if entry.get("classification", "").startswith("OPTIONAL"):
            out[entry["action"]] = f"historical_denials_classified: {entry['classification']}"
    return out


def rollout_required_actions() -> dict[str, str]:
    """REQUIRED for the bootstrap operator, derived from the rollout owner graph.

    The closure contract describes the OpenTofu operator and says nothing about boundary
    administration, so the bootstrap operator's nine policy-lifecycle grants classified as
    UNCLASSIFIED against it. The rollout graph is their real external justification: it
    enumerates the operations, names the principal for each, and is itself verified by
    scripts/gen_boundary_rollout.py --check.
    """
    out: dict[str, str] = {}
    for operation in rollout.operations():
        if operation["principal"] != rollout.BOOTSTRAP:
            continue
        for action in operation.get("actions", []):
            out[action] = f"rollout operation {operation['n']}: {operation['op']}"
    return out


def classify(action: str, extra_required: dict[str, str] | None = None) -> tuple[str, str]:
    if extra_required and action in extra_required and action not in FORBIDDEN_CAPABILITIES:
        return REQUIRED, extra_required[action]
    if action in FORBIDDEN_CAPABILITIES:
        return FORBIDDEN, FORBIDDEN_CAPABILITIES[action]
    required, optional = required_actions(), optional_actions()
    if action in required:
        return REQUIRED, required[action]
    if action in optional:
        return OPTIONAL, optional[action]
    return UNCLASSIFIED, "granted but justified in no external source"


# Wildcards a policy may legitimately grant, each with the reason it cannot be enumerated.
# Anything not listed here is an UNCLASSIFIED grant even if every action it matches is
# individually required.
PERMITTED_WILDCARDS = {
    "sts:GetCallerIdentity": "not a wildcard; listed for symmetry with the audit output",
}


class WildcardJustificationError(RuntimeError):
    """Fail-closed. A wildcard nothing independent justifies is never treated as permitted."""


def require_independently_justified_wildcards(permitted: dict | None = None) -> None:
    """GATE 4N-I27O. Membership in PERMITTED_WILDCARDS is not a justification.

    THE DEFECT THIS CLOSES. `audit_grants` classified a granted token OPTIONAL when the token
    appeared in PERMITTED_WILDCARDS and UNCLASSIFIED otherwise, so the list WAS the oracle.
    Granting `budgets:*` and adding `budgets:*` to this dict made the audit clean: the thing
    being audited supplied its own permission. Executed at Gate 4N-I27K and re-executed here.

    THE INDEPENDENT SOURCE. `contract()` — infra/aws/operator-closure-contract.json — is
    authored separately from this module and is what `required_actions()` / `optional_actions()`
    already derive from. An entry earns its place here only if that contract speaks for it.

    WHY `service:*` IS REFUSED OUTRIGHT. A wildcard's danger is the actions it will match that
    nobody has reviewed, including ones AWS has not shipped yet. No repository-side source can
    bound that set, so there is no evidence that would justify it — and a control that cannot
    be satisfied by evidence must refuse, not shrug. Enumerate the actions instead.
    """
    permitted = PERMITTED_WILDCARDS if permitted is None else permitted
    if not isinstance(permitted, dict):
        raise WildcardJustificationError(
            f"the permitted-wildcard declaration must be a mapping of token -> reason, got "
            f"{type(permitted).__name__}")
    problems: list[str] = []
    for token, reason in sorted(permitted.items()):
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{token}: no stated reason")
        swallowed = sorted(a for a in FORBIDDEN_CAPABILITIES if fnmatch.fnmatch(a, token))
        if swallowed:
            problems.append(
                f"{token}: swallows must-not-contract capabilities {swallowed}")
        if "*" in token or "?" in token:
            problems.append(
                f"{token}: a wildcard cannot be justified by any repository-side source, "
                "because the actions it will match are not enumerable. Grant the exact "
                "actions the closure contract names instead.")
            continue
        classification, why = classify(token)
        if classification not in (REQUIRED, OPTIONAL):
            problems.append(
                f"{token}: the independent closure contract classifies it {classification} "
                f"({why}); membership in this list is not a justification")
    if problems:
        raise WildcardJustificationError(
            "permitted-wildcard declarations are not independently justified:\n  "
            + "\n  ".join(problems))


def granted_action_tokens(policy: dict) -> list[str]:
    out = []
    for statement in policy["Statement"]:
        if statement.get("Effect") != "Allow":
            continue
        raw = statement.get("Action", [])
        out.extend(raw if isinstance(raw, list) else [raw])
    return sorted(set(out))


def role_bootstrap_required_actions() -> dict[str, str]:
    """REQUIRED for the RoleBootstrapOperator, from the closure contract's own section.

    Gate 4N-I9 moved iam:CreateRole / PutRolePolicy / TagRole out of stage_a_create_closure
    into role_bootstrap_closure, because Stage-A no longer authors roles. Reading that
    section here keeps the justification external: the principal's grants are checked
    against the contract, not against the generator that produced them.
    """
    section = contract().get("role_bootstrap_closure", {})
    out: dict[str, str] = {}
    for group, actions in section.items():
        if group.startswith("_") or not isinstance(actions, list):
            continue
        for action in actions:
            out[action] = f"role_bootstrap_closure.{group}"
    return out


# Principals whose requirement set is not the OpenTofu refresh closure.
EXTRA_REQUIRED_SOURCES = {
    "bootstrap_operator": rollout_required_actions,
    "role_bootstrap_operator": role_bootstrap_required_actions,
}


def audit_grants(name: str, policy: dict) -> list[dict]:
    """Classify every action token the policy grants."""
    # GATE 4N-I27O. Validate the permitted-wildcard declaration against the INDEPENDENT closure
    # contract before it is used to excuse anything, so the list cannot license itself.
    require_independently_justified_wildcards()
    extra = EXTRA_REQUIRED_SOURCES.get(name, dict)()
    rows = []
    for token in granted_action_tokens(policy):
        if token == "*":
            # The boundary ceiling. It grants nothing on its own — a boundary only ever
            # intersects — and is covered by the escape proof below rather than here.
            rows.append({"policy": name, "action": token, "class": OPTIONAL,
                         "why": "boundary ceiling: intersects, never grants"})
            continue
        if "*" in token:
            swallowed = sorted(a for a in FORBIDDEN_CAPABILITIES
                               if fnmatch.fnmatch(a, token))
            rows.append({
                "policy": name, "action": token,
                "class": UNCLASSIFIED if (swallowed or token not in PERMITTED_WILDCARDS)
                else OPTIONAL,
                "why": f"wildcard swallows forbidden capabilities: {swallowed}" if swallowed
                else PERMITTED_WILDCARDS.get(token, "wildcard grant with no justification"),
                "swallows_forbidden": swallowed,
            })
            continue
        kind, why = classify(token, extra)
        rows.append({"policy": name, "action": token, "class": kind, "why": why})
    return rows


# --- the both-sided ceiling proof ------------------------------------------------------


def _inject_into_allow(policy: dict, action: str) -> dict:
    """Widen the policy with a FRESH, UNCONDITIONED Allow on Resource "*".

    The first draft of this appended the action to the policy's first existing Allow
    statement. That statement carries an expiry condition, so the injected action inherited
    it and the evaluator returned MISSING_CONTEXT — an inconclusive result that looked like
    an escape in the report and would have looked like a pass to a less strict reader.

    A fresh unconditioned statement is also the STRONGER mutation: it is the most
    permissive grant an editing mistake can produce, so absorbing it is the harder claim.
    """
    return {**policy, "Statement": [
        {"Sid": "InjectedWideningMutation", "Effect": "Allow",
         "Action": action, "Resource": "*"},
        *policy["Statement"],
    ]}


def prove_ceiling(name: str, policy: dict, context: dict, probe_resource) -> dict:
    """0 escapes. Every forbidden capability is scored, none is skipped.

    Unexempted capability: inject the widening mutation, require EXPLICIT_DENY.
    Exempted capability:   require EXPLICIT_ALLOW on the named in-scope resource AND
                           EXPLICIT_DENY on every out-of-scope resource. An exemption that
                           is allowed out of scope is an escape, not an exemption.
    """
    escapes, absorbed, exempted = [], [], []
    exemptions = EXEMPTIONS.get(name, {})

    for action in sorted(FORBIDDEN_CAPABILITIES):
        consequence = FORBIDDEN_CAPABILITIES[action]

        if action in exemptions:
            spec = exemptions[action]
            ctx = {**context, **(spec.get("context", dict)() or {})}
            in_scope = spec["in_scope"]()
            in_result = iam_eval.decide(policy, action, in_scope, ctx)
            row = {"policy": name, "action": action, "consequence": consequence,
                   "reason": spec["reason"], "in_scope": in_scope,
                   "in_scope_decision": in_result.decision.name, "out_of_scope": []}
            ok = in_result.decision is Decision.EXPLICIT_ALLOW
            if not ok:
                row["defect"] = ("the exemption does not actually work: the in-scope "
                                 "resource is not allowed, so the capability is lost")
            for resource in spec["out_of_scope"]():
                out_result = iam_eval.decide(policy, action, resource, ctx)
                row["out_of_scope"].append(
                    {"resource": resource, "decision": out_result.decision.name})
                if out_result.decision is not Decision.EXPLICIT_DENY:
                    ok = False
                    row["defect"] = ("the exemption is NOT scoped: the capability is also "
                                     f"available on {resource}")
            (exempted if ok else escapes).append(row)
            continue

        widened = _inject_into_allow(policy, action)
        resource = probe_resource(action)
        result = iam_eval.decide(widened, action, resource, context)
        row = {"policy": name, "action": action, "resource": resource,
               "decision": result.decision.name, "consequence": consequence}
        if result.decision is Decision.EXPLICIT_DENY:
            absorbed.append(row)
        else:
            escapes.append(row)

    scored = len(absorbed) + len(exempted)
    return {"absorbed": absorbed, "exempted": exempted, "escapes": escapes,
            "score": f"{scored}/{len(FORBIDDEN_CAPABILITIES)}"}


def prove_no_losses(name: str, policy: dict, context: dict, probe_resource,
                    required: dict[str, str] | None = None,
                    probe_overrides: dict[str, str] | None = None) -> dict:
    """0 losses: the ceiling must not remove a capability the design requires.

    A scoped capability is probed on the resource its exemption names. The generic probe
    returns a deliberately out-of-scope ARN — that is what makes the escape half strict —
    so using it here would report every fenced capability as a loss.

    `required` selects the principal's OWN requirement set (INFRA-9 B-3): the full
    contract for the temporary operator, w0_required_actions() for permanent W0.
    """
    losses = []
    exemptions = EXEMPTIONS.get(name, {})
    # A probe override must name a resource the emitted policy ACTUALLY grants for that
    # action — otherwise the thing being proved supplies its own unverified probe, the
    # shape Gate 4N-I27O hardened PERMITTED_WILDCARDS against (architect-lane finding 9).
    # A rejected override is reported as a loss, never silently believed.
    for action, resource in sorted((probe_overrides or {}).items()):
        granted: list = []
        for statement in policy["Statement"]:
            if statement.get("Effect") != "Allow":
                continue
            acts = statement.get("Action", [])
            if action in ([acts] if isinstance(acts, str) else acts):
                res = statement.get("Resource", [])
                granted.extend([res] if isinstance(res, str) else res)
        if resource not in granted:
            losses.append({"policy": name, "action": action, "resource": resource,
                           "provenance": "probe override",
                           "note": "OVERRIDE REJECTED: names a resource the emitted Allow "
                                   "does not grant — an override must be verifiable against "
                                   "the policy, not merely asserted by the caller"})
    for action, provenance in sorted((required if required is not None
                                      else required_actions()).items()):
        spec = exemptions.get(action)
        resource = (spec["in_scope"]() if spec
                    else (probe_overrides or {}).get(action) or probe_resource(action))
        ctx = {**context, **(spec.get("context", dict)() or {})} if spec else context
        result = iam_eval.decide(policy, action, resource, ctx)
        if result.decision is Decision.EXPLICIT_DENY:
            losses.append({"policy": name, "action": action, "resource": resource,
                           "provenance": provenance,
                           "note": "REQUIRED by the closure contract but explicitly denied"})
    # The exempted-but-not-required direction: a fence that is too tight silently removes
    # a capability nothing else would notice.
    for action, spec in sorted(exemptions.items()):
        ctx = {**context, **(spec.get("context", dict)() or {})}
        result = iam_eval.decide(policy, action, spec["in_scope"](), ctx)
        if result.decision is not Decision.EXPLICIT_ALLOW:
            losses.append({"policy": name, "action": action, "resource": spec["in_scope"](),
                           "provenance": f"scoped exemption: {spec['reason']}",
                           "note": f"exempted but {result.decision.name} in scope"})
    return {"losses": losses}


# --- probes ---------------------------------------------------------------------------


def boundary_probe(action: str) -> str:
    """A plausible in-scope resource for each action, so a Deny fence is actually tested."""
    service = action.split(":", 1)[0]
    return {
        "s3": f"{gen.ARN['state_bucket']}/{gen.LIVE_NAMES['state_key']}",
        "dynamodb": gen.ARN["lock"],
        "kms": gen.ARN["cmk_state"],
        "secretsmanager": f"arn:aws:secretsmanager:{gen.REGION}:{gen.ACCOUNT}:secret:other/x",
        "rds": gen.ARN["db"],
        "cloudtrail": gen.ARN["trail"],
        "iam": f"arn:aws:iam::{gen.ACCOUNT}:role/anything",
        "ecs": "*",
        "sso": "*",
        "organizations": "*",
        "sts": "arn:aws:iam::999988887777:role/outside",
    }.get(service, "*")


# GATE 4N-I8 DEFECT 11. The previous version built the two temporary policies with the
# unstamped placeholder and evaluated them at aws:CurrentTime = 2000-01-01. That passed only
# because ASCII '2' (0x32) sorts below '<' (0x3C) — the proof never exercised a real expiry
# at all. Both policies are now STAMPED with a real instant, and the evaluation clock sits
# inside the window rather than in the year 2000.
# GATE 4N-I19, ADV-A: the ONE authoritative reviewed window. Gate 4N-I17's architect lane
# found ~20 independent expiry literals with nothing asserting they agreed; they now all
# resolve to the single authorized pair, so a restamp cannot leave stragglers behind.
import expiry_authorization as _ea  # noqa: E402

MODEL_EXPIRY = _ea.ACTIVE_EXPIRY_UTC
IN_WINDOW = {"aws:CurrentTime": "2026-07-31T12:00:00Z"}

TARGETS = {
    "boundary": (gb.boundary_policy, {}, boundary_probe),
    "permanent_w0": (gen.permanent_w0_policy, {}, boundary_probe),
    "temporary_operator": (lambda: gen.bootstrap_temp_policy(MODEL_EXPIRY),
                           IN_WINDOW, boundary_probe),
    "bootstrap_operator": (lambda: boot.bootstrap_operator_policy(MODEL_EXPIRY),
                           IN_WINDOW, boundary_probe),
    "role_bootstrap_operator": (lambda: rb.role_bootstrap_policy(MODEL_EXPIRY),
                                IN_WINDOW, boundary_probe),
}


def run() -> dict:
    report = {"forbidden_count": len(FORBIDDEN_CAPABILITIES), "policies": {}}
    for name, (build, context, probe) in TARGETS.items():
        policy = build()
        grants = audit_grants(name, policy)
        ceiling = prove_ceiling(name, policy, context, probe)
        entry = {
            "grants": grants,
            "unclassified": [g for g in grants if g["class"] == UNCLASSIFIED],
            "escapes": ceiling["escapes"],
            "exempted": ceiling["exempted"],
            "escape_score": ceiling["score"],
        }
        # Loss analysis applies only to principals that must perform the closure. The
        # boundary caps ROLES, which never run tofu, so the closure contract is not its
        # requirement set. INFRA-9 B-3: permanent W0 became the apply identity, so it is
        # loss-proved over ITS requirement set (refresh + backend + Stage-B registration).
        if name == "temporary_operator":
            entry.update(prove_no_losses(name, policy, context, probe,
                                         required=temporary_required_actions()))
        elif name == "permanent_w0":
            # ecs:TagResource is fenced but NOT forbidden, so it has no EXEMPTIONS entry to
            # supply an in-scope probe; the generic ecs probe ("*") lands on the fence BY
            # DESIGN. Probe it at the resource the closure grants. (Conditioned grants —
            # kms:GenerateDataKey, ecs:DescribeTaskDefinition — evaluate MISSING_CONTEXT
            # here and are proven positively by the pytest suite with real contexts.)
            entry.update(prove_no_losses(
                name, policy, context, probe, required=w0_required_actions(),
                probe_overrides={"ecs:TagResource": gen.TASK_DEFINITION_FAMILY_ARNS[0]}))
        report["policies"][name] = entry

    report["totals"] = {
        "unclassified_grants": sum(len(p["unclassified"]) for p in report["policies"].values()),
        "escapes": sum(len(p["escapes"]) for p in report["policies"].values()),
        "losses": sum(len(p.get("losses", [])) for p in report["policies"].values()),
    }
    report["clean"] = all(v == 0 for v in report["totals"].values())
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["clean"] else 1
    for name, entry in report["policies"].items():
        print(f"  {name:20s} escapes {entry['escape_score']:>7s}  "
              f"unclassified {len(entry['unclassified'])}  losses {len(entry.get('losses', []))}")
    for name, entry in report["policies"].items():
        for row in entry["escapes"]:
            detail = row.get("defect") or f"-> {row.get('decision')}"
            print(f"    ESCAPE {name} {row['action']} {detail} "
                  f"({row['consequence']})", file=sys.stderr)
        for row in entry["unclassified"]:
            print(f"    UNCLASSIFIED {name} {row['action']}: {row['why']}", file=sys.stderr)
        for row in entry.get("losses", []):
            print(f"    LOSS {name} {row['action']} ({row['provenance']})", file=sys.stderr)
    print("ALLOW MODEL: clean" if report["clean"] else "ALLOW MODEL: findings")
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
