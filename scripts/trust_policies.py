#!/usr/bin/env python3
"""Exact trust-policy documents for every role that may be created (Gate 4N-I9, Defect 1).

THE DEFECT. `iam:CreateRole` accepts the AssumeRolePolicyDocument in the request, and AWS
provides NO condition key that compares the whole submitted trust document to an approved
hash. So a principal holding CreateRole for an approved role NAME can still create that role
with an attacker-chosen trust policy — an external account, a wildcard principal, a widened
OIDC subject — and the role and its trust SURVIVE the operator's own expiry. Exact role-name
scoping, `iam:PermissionsBoundary` conditioning and policy-name scoping do not touch this:
they constrain what the role may DO, never who may ASSUME it.

WHAT THIS FILE IS. The exact bytes. Because IAM cannot enforce them at the API, the
enforcement is operational and is stated as such rather than dressed up:

  1. Stage-A holds no iam:CreateRole at all (Gate 4N-I9 Phase C).
  2. A separate, minimal RoleBootstrapOperator creates exactly these roles.
  3. It is handed THESE FILES, by hash.
  4. After creation, GetRole is read back, the returned document canonicalized, and compared
     to the hash below.
  5. On any mismatch the role is deleted immediately.

Step 4 is the real control. Anyone reviewing this design should read it as
"detect-and-revert", not "prevent" — AWS does not offer prevent here.

Usage:
    python3 scripts/trust_policies.py [--hash]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from signalnest_identity import (  # noqa: E402
    ACCOUNT, GITHUB_OIDC_PROVIDER_ARN, PREFIX, REVISION_READER_ROLE_NAMES, iam_role_arn,
)

# The GitHub OIDC provider ARN is owned by the authoritative resource layer (Gate 4N-I10
# Defect 5). This module imports it; reconstructing it here is a static-audit failure.

# The repository whose workflows may assume the CI roles. Derived from the git remote by
# scripts/trust_validator.py INDEPENDENTLY; stated here as the generator's input.
GITHUB_REPOSITORY = "bolade04/signal_nest"

ECS_TASKS_SERVICE_PRINCIPAL = "ecs-tasks.amazonaws.com"
OIDC_AUDIENCE = "sts.amazonaws.com"

# Deployment environments gate each CI role. The environment segment is what stops any
# workflow in the repository from assuming these roles: only a job running in the named
# environment receives a token with this `sub`.
PUBLISHER_ENVIRONMENT = "staging-reader-publish"
RUNNER_ENVIRONMENT = "staging-reader-run"


def ecs_tasks_trust() -> dict:
    """Service trust for a task role.

    `aws:SourceAccount` is the confused-deputy guard: without it, ECS in ANY account could
    be induced to assume this role.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "EcsTasksInThisAccountOnly",
            "Effect": "Allow",
            "Principal": {"Service": ECS_TASKS_SERVICE_PRINCIPAL},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
        }],
    }


def oidc_trust(environment: str) -> dict:
    """GitHub OIDC trust pinned to one repository AND one deployment environment.

    Both conditions are StringEquals, never StringLike. A `sub` written with a wildcard —
    `repo:owner/name:*` — would let any branch, any pull request from a fork, and any
    environment assume the role. That is mutation 4 in the trust mutation matrix.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "GitHubOidcExactRepositoryAndEnvironment",
            "Effect": "Allow",
            "Principal": {"Federated": GITHUB_OIDC_PROVIDER_ARN},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": OIDC_AUDIENCE,
                    "token.actions.githubusercontent.com:sub":
                        f"repo:{GITHUB_REPOSITORY}:environment:{environment}",
                },
            },
        }],
    }


# role name -> (trust document, purpose). The purpose is what the independent validator
# checks the document against; it is not a comment.
# Keyed off the AUTHORITATIVE role names, not rebuilt here. REVISION_READER_ROLE_NAMES is
# ordered (publisher, execution, runner); the mapping below is explicit rather than
# positional so a reordering there cannot silently swap two roles' trust documents.
_BY_SUFFIX = {n.rsplit("-", 1)[-1]: n for n in REVISION_READER_ROLE_NAMES}

ROLE_TRUST = {
    _BY_SUFFIX["execution"]: (
        ecs_tasks_trust(),
        "ECS task execution role: assumed by the ECS service in THIS account only"),
    _BY_SUFFIX["publisher"]: (
        oidc_trust(PUBLISHER_ENVIRONMENT),
        "CI publisher: assumed by GitHub Actions from one repository in one environment"),
    _BY_SUFFIX["runner"]: (
        oidc_trust(RUNNER_ENVIRONMENT),
        "CI runner: assumed by GitHub Actions from one repository in one environment"),
}


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def rendered(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def trust_manifest() -> dict:
    out = {}
    for role_name, (doc, purpose) in sorted(ROLE_TRUST.items()):
        out[role_name] = {
            "role_name": role_name,
            "role_arn_expectation": iam_role_arn(role_name),
            "purpose": purpose,
            "trust_policy": doc,
            "canonical_sha256": hashlib.sha256(canonical(doc)).hexdigest(),
            "file_byte_sha256": hashlib.sha256(rendered(doc).encode("utf-8")).hexdigest(),
            "permissions_boundary_expectation":
                "the reviewed boundary policy; supplied at CreateRole",
            "tags_expectation": {"Name": role_name},
            "external_account_prohibited": True,
            "wildcard_principal_prohibited": True,
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hash", action="store_true")
    args = parser.parse_args()
    manifest = trust_manifest()
    if args.hash:
        for role_name, entry in manifest.items():
            print(f"{role_name}")
            print(f"  canonical  {entry['canonical_sha256']}")
            print(f"  file_byte  {entry['file_byte_sha256']}")
        return 0
    sys.stdout.write(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
