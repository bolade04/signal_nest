#!/usr/bin/env python3
"""INDEPENDENT trust-policy validator (Gate 4N-I9, Phase E).

WHY IT MUST BE INDEPENDENT. Gate 4N-I7 Defect 1 and Gate 4N-I8 Defect 2 were the same shape
twice: a checker that took its expected value from the thing it was checking. Falsify the
subject and the expectation moves with it, and the suite stays green. A validator that
imported `trust_policies.ROLE_TRUST` and compared it to itself would repeat that mistake for
the highest-value document in the design — the one saying WHO MAY ASSUME a role.

So this module derives its expectations from three sources, none of which is the trust
generator:

  ANCHOR      ~/.signalnest/anchor/ — the approved account and partition, outside the
              repository, mode 400, built from AWS-signed pre-branch evidence.
  WORKFLOW    the repository's own git remote and CI workflow files — the real GitHub
              identity, read where CI actually gets it.
  PURPOSE     the role-purpose contract below: what each role is FOR, written as allowed
              principal shapes rather than as expected bytes.

It imports `trust_policies` ONLY to obtain the documents under test, never to learn what
they should contain. A test enforces that.

Usage:
    python3 scripts/trust_validator.py [--json]
Exit: 0 iff every trust document is valid against the independent expectation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from external_anchor import AnchorUnavailable, load_anchor  # noqa: E402

SERVICE_ROLE = "service"
OIDC_ROLE = "oidc"

# --- PURPOSE contract -------------------------------------------------------------------
#
# What each role is FOR. Deliberately expressed as a KIND plus the one variable part, not as
# an expected document: a validator holding the expected document is a mirror.
ROLE_PURPOSE = {
    "revision-reader-execution": {
        "kind": SERVICE_ROLE,
        "service_principal": "ecs-tasks.amazonaws.com",
        "why": "the ECS service assumes it to start the reader task",
    },
    "revision-reader-publisher": {
        "kind": OIDC_ROLE,
        "environment": "staging-reader-publish",
        "why": "a CI job in one deployment environment publishes the reader image",
    },
    "revision-reader-runner": {
        "kind": OIDC_ROLE,
        "environment": "staging-reader-run",
        "why": "a CI job in one deployment environment runs the reader task",
    },
}

ALLOWED_SERVICE_PRINCIPALS = {"ecs-tasks.amazonaws.com"}
ALLOWED_OIDC_HOST = "token.actions.githubusercontent.com"
REQUIRED_OIDC_AUDIENCE = "sts.amazonaws.com"


class TrustValidationError(Exception):
    """Raised when the independent expectation cannot be established at all."""


# --- WORKFLOW identity, read from the repository itself ----------------------------------


def _repository_from_terraform() -> str | None:
    """The TRACKED declaration in infra/aws/variables.tf.

    Portable — it travels with the repository — and still independent of the trust
    generator, which is the property that matters. The OpenTofu variable is what actually
    builds the OIDC subjects at apply time, so this is where CI's identity really comes from.
    """
    path = REPO_ROOT / "infra" / "aws" / "variables.tf"
    if not path.exists():
        return None
    block = re.search(r'variable "github_repository" \{(.*?)\n\}',
                      path.read_text(encoding="utf-8"), re.DOTALL)
    if not block:
        return None
    default = re.search(r'default\s*=\s*"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', block.group(1))
    return default.group(1) if default else None


def _repository_from_git_remote() -> str | None:
    """Corroboration only. Returns None when the remote is not a GitHub URL."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def github_repository() -> str:
    """The repository whose workflows may assume the CI roles.

    GATE 4N-I10, found by CLEAN-CHECKOUT VALIDATION. The first version read ONLY the git
    remote. That works in the development tree and in GitHub Actions, and fails in any
    clone-from-local — where `origin` is a file:// path — taking 37 tests down with it. A
    validator whose expectation depends on how the repository was cloned is not deriving an
    expectation, it is reading its environment.

    The tracked OpenTofu variable is the portable source and is the value that actually
    builds the OIDC subjects at apply time. The git remote is kept as CORROBORATION: when it
    is a GitHub URL it must agree, so a repository renamed on GitHub without updating the
    Terraform default is still caught.
    """
    declared = _repository_from_terraform()
    if declared is None:
        raise TrustValidationError(
            "infra/aws/variables.tf declares no github_repository default, so nothing in "
            "the repository states which GitHub identity may assume the CI roles")
    remote = _repository_from_git_remote()
    if remote is not None and remote != declared:
        raise TrustValidationError(
            f"the git remote says {remote!r} but infra/aws/variables.tf says {declared!r}. "
            "One of them is stale, and the OIDC trust subjects are built from the Terraform "
            "value — so the roles would trust the wrong repository.")
    return declared


