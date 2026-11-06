#!/usr/bin/env python3
"""Fail-closed Stage-A permissions-boundary precheck (P6-INF-15).

`infra/aws/operator-closure-contract.json` records the hazard this check exists for:

  stage_a_create_closure._ordering_hazard     OpenTofu creates the two reader ECR resources
                                              before the IAM role, so a role that cannot be
                                              created fails the apply AFTER they exist: a
                                              partial apply.
  stage_a_create_closure._boundary_dependency the role sets permissions_boundary =
                                              var.role_permissions_boundary_arn, so Stage A
                                              REQUIRES that variable to be the exact boundary
                                              ARN and the boundary policy to exist.

The variable validations in infra/aws/variables.tf already refuse the mode/ARN pairings at plan
time. Nothing asserted that the boundary POLICY exists. This precheck asserts, before the
apply and against the saved plan the apply will execute:

  PLAN       role_boundary_mode = "required"; role_permissions_boundary_arn is exactly the
             reviewed boundary ARN for the declared anchor tier; every planned aws_iam_role
             create or update carries that boundary (never unknown at plan time).
  IAM        iam:GetPolicy on that exact ARN returns the policy (name, path, attachable,
             default version); iam:GetPolicyVersion on the default version returns a document
             whose canonical SHA-256 equals the reviewed render of scripts/gen_boundary_policy.py
             (or an explicitly supplied reviewed digest).

Permission class: INVENTORY_READS names the only AWS calls this script makes, for the report.
Both are read-only (scripts/action_classifier.py classifies them READ_ONLY; the tests assert
it and that the client issues exactly these two commands). There is no mutation path: the
client has exactly two public methods, each issuing one literal read.

Fail-closed: exit 0 only when every check passes. Any finding, any read error or denial, a
missing or duplicated key, an unparseable document or an undeclared anchor tier exits 1. A live
run is refused unless SIGNALNEST_ANCHOR_TIER=TIER_2_PROTECTED, so the synthetic tier used by CI
can never reach AWS. Output redacts every 12-digit account id. A failed read is reported only as
its AWS error code and operation, for example `AccessDenied (GetPolicy)`: the AWS CLI's error text
names the caller (role, Identity Center role suffix, session name, usually an e-mail address), so
none of it is kept or printed, and error text in any other form is withheld. A malformed plan
entry (a non-object member, or an aws_iam_role whose change or actions cannot be read) is refused,
never skipped. Unit tests never call AWS: they drive evaluate() and the injectable client with
stubbed responses.

What a PASS does NOT prove: that the apply principal's grants match; that the boundary is
attached to any existing role; anything about P6-INF-10 (PassRole), the other Stage-A gates,
or any state after the reads were made.

Usage (operator, between `tofu plan -out <plan>` and `tofu apply <plan>`):
    tofu show -json <plan> > plan.json
    SIGNALNEST_ANCHOR_TIER=TIER_2_PROTECTED <protected anchor variables> \\
        python3 scripts/boundary_precheck.py --plan-json plan.json --profile <read-only profile> [--json]
    python3 scripts/boundary_precheck.py --plan-json plan.json --fixture inventory.json  # offline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

INVENTORY_READS = ("iam:GetPolicy", "iam:GetPolicyVersion")

PROTECTED_TIER = "TIER_2_PROTECTED"
_ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
# A failed read is reported ONLY as "<AWS error code> (<operation>)". The AWS CLI's error text
# names the caller (role ARN, Identity Center role suffix, session name: usually an e-mail
# address), so none of it is kept. Both patterns admit only [A-Za-z0-9_.] in the code and
# [A-Za-z0-9] in the operation, so neither can carry an identity.
_CLI_ERROR = re.compile(r"An error occurred \(([A-Za-z0-9_.]{1,64})\) when calling the "
                        r"([A-Za-z0-9]{1,64}) operation")
_ERROR_SUMMARY = re.compile(r"[A-Za-z0-9_.]{1,64} \([A-Za-z0-9]{1,64}\)")
_ERROR_KEY = re.compile(r"iam:[A-Za-z]{1,64}")


class PrecheckInputError(RuntimeError):
    """An input that cannot be evaluated. Fail-closed: never a pass."""


class ReadFailed(RuntimeError):
    """A read-only IAM call that failed, was denied, or returned something unusable.

    Its message is always "<Code> (<Operation>)", never AWS CLI text.
    """


def redact(text: str) -> str:
    """Replace every 12-digit account id. This is a public repository's tool."""
    return _ACCOUNT_ID.sub("<account>", text)


