#!/usr/bin/env python3
"""RoleBootstrap lifecycle DAG (Gate 4N-I16, Defects 5, 6 and 7).

WHAT THIS FIXES, precisely.

DEFECT 5 — READ_ONLY_VERIFIER OWNED TWELVE STEPS AND DID NOT EXIST. It had no policy, no
permission set, no creation path and no retirement, and `action_availability()` skipped it BY
NAME while its docstring claimed to cover "every non-root step". Six of its steps could not
have executed. Coverage is now DISCOVERED from the graph: every owner that holds an AWS
action must have a registered policy, and a missing policy is a finding rather than a skip.
There is no exclusion list keyed to a principal's name anywhere in this file.

DEFECT 6 — ORDERING WAS NOT A VALIDATED DAG. Both ordering invariants keyed off an integer
`n` that nothing validated: not uniqueness, not integrality, not agreement with the
prerequisite chain, and there was no cycle detection at all. Flattening every `n` to the same
value silently disabled BOTH invariants while the defects they exist to catch were present,
and a step could name a prerequisite that ran after it. Ordering is now a real DAG:
dependencies are edges, a Kahn topological sort proves acyclicity, and the explicit sequence
must agree with a valid topological order.

DEFECT 7 — THE GRAPH HASH WAS SELF-REFERENTIAL. `graph_hash() == graph_hash()` plus a length
check; replacing the body with a constant left the suite green. The canonical form now lives
in scripts/lifecycle_canonical.py and is implemented independently there; this module's hash
is checked AGAINST that implementation and against a byte fixture that is STAGED FOR
ADDITION (Gate 4N-I20: it is not in HEAD yet, so calling it 'committed' was false).

NOTHING HERE EXECUTES. This gate authorizes no AWS call. The graph is a reviewable design for
a future, separately authorized operation.

Usage:
    python3 scripts/role_bootstrap_lifecycle.py [--json]
Exit: 0 iff every lifecycle invariant holds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_readonly_verifier_policy as verifier  # noqa: E402
import gen_role_bootstrap_policy as rb  # noqa: E402
import iam_eval  # noqa: E402
import lifecycle_canonical  # noqa: E402
import signalnest_identity as identity  # noqa: E402
from iam_eval import Decision  # noqa: E402

# GATE 4N-I19, ADV-A: the ONE authoritative reviewed window. Gate 4N-I17's architect lane
# found ~20 independent expiry literals with nothing asserting they agreed; they now all
# resolve to the single authorized pair, so a restamp cannot leave stragglers behind.
import expiry_authorization as _ea  # noqa: E402

EXPIRY = _ea.ACTIVE_EXPIRY_UTC
IN_WINDOW = {"aws:CurrentTime": "2026-07-31T12:00:00Z",
             "aws:RequestedRegion": identity.REGION}

# --- actor classes --------------------------------------------------------------------
ROOT = "ROOT_CONSOLE_OPERATOR"
BOOTSTRAP = "ROLE_BOOTSTRAP_OPERATOR"
VERIFIER = "READ_ONLY_VERIFIER"
LEAD = "LEAD_COORDINATOR"

ACTOR_RULES = {
    ROOT: {
        "may_own": "root-gated Identity Center administration only",
        "prohibited": ["root CLI", "root access keys", "any IAM data-plane mutation"],
        "policy": None,             # the root console is not policy-governed
        "root_gated": True,
    },
    BOOTSTRAP: {
        "may_own": "SSO identity validation, candidate-bound role creation, immediate trust "
                   "and boundary read-back, mismatch rollback",
        "prohibited": ["Identity Center administration", "PassRole",
                       "UpdateAssumeRolePolicy", "state", "secrets"],
        "policy": lambda: rb.role_bootstrap_policy(EXPIRY),
        "root_gated": False,
    },
    VERIFIER: {
        "may_own": "read-back and evidence inspection only",
        "prohibited": ["every mutating action"],
        "policy": lambda: verifier.readonly_verifier_policy(EXPIRY),
        "root_gated": False,
    },
    LEAD: {
        "may_own": "local artifact preparation and evidence collection",
        "prohibited": ["every AWS action"],
        "policy": None,             # holds no AWS action; validated below
        "root_gated": False,
    },
}

# Identity Center administration is root-console only. The two STATUS POLLS are reads and are
# deliberately NOT in this set — Gate 4N-I15 listed them here while assigning them to the
# verifier, so the artifact asserted an absolute the code contradicted. The set now contains
# only actions that CHANGE Identity Center state.
IDENTITY_CENTRE_ADMIN_ACTIONS = {
    "sso:CreatePermissionSet", "sso:PutInlinePolicyToPermissionSet",
    "sso:CreateAccountAssignment", "sso:ProvisionPermissionSet",
    "sso:DeleteAccountAssignment", "sso:DeletePermissionSet",
}

ROLE_ARNS = rb.TARGET_ROLE_ARNS
BOOTSTRAP_PS = "SignalNestRoleBootstrapOperator"
VERIFIER_PS = verifier.PERMISSION_SET_NAME


def _s(sequence, sid, owner, action, resource, *, depends_on=(), readback=None, evidence=None,
       timeout=None, rollback=None, retire_after=False, expiry_dependent=False,
       needs_assignment=True, note=None, mutation=False):
    return {"sequence": sequence, "step_id": sid, "owner": owner, "actor_class": owner,
            "action": action, "resource": resource, "depends_on": list(depends_on),
            "read_back": readback, "evidence": evidence, "timeout_seconds": timeout,
            "rollback_owner": rollback, "is_mutation": mutation,
            "retires_principal_after": retire_after, "expiry_dependent": expiry_dependent,
            "requires_assignment": needs_assignment, "note": note}


def steps() -> list[dict]:
    """The lifecycle as a DAG. `sequence` is the explicit ordering key; `depends_on` are the
    edges. Both are validated — neither is trusted."""
    out = [
        _s(1, "root_session_open", ROOT, None, None,
           evidence="operator attestation", needs_assignment=False,
           note="root console only; no root CLI and no root access keys"),

        # === VERIFIER LIFECYCLE (Phase Q) — created FIRST, because it verifies the
        # bootstrap operator's own permission set before that operator is ever assigned.
        _s(2, "verifier_create_permission_set", ROOT, "sso:CreatePermissionSet", VERIFIER_PS,
           mutation=True, depends_on=["root_session_open"],
           readback="sso:DescribePermissionSet", evidence="verifier permission-set ARN",
           rollback=ROOT, needs_assignment=False),
        _s(3, "verifier_install_inline_policy", ROOT, "sso:PutInlinePolicyToPermissionSet",
           VERIFIER_PS, mutation=True, depends_on=["verifier_create_permission_set"],
           readback="sso:GetInlinePolicyForPermissionSet",
           evidence="verifier inline policy canonical hash", rollback=ROOT,
           needs_assignment=False),
        _s(4, "verifier_assign", ROOT, "sso:CreateAccountAssignment", VERIFIER_PS,
           mutation=True, depends_on=["verifier_install_inline_policy"],
           readback="sso:DescribeAccountAssignmentCreationStatus",
           evidence="verifier assignment request id", rollback=ROOT, needs_assignment=False),
        _s(5, "verifier_poll_assignment_creation", ROOT,
           "sso:DescribeAccountAssignmentCreationStatus", VERIFIER_PS, timeout=600,
           depends_on=["verifier_assign"], readback="terminal SUCCEEDED or FAILED",
           evidence="verifier assignment terminal status", needs_assignment=False,
           note="Gate 4N-I15 left CreateAccountAssignment unpolled while polling the other "
                "two async Identity Center calls; an eventually-consistent list cannot "
                "distinguish IN_PROGRESS from FAILED"),
        _s(6, "verifier_provision", ROOT, "sso:ProvisionPermissionSet", VERIFIER_PS,
           mutation=True, depends_on=["verifier_poll_assignment_creation"],
           readback="sso:DescribePermissionSetProvisioningStatus",
           evidence="verifier provisioning request id", rollback=ROOT, needs_assignment=False),
        _s(7, "verifier_poll_provisioning", ROOT,
           "sso:DescribePermissionSetProvisioningStatus", VERIFIER_PS, timeout=600,
           depends_on=["verifier_provision"], readback="terminal SUCCEEDED or FAILED",
           evidence="verifier provisioning terminal status", needs_assignment=False),
        _s(8, "verifier_sso_login", VERIFIER, None, None,
           depends_on=["verifier_poll_provisioning"], evidence="verifier session start",
           expiry_dependent=True, needs_assignment=False),
        _s(9, "verifier_verify_caller_identity", VERIFIER, "sts:GetCallerIdentity", "*",
           depends_on=["verifier_sso_login"],
           readback="assumed-role ARN matches the verifier reserved role",
           evidence="verifier caller identity", expiry_dependent=True,
           needs_assignment=False),

        # === BOOTSTRAP OPERATOR PERMISSION SET ==========================================
        _s(10, "create_permission_set", ROOT, "sso:CreatePermissionSet", BOOTSTRAP_PS,
           mutation=True, depends_on=["root_session_open"],
           readback="sso:DescribePermissionSet", evidence="permission-set ARN",
           rollback=ROOT, needs_assignment=False),
        _s(11, "install_inline_policy", ROOT, "sso:PutInlinePolicyToPermissionSet",
           BOOTSTRAP_PS, mutation=True, depends_on=["create_permission_set"],
           readback="sso:GetInlinePolicyForPermissionSet",
           evidence="inline policy canonical hash", rollback=ROOT, needs_assignment=False),

        # Verified by the VERIFIER, which is why its lifecycle runs first.
        _s(12, "verify_inline_policy_hash", VERIFIER, "sso:GetInlinePolicyForPermissionSet",
           BOOTSTRAP_PS, depends_on=["install_inline_policy", "verify_caller_identity_gate"],
           readback="canonical hash comparison", evidence="hash match record",
           needs_assignment=False,
           note="compares against the reviewed RoleBootstrapOperator policy hash"),

        # PHASE A3 FIX (Gate 4N-I15 architect): the managed-policy check now runs BEFORE the
        # operator is ever assigned, not after the permission set is deleted. As previously
        # ordered it ran against a deleted resource and passed on the "or the set is gone"
        # branch — a verification whose success criterion is the absence of its subject.
        _s(13, "verify_no_managed_policy_attachments", VERIFIER,
           "sso:ListManagedPoliciesInPermissionSet", BOOTSTRAP_PS,
           depends_on=["verify_inline_policy_hash"],
           readback="the attached-managed-policy list is EMPTY",
           evidence="attachment listing", needs_assignment=False,
           note="unconditional empty-list assertion; the inline hash comparison cannot see "
                "a MANAGED policy, so this is the only control over that path"),

        _s(14, "assign_operator", ROOT, "sso:CreateAccountAssignment", BOOTSTRAP_PS,
           mutation=True, depends_on=["verify_no_managed_policy_attachments"],
           readback="sso:DescribeAccountAssignmentCreationStatus",
           evidence="assignment request id", rollback=ROOT, needs_assignment=False),
        _s(15, "poll_assignment_creation", VERIFIER,
           "sso:DescribeAccountAssignmentCreationStatus", BOOTSTRAP_PS, timeout=600,
           depends_on=["assign_operator"], readback="terminal SUCCEEDED or FAILED",
           evidence="assignment terminal status", needs_assignment=False),
        _s(16, "provision_permission_set", ROOT, "sso:ProvisionPermissionSet", BOOTSTRAP_PS,
           mutation=True, depends_on=["poll_assignment_creation"],
           readback="sso:DescribePermissionSetProvisioningStatus",
           evidence="provisioning request id", rollback=ROOT, needs_assignment=False),
        _s(17, "poll_provisioning_to_terminal", VERIFIER,
           "sso:DescribePermissionSetProvisioningStatus", BOOTSTRAP_PS, timeout=600,
           depends_on=["provision_permission_set"], readback="terminal SUCCEEDED or FAILED",
           evidence="terminal status", needs_assignment=False,
           note="NOT fire-and-forget: a provisioning call without a poll has no result"),
        _s(18, "verify_reserved_role_exists", VERIFIER, "iam:GetRole",
           verifier.RESERVED_SSO_ROLE_GLOB, depends_on=["poll_provisioning_to_terminal"],
           readback="role present", evidence="reserved role ARN", needs_assignment=False,
           note="the materialized role lives under aws-reserved/sso.amazonaws.com/<region>/ "
                "— Gate 4N-I15 named a bare AWSReservedSSO_* string that is not an ARN"),

        _s(19, "operator_sso_login", BOOTSTRAP, None, None,
           depends_on=["verify_reserved_role_exists"], evidence="session start time",
           expiry_dependent=True,
           note="session duration bounds the window; the policy expiry bounds it harder"),
        _s(20, "verify_caller_identity", BOOTSTRAP, "sts:GetCallerIdentity", "*",
           depends_on=["operator_sso_login"],
           readback="assumed-role ARN matches the reserved role",
           evidence="caller identity", expiry_dependent=True),
        _s(21, "prepare_executor_manifest", LEAD, None, None,
           depends_on=["verify_caller_identity"], evidence="manifest hash",
           needs_assignment=False,
           note="local only; binds candidate id, trust hashes, boundary ARN and expiry"),
        _s(22, "verify_manifest_binding", LEAD, None, None,
           depends_on=["prepare_executor_manifest"],
           readback="hash comparison against the frozen candidate",
           evidence="binding record", needs_assignment=False),
    ]

    # A gate node so the verifier's identity check is a real dependency of its first use
    # without creating a cycle against the bootstrap operator's own identity step.
    out.insert(9, _s(9.5, "verify_caller_identity_gate", VERIFIER, None, None,
                     depends_on=["verifier_verify_caller_identity"],
                     evidence="verifier readiness attestation", needs_assignment=False,
                     note="marks the verifier as live before any step depends on its reads"))

    seq = 23
    prev = "verify_manifest_binding"
    for i, role in enumerate(ROLE_ARNS, start=1):
        out.append(_s(seq, f"bootstrap_role_{i}", BOOTSTRAP, "iam:CreateRole", role,
                      mutation=True, depends_on=[prev], readback="iam:GetRole",
                      evidence=f"role {i} executor result", timeout=30, rollback=BOOTSTRAP,
                      expiry_dependent=True,
                      note="scripts/role_bootstrap_executor.py; bounded, one role per call"))
        seq += 1
        out.append(_s(seq, f"verify_role_{i}", VERIFIER, "iam:GetRole", role,
                      depends_on=[f"bootstrap_role_{i}"],
                      readback="trust canonical hash AND permissions boundary",
                      evidence=f"role {i} trust + boundary record", expiry_dependent=True))
        prev = f"verify_role_{i}"
        seq += 1
        # Rollback is modelled for EVERY role, not just the first (Gate 4N-I15 A13).
        out.append(_s(seq, f"mismatch_rollback_{i}", BOOTSTRAP, "iam:DeleteRole", role,
                      mutation=True, depends_on=[f"bootstrap_role_{i}"], timeout=30,
                      rollback=BOOTSTRAP, readback="iam:GetRole returns NoSuchEntity",
                      evidence=f"role {i} deletion verification", expiry_dependent=True,
                      note="conditional path; the executor performs it inline on mismatch"))
        seq += 1

    out += [
        _s(seq, "verify_expected_role_set", VERIFIER, "iam:ListRoles", "*",
           depends_on=[prev], readback="exactly the three expected roles, no extras",
           evidence="role inventory"),
        _s(seq + 1, "remove_account_assignment", ROOT, "sso:DeleteAccountAssignment",
           BOOTSTRAP_PS, mutation=True, depends_on=["verify_expected_role_set"],
           readback="sso:DescribeAccountAssignmentDeletionStatus",
           evidence="deletion request id", rollback=ROOT, needs_assignment=False),
        _s(seq + 2, "poll_removal_to_terminal", VERIFIER,
           "sso:DescribeAccountAssignmentDeletionStatus", BOOTSTRAP_PS, timeout=600,
           depends_on=["remove_account_assignment"], readback="terminal SUCCEEDED or FAILED",
           evidence="terminal removal status", needs_assignment=False,
           note="the removal is not done until it is provisioned and polled"),
        _s(seq + 3, "verify_no_assignment_remains", VERIFIER, "sso:ListAccountAssignments",
           BOOTSTRAP_PS, depends_on=["poll_removal_to_terminal"],
           readback="empty for this permission set", evidence="assignment listing",
           needs_assignment=False),
        _s(seq + 4, "verify_new_session_unavailable", VERIFIER, None, None,
           depends_on=["verify_no_assignment_remains"],
           readback="the retired permission set is not offered at SSO login",
           evidence="login surface record", needs_assignment=False,
           note="NEW sessions only. An ALREADY-ISSUED session is NOT invalidated by "
                "assignment removal. The bound that actually holds is the policy expiry: "
                "every Allow carries DateLessThan, so a surviving session is inert after "
                "that instant regardless of session length. SessionDuration is NOT set by "
                "this design and must not be cited as the bound."),
        _s(seq + 5, "operator_sign_out", BOOTSTRAP, None, None,
           depends_on=["verify_new_session_unavailable"], evidence="sign-out attestation",
           retire_after=True, needs_assignment=False,
           note="the bootstrap operator performs NO further step after this"),
        _s(seq + 6, "permission_set_disposition", ROOT, "sso:DeletePermissionSet",
           BOOTSTRAP_PS, mutation=True, depends_on=["operator_sign_out"],
           readback="sso:DescribePermissionSet returns ResourceNotFound",
           evidence="disposition record", rollback=ROOT, needs_assignment=False,
           note="MODEL A: delete after use — see the disposition record"),
        _s(seq + 7, "capture_cloudtrail_evidence", VERIFIER, "cloudtrail:LookupEvents", "*",
           depends_on=["permission_set_disposition"],
           readback="every mutating step above appears", evidence="CloudTrail extract",
           needs_assignment=False),

        # === VERIFIER RETIREMENT (Phase Q) — it is not retired by assertion.
        _s(seq + 8, "verifier_remove_assignment", ROOT, "sso:DeleteAccountAssignment",
           VERIFIER_PS, mutation=True, depends_on=["capture_cloudtrail_evidence"],
           readback="sso:DescribeAccountAssignmentDeletionStatus",
           evidence="verifier deletion request id", rollback=ROOT, needs_assignment=False),
        _s(seq + 9, "verifier_poll_removal", ROOT,
           "sso:DescribeAccountAssignmentDeletionStatus", VERIFIER_PS, timeout=600,
           depends_on=["verifier_remove_assignment"], readback="terminal SUCCEEDED or FAILED",
           evidence="verifier removal terminal status", needs_assignment=False),
        _s(seq + 10, "verifier_sign_out", VERIFIER, None, None,
           depends_on=["verifier_poll_removal"], evidence="verifier sign-out attestation",
           retire_after=True, needs_assignment=False),
        _s(seq + 11, "verifier_disposition", ROOT, "sso:DeletePermissionSet", VERIFIER_PS,
           mutation=True, depends_on=["verifier_sign_out"],
           readback="sso:DescribePermissionSet returns ResourceNotFound",
           evidence="verifier disposition record", rollback=ROOT, needs_assignment=False),
        _s(seq + 12, "root_sign_out", ROOT, None, None,
           depends_on=["verifier_disposition"], evidence="root sign-out attestation",
           retire_after=True, needs_assignment=False),
    ]
    return out


# --- Phase S/T: DAG validation ----------------------------------------------------------


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_dag(graph: list[dict]) -> list[str]:
    """Structural validation. Real cycle detection, not an inference from numbers."""
    problems: list[str] = []
    ids = [s["step_id"] for s in graph]
    seen = set()
    for sid in ids:
        if sid in seen:
            problems.append(f"duplicate step_id {sid!r}")
        seen.add(sid)

    sequences = [s.get("sequence") for s in graph]
    for step in graph:
        value = step.get("sequence")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(f"{step['step_id']}: sequence {value!r} is not a number")
    if len(set(sequences)) != len(sequences):
        duplicates = sorted({x for x in sequences if sequences.count(x) > 1},
                            key=lambda v: str(v))
        problems.append(f"duplicate sequence values: {duplicates}")

    by_id = {s["step_id"]: s for s in graph}
    for step in graph:
        sid = step["step_id"]
        deps = step.get("depends_on") or []
        if len(set(deps)) != len(deps):
            problems.append(f"{sid}: duplicate dependency entries")
        for dep in deps:
            if dep == sid:
                problems.append(f"{sid}: depends on ITSELF")
            elif dep not in by_id:
                problems.append(f"{sid}: depends on {dep!r} which does not exist")

    # Kahn topological sort. If any node remains, the remainder contains a cycle.
    indegree = {s["step_id"]: 0 for s in graph}
    adjacency = {s["step_id"]: [] for s in graph}
    for step in graph:
        for dep in step.get("depends_on") or []:
            if dep in indegree:
                adjacency[dep].append(step["step_id"])
                indegree[step["step_id"]] += 1
    queue = sorted([n for n, d in indegree.items() if d == 0])
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(adjacency[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
        queue.sort()
    if len(order) != len(graph):
        stuck = sorted(set(by_id) - set(order))
        problems.append(f"the graph contains a CYCLE among: {stuck}")
    else:
        # The explicit sequence must agree with SOME valid topological order: a dependency
        # may never carry a higher sequence than the step that depends on it.
        #
        # Guarded on numeric sequences. If a sequence is not a number that is ALREADY a
        # finding above, and comparing it here would raise instead — a validator that dies
        # on a mutation has not detected the mutation.
        for step in graph:
            if not _numeric(step.get("sequence")):
                continue
            for dep in step.get("depends_on") or []:
                if dep in by_id and _numeric(by_id[dep]["sequence"]) \
                        and by_id[dep]["sequence"] > step["sequence"]:
                    problems.append(
                        f"{step['step_id']} (sequence {step['sequence']}) depends on "
                        f"{dep} which has a LATER sequence {by_id[dep]['sequence']}")

    producers: dict[str, list[str]] = {}
    for step in graph:
        if step.get("evidence"):
            producers.setdefault(step["evidence"], []).append(step["step_id"])
    for artifact, owners in sorted(producers.items()):
        if len(owners) > 1:
            problems.append(f"evidence {artifact!r} has MULTIPLE producers: {owners}")

    orphans = [s["step_id"] for s in graph
               if not (s.get("depends_on") or []) and s["step_id"] != "root_session_open"]
    if orphans:
        problems.append(f"steps with no dependency and no entry-point status: {orphans}")

    for step in graph:
        if step["rollback_owner"] and step["rollback_owner"] not in ACTOR_RULES:
            problems.append(f"{step['step_id']}: unknown rollback owner")
        # A rollback owner must be a principal permitted to mutate. Gate 4N-I15 never
        # checked this field against ACTOR_RULES at all, so a rollback could be assigned to
        # the read-only verifier or to the lead and the graph reported clean.
        if step["rollback_owner"] in (VERIFIER, LEAD):
            problems.append(
                f"{step['step_id']}: rollback assigned to {step['rollback_owner']}, which is "
                "prohibited from mutating")
    return problems


# --- Phase P/R: action availability for EVERY non-root actor ----------------------------


def principals_in_graph(graph: list[dict]) -> dict:
    """DISCOVERED from the graph. No hand-maintained list, no exclusion by name."""
    owners: dict[str, dict] = {}
    for step in graph:
        entry = owners.setdefault(step["owner"], {"steps": 0, "actions": set(),
                                                  "mutations": 0})
        entry["steps"] += 1
        if step["action"]:
            entry["actions"].add(step["action"])
        if step["is_mutation"]:
            entry["mutations"] += 1
    return owners


def action_availability() -> list[dict]:
    """Evaluate EVERY action-bearing step against its owner's actual generated policy.

    The Gate 4N-I15 version read:
        if step["action"] is None or step["owner"] in (ROOT, LEAD, VERIFIER): continue
    while its docstring claimed to cover every non-root step. Only ROOT is exempt now, and
    only because the root console is not policy-governed; every other owner that holds an
    action must have a registered policy or the step is reported UNRESOLVED.
    """
    cache: dict[str, dict] = {}
    rows = []
    for step in steps():
        if step["action"] is None:
            continue
        owner = step["owner"]
        rule = ACTOR_RULES.get(owner)
        if rule is None:
            rows.append({"step_id": step["step_id"], "owner": owner,
                         "action": step["action"], "resource": step["resource"],
                         "decision": "NO_SUCH_ACTOR", "available": False,
                         "supporting_sids": []})
            continue
        if rule["root_gated"]:
            rows.append({"step_id": step["step_id"], "owner": owner,
                         "action": step["action"], "resource": step["resource"],
                         "decision": "ROOT_CONSOLE_NOT_POLICY_GOVERNED", "available": True,
                         "supporting_sids": []})
            continue
        if rule["policy"] is None:
            # LEAD holds no AWS action at all; reaching here means the graph gave it one.
            rows.append({"step_id": step["step_id"], "owner": owner,
                         "action": step["action"], "resource": step["resource"],
                         "decision": "OWNER_HAS_NO_POLICY", "available": False,
                         "supporting_sids": []})
            continue
        if owner not in cache:
            cache[owner] = rule["policy"]()
        ctx = dict(IN_WINDOW)
        if step["action"] == "iam:CreateRole":
            ctx["iam:PermissionsBoundary"] = identity.BOUNDARY_POLICY_ARN
        result = iam_eval.decide(cache[owner], step["action"], step["resource"], ctx)
        rows.append({
            "step_id": step["step_id"], "owner": owner, "action": step["action"],
            "resource": step["resource"], "decision": result.decision.name,
            "available": result.decision is Decision.EXPLICIT_ALLOW,
            "supporting_sids": list(result.matching_allow_sids),
        })
    return rows


def validate() -> dict:
    graph = steps()
    by_id = {s["step_id"]: s for s in graph}
    problems: list[str] = validate_dag(graph)

    retired_at = {}
    for step in graph:
        if step["retires_principal_after"]:
            retired_at.setdefault(step["owner"], step["sequence"])

    for step in graph:
        sid = step["step_id"]
        if not step["owner"]:
            problems.append(f"{sid}: OWNERLESS")
        if step["owner"] not in ACTOR_RULES:
            problems.append(f"{sid}: unknown actor class {step['owner']!r}")
        if step["is_mutation"]:
            for field, label in (("action", "no action"), ("resource", "no resource"),
                                 ("rollback_owner", "NO rollback owner"),
                                 ("read_back", "no read-back")):
                if not step[field]:
                    problems.append(f"{sid}: mutation with {label}")
        if not step["evidence"]:
            problems.append(f"{sid}: no evidence artifact")
        cutoff = retired_at.get(step["owner"])
        if cutoff is not None and _numeric(cutoff) and _numeric(step["sequence"]) \
                and step["sequence"] > cutoff:
            problems.append(f"{sid}: {step['owner']} acts AFTER it was retired at "
                            f"sequence {cutoff}")
        if step["action"] in IDENTITY_CENTRE_ADMIN_ACTIONS and step["owner"] != ROOT:
            problems.append(
                f"{sid}: Identity Center ADMINISTRATION action {step['action']} assigned to "
                f"{step['owner']} — that principal must not hold it")
        if step["owner"] == VERIFIER and step["is_mutation"]:
            problems.append(f"{sid}: the read-only verifier owns a MUTATION")
        if step["owner"] == LEAD and step["action"]:
            problems.append(f"{sid}: the lead coordinator owns an AWS action")
        removal = by_id.get("remove_account_assignment")
        if removal and step["requires_assignment"] and _numeric(step["sequence"]) \
                and _numeric(removal["sequence"]) and step["sequence"] > removal["sequence"]:
            problems.append(f"{sid}: needs the assignment but runs after its removal")
        if step["is_mutation"] and step["owner"] != ROOT and not step["timeout_seconds"]:
            problems.append(f"{sid}: temporary-principal mutation with no timeout")

    # Every async Identity Center initiator must have a bounded poll. DERIVED, not a
    # hardcoded pair list — Gate 4N-I15 hardcoded two pairs and the structurally identical
    # third (CreateAccountAssignment) went unpolled with no finding.
    ASYNC_INITIATORS = {
        "sso:ProvisionPermissionSet": "sso:DescribePermissionSetProvisioningStatus",
        "sso:CreateAccountAssignment": "sso:DescribeAccountAssignmentCreationStatus",
        "sso:DeleteAccountAssignment": "sso:DescribeAccountAssignmentDeletionStatus",
    }
    for step in graph:
        poller_action = ASYNC_INITIATORS.get(step["action"] or "")
        if not poller_action:
            continue
        pollers = [s for s in graph if s["action"] == poller_action
                   and step["step_id"] in (s.get("depends_on") or [])
                   and s["resource"] == step["resource"]]
        if not pollers:
            problems.append(f"{step['step_id']}: async {step['action']} with NO terminal-"
                            f"status poll depending on it")
        elif not any(p["timeout_seconds"] for p in pollers):
            problems.append(f"{step['step_id']}: its poll has no bounded timeout")

    for required in ("verify_new_session_unavailable", "operator_sign_out", "root_sign_out",
                     "capture_cloudtrail_evidence", "verify_no_assignment_remains",
                     "remove_account_assignment", "provision_permission_set",
                     "verify_caller_identity", "permission_set_disposition",
                     "verify_no_managed_policy_attachments", "verifier_sign_out",
                     "verifier_disposition"):
        if required not in by_id:
            problems.append(f"missing mandatory step {required}")

    # Every non-root principal holding an AWS action must have a policy AND a retirement.
    owners = principals_in_graph(graph)
    for owner, info in sorted(owners.items(), key=lambda kv: str(kv[0])):
        rule = ACTOR_RULES.get(owner)
        if rule is None or rule["root_gated"]:
            continue
        if info["actions"] and rule["policy"] is None:
            problems.append(f"{owner} holds {len(info['actions'])} AWS actions but has NO "
                            "registered policy")
        if info["actions"] and owner not in retired_at:
            problems.append(f"{owner} holds AWS actions but is NEVER retired")

    availability = action_availability()
    for row in availability:
        if not row["available"]:
            problems.append(
                f"{row['step_id']}: {row['owner']} is assigned {row['action']} but its policy "
                f"returns {row['decision']} — the step cannot execute")

    return {
        "steps": len(graph),
        "principals": {str(k): {"steps": v["steps"], "actions": sorted(v["actions"]),
                                "mutations": v["mutations"]}
                       for k, v in sorted(owners.items(), key=lambda kv: str(kv[0]))},
        "invariants": {
            "ownerless_steps": sum(1 for s in graph if not s["owner"]),
            "mutations_without_rollback_owner":
                sum(1 for s in graph if s["is_mutation"] and not s["rollback_owner"]),
            "mutations_without_readback":
                sum(1 for s in graph if s["is_mutation"] and not s["read_back"]),
            "steps_without_evidence": sum(1 for s in graph if not s["evidence"]),
            "unavailable_actions": sum(1 for r in availability if not r["available"]),
            "non_root_principals_without_a_policy": sum(
                1 for o, i in owners.items()
                if i["actions"] and ACTOR_RULES.get(o) and not ACTOR_RULES[o]["root_gated"]
                and ACTOR_RULES[o]["policy"] is None),
            "dag_problems": len(validate_dag(graph)),
        },
        "action_availability": availability,
        "problems": problems,
        "clean": not problems,
    }


def graph_hash() -> str:
    """Production hash. The canonical FORM is defined in lifecycle_canonical, which
    implements it independently so a test can disagree with this function."""
    return hashlib.sha256(lifecycle_canonical.canonical_bytes(steps())).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = validate()
    if args.json:
        print(json.dumps({**result, "steps_detail": steps(),
                          "graph_sha256": graph_hash()}, indent=2, ensure_ascii=True))
    else:
        print(f"  steps {result['steps']}  {result['invariants']}")
        for owner, info in result["principals"].items():
            print(f"    {owner:24s} steps={info['steps']:3d} actions={len(info['actions']):2d} "
                  f"mutations={info['mutations']}")
        for problem in result["problems"]:
            print(f"    {problem}", file=sys.stderr)
        print("LIFECYCLE GRAPH: clean" if result["clean"] else "LIFECYCLE GRAPH: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