# --- the checks --------------------------------------------------------------------------


def _principal_problems(principal: object, purpose: dict, anchor: dict) -> list[str]:
    problems: list[str] = []
    if not isinstance(principal, dict):
        return [f"Principal must be an object, got {principal!r} — a bare '*' is a wildcard "
                "principal and would let anyone assume the role"]
    if "*" in principal.values() or principal == "*":
        problems.append("wildcard principal")

    if purpose["kind"] == SERVICE_ROLE:
        if set(principal) != {"Service"}:
            problems.append(f"expected only a Service principal, got keys {sorted(principal)}")
        service = principal.get("Service")
        services = service if isinstance(service, list) else [service]
        for entry in services:
            if entry not in ALLOWED_SERVICE_PRINCIPALS:
                problems.append(f"unauthorized service principal {entry!r}")
    else:
        if set(principal) != {"Federated"}:
            problems.append(f"expected only a Federated principal, got keys {sorted(principal)}")
        federated = principal.get("Federated")
        if not isinstance(federated, str):
            problems.append(f"Federated principal must be a single ARN, got {federated!r}")
        else:
            problems += _federated_arn_problems(federated, anchor)
    return problems


def _federated_arn_problems(arn: str, anchor: dict) -> list[str]:
    """Parse the WHOLE federated ARN, component by component.

    GATE 4N-I11 DEFECT 17. The previous version checked the account digits and treated the
    OIDC host as a SUBSTRING. Swapping the PARTITION —
    arn:aws -> arn:aws-us-gov, account and host unchanged — was ACCEPTED, and the same value
    was absent from external_anchor.account_sensitive_identities(), so both of the design's
    two independent checks missed the same field on the same value. That is the failure shape
    this gate chain has been correcting since 4N-I7, reproduced on the document that decides
    WHO MAY ASSUME a role.
    """
    problems: list[str] = []
    parts = arn.split(":")
    if len(parts) != 6 or parts[0] != "arn":
        return [f"federated principal {arn!r} is not a well-formed 6-segment ARN"]
    partition, service, region, account, resource = parts[1], parts[2], parts[3], parts[4], parts[5]

    if partition != anchor["partition"]:
        problems.append(
            f"OIDC provider partition {partition!r} is not the anchored partition "
            f"{anchor['partition']!r} — a different partition is a different AWS deployment")
    if service != "iam":
        problems.append(f"OIDC provider service {service!r} is not iam")
    if region != "":
        problems.append(f"IAM is global; the region segment must be empty, got {region!r}")
    if account != anchor["approved_account_id"]:
        problems.append(f"OIDC provider is in account ending {account[-4:] if account else '?'}, "
                        "not the anchored account")
    if "*" in arn:
        problems.append(f"wildcard in the federated principal ARN {arn!r}")

    resource_type, _, path = resource.partition("/")
    if resource_type != "oidc-provider":
        problems.append(f"federated principal resource type {resource_type!r} is not "
                        "oidc-provider")
    if path != ALLOWED_OIDC_HOST:
        problems.append(f"OIDC provider path {path!r} is not exactly {ALLOWED_OIDC_HOST!r} "
                        "(a substring check would accept a look-alike host)")
    return problems