def error_summary(text: str) -> str:
    """The only form in which a failed read is reported: "<Code> (<Operation>)".

    Accepts that form, or extracts it from AWS CLI error text. Anything else is withheld, so an
    inventory file carrying raw CLI text cannot put the caller's identity into the output.
    """
    if _ERROR_SUMMARY.fullmatch(text):
        return text
    match = _CLI_ERROR.search(text)
    if match:
        return f"{match.group(1)} ({match.group(2)})"
    return "unrecognised error text withheld"


def _no_duplicate_keys(pairs: list) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise PrecheckInputError(f"duplicate JSON key {key!r}: refusing an ambiguous input")
        seen[key] = value
    return seen


def load_json(path: Path) -> tuple[object, str]:
    """Parse a file and return it with the SHA-256 of the exact bytes parsed."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PrecheckInputError(f"cannot read {path.name}: {type(exc).__name__}") from exc
    try:
        return (json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys),
                hashlib.sha256(raw).hexdigest())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrecheckInputError(f"{path.name} is not valid UTF-8 JSON") from exc


def canonical_sha256(document: dict) -> str:
    """The boundary generator's own canonical form, so the two hashes are comparable."""
    import gen_boundary_policy

    return hashlib.sha256(gen_boundary_policy.canonical(document)).hexdigest()


def expected_identity(document_sha256: str | None = None) -> dict:
    """Everything compared against, from the single identity source. No literal here."""
    import anchor_loader
    import gen_boundary_policy
    import signalnest_identity

    if document_sha256 is not None and not _SHA256_HEX.fullmatch(document_sha256):
        raise PrecheckInputError("--expected-document-sha256 must be 64 lowercase hex characters")
    rendered = canonical_sha256(gen_boundary_policy.boundary_policy())
    return {
        "anchor_tier": anchor_loader.declared_tier(),
        "boundary_policy_arn": signalnest_identity.BOUNDARY_POLICY_ARN,
        "boundary_policy_name": signalnest_identity.BOUNDARY_POLICY_NAME,
        "boundary_policy_path": signalnest_identity.BOUNDARY_POLICY_PATH,
        "document_sha256": document_sha256 or rendered,
        "document_sha256_source": (
            "operator-supplied --expected-document-sha256" if document_sha256 else
            "repository render: scripts/gen_boundary_policy.py, canonical form"),
        "repository_render_sha256": rendered,
    }


def _variable(variables: dict, name: str) -> tuple[bool, object]:
    entry = variables.get(name)
    if not isinstance(entry, dict) or "value" not in entry:
        return False, None
    return True, entry["value"]