def _condition_problems(statement: dict, purpose: dict, repository: str,
                        anchor: dict) -> list[str]:
    problems: list[str] = []
    condition = statement.get("Condition") or {}

    if purpose["kind"] == SERVICE_ROLE:
        source_account = condition.get("StringEquals", {}).get("aws:SourceAccount")
        if not source_account:
            problems.append(
                "missing aws:SourceAccount — without it, the service in ANY account can be "
                "induced to assume this role (confused deputy)")
        elif source_account != anchor["approved_account_id"]:
            # Found by the Phase F mutation matrix: the first draft checked only that the key
            # was PRESENT. A foreign value passes that check while pointing the confused-deputy
            # guard at the attacker's account, which is worse than having no guard at all
            # because it looks correct.
            problems.append(
                f"aws:SourceAccount is not the anchored account (ends {source_account[-4:]})")
        if "StringLike" in condition:
            problems.append("StringLike in a service trust condition permits a wildcard account")
        return problems

    # OIDC. Every check here is a mutation in the Phase F matrix.
    if "StringLike" in condition:
        problems.append(
            "StringLike in an OIDC trust condition — a wildcard sub or aud lets branches, "
            "forks and other environments assume the role. Use StringEquals.")
    equals = condition.get("StringEquals", {})
    if not equals:
        problems.append("no StringEquals condition at all: any GitHub repository could assume")
        return problems

    audience = equals.get(f"{ALLOWED_OIDC_HOST}:aud")
    if audience is None:
        problems.append("missing OIDC audience condition")
    elif audience != REQUIRED_OIDC_AUDIENCE:
        problems.append(f"wrong OIDC audience {audience!r}")

    subject = equals.get(f"{ALLOWED_OIDC_HOST}:sub")
    if subject is None:
        problems.append("missing OIDC subject condition: any repository could assume")
        return problems
    if "*" in str(subject):
        problems.append(f"wildcard in OIDC subject {subject!r}")

    expected_sub = f"repo:{repository}:environment:{purpose['environment']}"
    if subject != expected_sub:
        problems.append(
            f"OIDC subject {subject!r} does not match the repository and environment this "
            f"role is for (expected {expected_sub!r} from the git remote + purpose contract)")
    return problems


def validate_document(role_name: str, document: dict) -> list[str]:
    """Independent validation of ONE trust document."""
    anchor = load_anchor()
    repository = github_repository()

    key = next((k for k in ROLE_PURPOSE if role_name.endswith(k)), None)
    if key is None:
        return [f"{role_name} has no entry in the role-purpose contract, so nothing "
                "independent says what its trust should be"]
    purpose = ROLE_PURPOSE[key]

    problems: list[str] = []
    if document.get("Version") != "2012-10-17":
        problems.append(f"unexpected policy Version {document.get('Version')!r}")
    statements = document.get("Statement")
    if not isinstance(statements, list) or not statements:
        return problems + ["Statement must be a non-empty list"]
    if len(statements) != 1:
        problems.append(
            f"{len(statements)} statements — a trust policy for these roles has exactly one; "
            "an extra statement is how a second principal is smuggled in")

    for statement in statements:
        if statement.get("Effect") != "Allow":
            problems.append(f"unexpected Effect {statement.get('Effect')!r}")
        action = statement.get("Action")
        actions = action if isinstance(action, list) else [action]
        expected_action = ("sts:AssumeRole" if purpose["kind"] == SERVICE_ROLE
                           else "sts:AssumeRoleWithWebIdentity")
        for entry in actions:
            if entry != expected_action:
                problems.append(f"unexpected trust action {entry!r} (expected {expected_action})")
        problems += _principal_problems(statement.get("Principal"), purpose, anchor)
        problems += _condition_problems(statement, purpose, repository, anchor)
    return problems