def evaluate_plan(plan: object, expected: dict) -> tuple[list[str], dict]:
    """The saved plan the apply will execute (`tofu show -json <plan>`). No AWS access."""
    findings: list[str] = []
    observed: dict = {"role_boundary_mode": None, "boundary_arn_is_reviewed": False,
                      "iam_role_changes_checked": 0}
    if not isinstance(plan, dict):
        return ["the plan JSON is not an object"], observed
    if plan.get("errored") is True:
        findings.append("the saved plan is marked errored; an errored plan is never applied")
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        findings.append("the plan JSON has no resource_changes list: it is not the output of "
                        "`tofu show -json <saved plan>`")
        changes = []
    variables = plan.get("variables")
    if not isinstance(variables, dict):
        findings.append("the plan JSON has no variables block; the boundary inputs cannot be read")
        variables = {}

    present, mode = _variable(variables, "role_boundary_mode")
    observed["role_boundary_mode"] = mode
    if not present:
        findings.append("role_boundary_mode is absent from the plan's variables")
    elif mode != "required":
        findings.append(f"role_boundary_mode is {mode!r}; Stage A requires 'required' "
                        "(stage_a_create_closure._boundary_dependency)")

    present, arn = _variable(variables, "role_permissions_boundary_arn")
    if not present:
        findings.append("role_permissions_boundary_arn is absent from the plan's variables")
    elif arn is None:
        findings.append("role_permissions_boundary_arn is null: the provider sends no boundary, so "
                        "a CreateRole grant conditioned on iam:PermissionsBoundary never matches")
    elif arn != expected["boundary_policy_arn"]:
        findings.append(f"role_permissions_boundary_arn {arn!r} is not the reviewed boundary ARN "
                        f"{expected['boundary_policy_arn']!r} for tier {expected['anchor_tier']}")
    else:
        observed["boundary_arn_is_reviewed"] = True

    _, bootstrap = _variable(variables, "enable_revision_reader_publication_bootstrap")
    observed["enable_revision_reader_publication_bootstrap"] = bootstrap

    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            findings.append(f"resource_changes[{index}] is not an object: refusing an unreadable plan")
            continue
        if change.get("type") != "aws_iam_role" or change.get("mode", "managed") != "managed":
            continue
        address = change.get("address", "<unaddressed aws_iam_role>")
        body = change.get("change")
        actions = body.get("actions") if isinstance(body, dict) else None
        if (not isinstance(actions, list) or not actions
                or not all(isinstance(action, str) for action in actions)):
            # A role entry whose change cannot be read is never skipped: it could be a create.
            findings.append(f"{address}: change/actions unreadable: refusing")
            continue
        if "create" not in actions and "update" not in actions:
            continue
        observed["iam_role_changes_checked"] += 1
        after = body.get("after") if isinstance(body.get("after"), dict) else {}
        unknown = body.get("after_unknown") if isinstance(body.get("after_unknown"), dict) else {}
        if unknown.get("permissions_boundary") is True:
            findings.append(f"{address}: permissions_boundary is unknown at plan time")
        elif after.get("permissions_boundary") != expected["boundary_policy_arn"]:
            findings.append(f"{address}: planned permissions_boundary "
                            f"{after.get('permissions_boundary')!r} is not the reviewed boundary")
    return findings, observed