def validate_all(*, env: dict | None = None) -> dict:
    import trust_policies  # SUBJECT under test only — never an authority on content

    import anchor_loader

    try:
        resolved = anchor_loader.load(anchor_loader.declared_tier(env), env=env)
    except anchor_loader.AnchorError:
        resolved = None
    rows = []
    for role_name, entry in sorted(trust_policies.trust_manifest().items()):
        problems = validate_document(role_name, entry["trust_policy"])
        rows.append({"role": role_name, "valid": not problems, "problems": problems})
    return {
        "repository_from_git_remote": github_repository(),
        "anchor_account_last4": load_anchor()["approved_account_id"][-4:],
        "tier": resolved.tier if resolved else "UNDECLARED",
        "certifies_production": bool(resolved and resolved.certifies_production),
        "rows": rows,
        "invalid": [r for r in rows if not r["valid"]],
        "clean": all(r["valid"] for r in rows),
    }



def _mechanism_probe_rejects_a_foreign_account() -> bool:
    """Rewrite one trust document to a non-approved account and require rejection.

    The probe asserts the injection actually changed the document before believing the
    rejection — a mutation that mutates nothing would otherwise "prove" detection.
    """
    import copy
    import json as _json

    import trust_policies

    approved = load_anchor()["approved_account_id"]
    foreign = "444444444444" if approved != "444444444444" else "555555555555"

    manifest = trust_policies.trust_manifest()
    for role_name, entry in sorted(manifest.items()):
        original = entry["trust_policy"]
        text = _json.dumps(original)
        if approved not in text:
            continue
        tampered = _json.loads(text.replace(approved, foreign))
        if tampered == original:
            continue
        return bool(validate_document(role_name, copy.deepcopy(tampered)))
    return False  # no document carries the account: nothing was exercised


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = validate_all()
    except (AnchorUnavailable, TrustValidationError) as exc:
        print(f"  {exc}", file=sys.stderr)
        print("TRUST VALIDATOR: fail-closed")
        return 2
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for row in result["rows"]:
            print(f"  {'OK  ' if row['valid'] else 'FAIL'} {row['role']}")
            for problem in row["problems"]:
                print(f"        {problem}", file=sys.stderr)
        if not result["certifies_production"]:
            # GATE 4N-I18, SEC-1. This used to say "Tier 1 uses a SYNTHETIC account, so the
            # anchored-account checks are EXPECTED to disagree" and took any invalid row as
            # proof the validator works. That disagreement existed only because the account was
            # a hard-coded literal in the identity layer; once it became tier-resolved the trust
            # documents and the anchor legitimately agreed and this guard declared its own
            # mechanism broken. Incidental disagreement was never evidence of detection.
            # The mechanism is now proven by INJECTING a foreign account into a trust document
            # and requiring the validator to reject it.
            # GATE 4N-I27L. This branch used to `return 0 if detected else 1`, discarding
            # result["clean"] entirely. Gate 4N-I27K narrowed the trust allow-list so a REAL
            # repository role carried an unauthorized service principal: this command printed
            # `FAIL <role>` and `unauthorized service principal`, and exited 0. A graded step
            # named "Trust-policy validator" could not fail on a trust-policy defect.
            # "Certifies nothing about PRODUCTION trust" is a statement about what a synthetic
            # anchor can attest — it was never a licence to ignore what the validator found.
            # Both checks are now mandatory and are combined with AND, never OR.
            print(f"  tier {result['tier']}: MECHANISM CHECK ONLY — certifies nothing about "
                  "production trust")
            detected = _mechanism_probe_rejects_a_foreign_account()
            print(f"  mechanism probe: {'detected the synthetic mismatch' if detected else 'FAILED to detect the synthetic mismatch'}")
            ok = detected and result["clean"]
            # Name what actually failed. "findings" when the mechanism broke and the trust
            # documents were fine would misattribute the failure just as surely as exiting 0
            # misreported it.
            if ok:
                verdict = "mechanism verified"
            elif detected:
                verdict = "findings"
            elif result["clean"]:
                verdict = "mechanism FAILED to detect the synthetic mismatch"
            else:
                verdict = "findings AND mechanism FAILED to detect the synthetic mismatch"
            print(f"TRUST VALIDATOR: {verdict}")
            return 0 if ok else 1
        print("TRUST VALIDATOR: clean" if result["clean"] else "TRUST VALIDATOR: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