def _document(raw: object) -> dict | None:
    """GetPolicyVersion returns the document URL-encoded; the AWS CLI hands back a decoded object."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(urllib.parse.unquote(raw), object_pairs_hook=_no_duplicate_keys)
        except (json.JSONDecodeError, PrecheckInputError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def evaluate_inventory(inventory: object, expected: dict) -> tuple[list[str], dict]:
    """The two read-only IAM responses. No AWS access."""
    findings: list[str] = []
    observed: dict = {"policy_exists": False, "default_version_id": None, "document_sha256": None}
    if not isinstance(inventory, dict):
        return ["the IAM inventory is not an object"], observed
    errors = inventory.get("errors") or {}
    if not isinstance(errors, dict):
        findings.append("the IAM inventory's errors field is not an object")
        errors = {}
    for action, message in sorted(errors.items()):
        label = action if _ERROR_KEY.fullmatch(str(action)) else "an unexpected error entry"
        summary = error_summary(str(message))
        if summary.startswith("NoSuchEntity ("):
            findings.append(f"{label}: the boundary policy does not exist (NoSuchEntity); "
                            "Stage A must not be applied")
        else:
            findings.append(f"{label} failed or was denied ({summary}), so existence cannot be "
                            "established")

    policy = inventory.get("policy")
    default_version = None
    if policy is None:
        if not errors:
            findings.append("the IAM inventory carries no policy record")
    elif not isinstance(policy, dict):
        findings.append("the policy record is not an object")
    else:
        if policy.get("Arn") != expected["boundary_policy_arn"]:
            findings.append(f"policy Arn {policy.get('Arn')!r} is not the reviewed boundary ARN")
        if policy.get("PolicyName") != expected["boundary_policy_name"]:
            findings.append(f"policy name {policy.get('PolicyName')!r} is not "
                            f"{expected['boundary_policy_name']!r}")
        if policy.get("Path") != expected["boundary_policy_path"]:
            findings.append(f"policy path {policy.get('Path')!r} is not "
                            f"{expected['boundary_policy_path']!r}")
        if policy.get("IsAttachable") is not True:
            findings.append(f"policy IsAttachable is {policy.get('IsAttachable')!r}, expected True")
        default_version = policy.get("DefaultVersionId")
        if not isinstance(default_version, str) or not default_version:
            findings.append("the policy reports no default version")
            default_version = None
        observed["policy_exists"] = True
        observed["default_version_id"] = default_version

    version = inventory.get("policy_version")
    if version is None:
        if isinstance(policy, dict) and not errors:
            findings.append("the IAM inventory carries no default-version record")
    elif not isinstance(version, dict):
        findings.append("the policy-version record is not an object")
    else:
        if default_version is not None and version.get("VersionId") != default_version:
            findings.append(f"policy version {version.get('VersionId')!r} is not the default "
                            f"version {default_version!r}")
        if version.get("IsDefaultVersion") is not True:
            findings.append("the policy version read is not marked IsDefaultVersion")
        document = _document(version.get("Document"))
        if document is None:
            findings.append("the default version's document is missing or not a JSON object")
        else:
            observed["document_sha256"] = canonical_sha256(document)
            if observed["document_sha256"] != expected["document_sha256"]:
                findings.append(
                    f"default version document sha256 {observed['document_sha256']} != expected "
                    f"{expected['document_sha256']} ({expected['document_sha256_source']})")
    return findings, observed


def evaluate(plan: object, inventory: object, expected: dict) -> dict:
    plan_findings, plan_observed = evaluate_plan(plan, expected)
    iam_findings, iam_observed = evaluate_inventory(inventory, expected)
    findings = plan_findings + iam_findings
    return {
        "check": "P6-INF-15 Stage-A permissions-boundary precheck",
        "inventory_reads": list(INVENTORY_READS),
        "expected": expected,
        "observed": {"plan": plan_observed, "iam": iam_observed},
        "findings": findings,
        "clean": not findings,
    }


class AwsCliIamReader:
    """The default client: exactly two AWS CLI reads, each a literal in its own method.

    Tests inject `runner` (or replace the whole client through main's `client_factory`).
    """

    def __init__(self, profile: str, runner=subprocess.run):
        if not profile:
            raise PrecheckInputError("a named AWS CLI profile is required for a live read")
        self.profile = profile
        self._runner = runner

    def _run(self, operation: str, arguments: list[str]) -> dict:
        command = ["aws", "iam", operation, *arguments,
                   "--profile", self.profile, "--output", "json"]
        api_operation = "".join(word.capitalize() for word in operation.split("-"))
        try:
            proc = self._runner(command, capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadFailed(f"{type(exc).__name__} ({api_operation})") from exc
        if proc.returncode != 0:
            # The CLI's text names the caller; keep only the error code and the operation.
            match = _CLI_ERROR.search(proc.stderr or "")
            if match:
                raise ReadFailed(f"{match.group(1)} ({match.group(2)})")
            exit_label = (f"CliExit{proc.returncode}" if proc.returncode > 0
                          else f"CliSignal{-proc.returncode}")
            raise ReadFailed(f"{exit_label} ({api_operation})")
        try:
            data = json.loads(proc.stdout or "", object_pairs_hook=_no_duplicate_keys)
        except (json.JSONDecodeError, PrecheckInputError) as exc:
            raise ReadFailed(f"MalformedResponse ({api_operation})") from exc
        if not isinstance(data, dict):
            raise ReadFailed(f"MalformedResponse ({api_operation})")
        return data

    def get_policy(self, policy_arn: str) -> dict:
        return self._run("get-policy", ["--policy-arn", policy_arn])

    def get_policy_version(self, policy_arn: str, version_id: str) -> dict:
        return self._run("get-policy-version",
                         ["--policy-arn", policy_arn, "--version-id", version_id])


def collect(client, policy_arn: str) -> dict:
    """Two read-only calls, in order. The second is made only if the first returned a version."""
    inventory: dict = {"policy": None, "policy_version": None, "errors": {}}
    try:
        inventory["policy"] = client.get_policy(policy_arn).get("Policy")
    except ReadFailed as exc:
        inventory["errors"]["iam:GetPolicy"] = str(exc)
        return inventory
    policy = inventory["policy"]
    version_id = policy.get("DefaultVersionId") if isinstance(policy, dict) else None
    if not isinstance(version_id, str) or not version_id:
        return inventory
    try:
        inventory["policy_version"] = \
            client.get_policy_version(policy_arn, version_id).get("PolicyVersion")
    except ReadFailed as exc:
        inventory["errors"]["iam:GetPolicyVersion"] = str(exc)
    return inventory


def _report(result: dict, as_json: bool) -> str:
    if as_json:
        return redact(json.dumps(result, indent=2, sort_keys=True))
    if result["clean"]:
        return redact("BOUNDARY PRECHECK: PASS (plan inputs, policy existence and default-version "
                      f"document {result['observed']['iam']['document_sha256']} all match)")
    return redact("BOUNDARY PRECHECK: FAIL\n" + "\n".join(f"  - {f}" for f in result["findings"]))


def main(argv: list[str] | None = None, *, client_factory=AwsCliIamReader) -> int:
    parser = argparse.ArgumentParser(description="P6-INF-15 Stage-A permissions-boundary precheck")
    parser.add_argument("--plan-json", required=True,
                        help="`tofu show -json <saved plan>` for the plan the apply will execute")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--profile", help="AWS CLI profile for the two read-only IAM reads "
                                          "(operator only; requires the protected anchor tier)")
    source.add_argument("--fixture", help="a recorded IAM inventory, evaluated offline")
    parser.add_argument("--expected-document-sha256", default=None,
                        help="a reviewed canonical digest to compare instead of the repository "
                             "render (it replaces the comparison, never skips it)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        plan, plan_sha256 = load_json(Path(args.plan_json))
        try:
            expected = expected_identity(args.expected_document_sha256)
        except PrecheckInputError:
            raise
        except Exception as exc:  # anchor or inventory resolution: refuse, never default
            raise PrecheckInputError(f"expected identity unavailable: {exc}") from exc
        if args.profile is not None:
            if expected["anchor_tier"] != PROTECTED_TIER:
                raise PrecheckInputError(
                    f"a live read requires SIGNALNEST_ANCHOR_TIER={PROTECTED_TIER}; tier "
                    f"{expected['anchor_tier']} names no real boundary, so no AWS call is made")
            inventory = collect(client_factory(args.profile), expected["boundary_policy_arn"])
        else:
            inventory, _ = load_json(Path(args.fixture))
    except PrecheckInputError as exc:
        print(redact(f"BOUNDARY PRECHECK: FAIL (refused) {exc}"), file=sys.stderr)
        return 1

    result = evaluate(plan, inventory, expected)
    result["inputs"] = {
        "plan_json_sha256": plan_sha256,
        "inventory_source": "live read-only IAM" if args.profile is not None else "fixture",
    }
    print(_report(result, args.json))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
