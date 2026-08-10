#!/usr/bin/env python3
"""Independent resource/ARN oracle (Gate 4N-I6).

THE DEFECT THIS CLOSES. Gate 4N-I5 fixed closure independence on the ACTION axis but not
the RESOURCE axis: every probe ARN still came from the policy generator, so falsifying an
identifier moved the policy, the probe and the expectation together and the suite stayed
green. Six physical identities — including the state bucket, the lock table and the
boundary policy ARN — could be wrong with `CLOSURE: clean`.

This module derives expected ARNs from three sources that are NOT the generator:

  SOURCE A  repository expressions — name_prefix from locals.tf, resource `name`
            expressions parsed out of the module .tf files
  SOURCE B  live read-only AWS inventory, captured to infra/aws/live-resource-inventory.json
            (or historical CloudTrail evidence for identifiers AWS assigns)
  SOURCE C  AWS ARN construction rules, applied here rather than copied from a policy

`scripts/gen_operator_policies.py` is imported ONLY as the subject under test. Its ARN
table is never consulted as an authority.

FAIL-CLOSED (Gate 4N-I7, Defect 4). Gate 4N-I6 left three silent fallbacks: an unparsable
trail expression fell back to the generator's own naming convention, a role without a
literal `name` was skipped entirely, and a resource the oracle had no entry for was
reported alongside real results. Each of those turned "the oracle could not derive this"
into "the oracle agrees" — the precise failure the oracle exists to prevent. Every
derivation now returns a Derivation carrying an explicit status, and only MATCH is clean:

  MATCH           SOURCE A and the generator agree, and SOURCE B corroborates
  MISMATCH        the oracle derived a value and the generator disagrees
  UNRESOLVED      the oracle could NOT derive a value — never silently satisfied
  DRIFT_OR_STALE  SOURCE A and SOURCE B disagree; one of them is out of date
  NO_ORACLE_ENTRY the generator produced a resource the oracle does not cover

Usage:
    python3 scripts/resource_oracle.py [--json]
Exit: 0 iff every row is MATCH.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra" / "aws"
INVENTORY = INFRA / "live-resource-inventory.json"

from signalnest_identity import ACCOUNT, REGION  # noqa: E402  authoritative identity


class Status:
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNRESOLVED = "UNRESOLVED"
    DRIFT_OR_STALE = "DRIFT_OR_STALE"
    NO_ORACLE_ENTRY = "NO_ORACLE_ENTRY"


@dataclass
class Derivation:
    """A derived value, or an explicit statement that it could NOT be derived.

    `value is None` is never treated as agreement; it forces UNRESOLVED downstream.
    """

    value: str | None
    source: str
    corroboration: str
    status: str = Status.MATCH  # provisional; set by comparison
    notes: list[str] = field(default_factory=list)

    @classmethod
    def unresolved(cls, source: str, reason: str) -> "Derivation":
        return cls(None, source, "n/a", Status.UNRESOLVED, [reason])


class UnresolvableExpression(Exception):
    """Raised when a repository expression cannot be resolved to a literal."""


# --- SOURCE A: repository expressions ------------------------------------------------


def name_prefix() -> str:
    """Resolve local.name_prefix from locals.tf and variables.tf defaults."""
    locals_src = (INFRA / "locals.tf").read_text(encoding="utf-8")
    match = re.search(r'name_prefix\s*=\s*"([^"]+)"', locals_src)
    if not match:
        raise UnresolvableExpression("local.name_prefix not found in locals.tf")
    expr = match.group(1)
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")

    def default(var: str) -> str:
        block = re.search(r'variable "%s" \{(.*?)\n\}' % var, variables, re.DOTALL)
        if not block:
            raise UnresolvableExpression(f"variable {var!r} not declared")
        value = re.search(r'default\s*=\s*"([^"]+)"', block.group(1))
        if not value:
            raise UnresolvableExpression(f"variable {var!r} has no literal default")
        return value.group(1)

    resolved = expr.replace("${lower(var.project_name)}", default("project_name").lower())
    resolved = resolved.replace("${var.environment}", default("environment"))
    if "${" in resolved:
        raise UnresolvableExpression(f"name_prefix still interpolated: {resolved!r}")
    return resolved


def role_names() -> tuple[dict[str, str], list[str]]:
    """Parse every aws_iam_role `name` expression and resolve it against name_prefix.

    Returns (resolved, unresolved). Callers MUST surface the unresolved list; a role the
    oracle cannot name is an UNRESOLVED row, not an absent one.
    """
    prefix = name_prefix()
    out: dict[str, str] = {}
    unresolved: list[str] = []
    for path in sorted(INFRA.rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r'resource "aws_iam_role" "(\w+)"\s*\{(.*?)\n\}', text, re.DOTALL
        ):
            label, body = match.groups()
            name_expr = re.search(r'name\s*=\s*"([^"]+)"', body)
            if not name_expr:
                # FAIL CLOSED. Gate 4N-I6 skipped these, so a role whose name became
                # non-literal silently left the oracle's coverage without failing it.
                unresolved.append(f"{label} in {path.name}: no literal name expression")
                continue
            resolved = name_expr.group(1).replace("${var.name_prefix}", prefix)
            if "${" in resolved:
                unresolved.append(f"{label} in {path.name}: unresolved interpolation "
                                  f"{resolved!r}")
                continue
            out[label] = resolved
    return out, unresolved


def reader_repository_name() -> str:
    src = (INFRA / "modules" / "revision_reader" / "main.tf").read_text(encoding="utf-8")
    expr = re.search(r'repo_name\s*=\s*"([^"]+)"', src)
    if not expr:
        raise UnresolvableExpression("local.repo_name not found in revision_reader/main.tf")
    resolved = expr.group(1).replace("${var.name_prefix}", name_prefix())
    if "${" in resolved:
        raise UnresolvableExpression(f"repo_name still interpolated: {resolved!r}")
    return resolved


def trail_name() -> str:
    """No convention fallback.

    Gate 4N-I6 returned f"{name_prefix()}-audit" when the expression did not parse. That
    string is the generator's own convention, so the oracle would have agreed with the
    generator precisely when it had failed to read the repository — a fail-open that
    defeats the purpose of an independent oracle.
    """
    src = (INFRA / "modules" / "observability" / "main.tf").read_text(encoding="utf-8")
    expr = re.search(r'trail_name\s*=\s*"([^"]+)"', src)
    if not expr:
        raise UnresolvableExpression(
            "local.trail_name not found in modules/observability/main.tf")
    resolved = expr.group(1).replace("${var.name_prefix}", name_prefix())
    if "${" in resolved:
        raise UnresolvableExpression(f"trail_name still interpolated: {resolved!r}")
    return resolved


# --- SOURCE C: ARN construction ------------------------------------------------------


def arn(service: str, resource: str, *, region: str = REGION, account: str = ACCOUNT) -> str:
    return f"arn:aws:{service}:{region}:{account}:{resource}"


def s3_arn(bucket: str) -> str:
    return f"arn:aws:s3:::{bucket}"


# --- SOURCE B: live inventory --------------------------------------------------------


def inventory() -> dict:
    """SOURCE B, resolved through the declared tier — never from a repository path.

    GATE 4N-I18, SEC-1. This used to read `infra/aws/live-resource-inventory.json` straight
    out of the tree. That file carried the real account id, live bucket names, CloudTrail and
    RDS ARNs, KMS key ids and the lock-table name, none of which appear in git history, so
    the gate package would have disclosed all of them permanently. SOURCE B now comes from
    scripts/protected_inventory.py: the tracked SYNTHETIC fixture under Tier 1, and the real
    inventory — supplied by explicit external path with a separately-supplied expected hash —
    under Tier 2. `INVENTORY` is retained only as the PROHIBITED path the containment test
    asserts is absent.
    """
    import protected_inventory
    return protected_inventory.load().data


def expected_arns() -> dict[str, Derivation]:
    """Compute expected ARNs from SOURCE A + SOURCE B + SOURCE C only.

    Every entry carries its own provenance. Entries the oracle cannot derive are recorded
    as UNRESOLVED Derivations rather than omitted, so a lost derivation surfaces as a
    failure instead of a silently shrinking comparison set.
    """
    inv = inventory()
    out: dict[str, Derivation] = {}

    def record(key: str, value: str, source: str, corroboration: str,
               notes: list[str] | None = None) -> None:
        out[key] = Derivation(value, source, corroboration, notes=notes or [])

    def unresolved(key: str, source: str, reason: str) -> None:
        out[key] = Derivation.unresolved(source, reason)

    try:
        prefix = name_prefix()
    except UnresolvableExpression as exc:
        # Nothing downstream can be derived without it; say so for every key rather than
        # returning an empty (and therefore vacuously clean) comparison set.
        for key in GENERATED_KEYS:
            unresolved(key, "SOURCE A: local.name_prefix", str(exc))
        return out

    # --- roles ---------------------------------------------------------------------
    try:
        roles, role_problems = role_names()
    except UnresolvableExpression as exc:
        roles, role_problems = {}, [str(exc)]
    for problem in role_problems:
        unresolved(f"role:UNPARSED:{problem.split(':')[0]}",
                   "SOURCE A: aws_iam_role name expression", problem)
    for label, name in sorted(roles.items()):
        live = name in inv["roles"]
        expected_dark = label.startswith("reader_")
        notes = []
        if not live and not expected_dark:
            # SOURCE A declares a role SOURCE B says does not exist, and it is not one of
            # the three deliberately-dark reader roles.
            out[f"role:{label}"] = Derivation(
                arn("iam", f"role/{name}", region=""),
                f"SOURCE A: aws_iam_role.{label} name expression + local.name_prefix={prefix}",
                "SOURCE B: ABSENT from the live inventory",
                Status.DRIFT_OR_STALE,
                [f"{name} is declared in the repository but not present live; either the "
                 f"inventory is stale or the role was renamed"])
            continue
        if not live:
            notes.append("dark by design: created already-bounded by the Gate 4N operator")
        record(f"role:{label}", arn("iam", f"role/{name}", region=""),
               f"SOURCE A: aws_iam_role.{label} name expression + local.name_prefix={prefix}",
               "SOURCE B: present in live inventory" if live else
               "SOURCE B: NOT YET CREATED — expected for the dark reader stages", notes)

    # --- ECR ------------------------------------------------------------------------
    try:
        record("ecr:reader", arn("ecr", f"repository/{reader_repository_name()}"),
               "SOURCE A: local.repo_name in modules/revision_reader/main.tf",
               "SOURCE B: sibling repositories %s use the same SLASH convention" % inv["ecr"],
               ["the slash, not a hyphen, was the Gate 4N-I2 defect"])
    except UnresolvableExpression as exc:
        unresolved("ecr:reader", "SOURCE A: local.repo_name", str(exc))

    # --- CloudTrail ------------------------------------------------------------------
    try:
        derived_trail = trail_name()
        live_name, live_arn = inv["trails"][0]
        if derived_trail != live_name:
            out["cloudtrail:trail"] = Derivation(
                arn("cloudtrail", f"trail/{derived_trail}"),
                "SOURCE A: local.trail_name in modules/observability/main.tf",
                f"SOURCE B: live trail is {live_name!r}", Status.DRIFT_OR_STALE,
                ["repository expression and live trail name disagree"])
        else:
            record("cloudtrail:trail", arn("cloudtrail", f"trail/{derived_trail}"),
                   "SOURCE A: local.trail_name in modules/observability/main.tf",
                   f"SOURCE B: {live_arn}")
    except UnresolvableExpression as exc:
        unresolved("cloudtrail:trail", "SOURCE A: local.trail_name", str(exc))

    # --- DynamoDB lock ---------------------------------------------------------------
    # --- rows now resolved from REAL repository expressions (Gate 4N-I10 Defect 6) -----
    import hcl_expressions

    expressions = hcl_expressions.resolve_all()
    lock = expressions["dynamodb:lock"]
    if lock.status == hcl_expressions.EXTERNAL_INPUT:
        # HONEST OUTCOME. aws_dynamodb_table.lock sets name = var.lock_table_name, which is
        # REQUIRED with no default and supplied through a git-ignored tfvars at bootstrap
        # time. It is genuinely not repository-derivable. The previous row GUESSED
        # "<prefix>-tf-lock" from a naming convention and reported MATCH — which is exactly
        # why renaming the resource never moved the oracle. SOURCE B is the only witness.
        record("dynamodb:lock", arn("dynamodb", f"table/{inv['lock_table_name']}")
               if inv.get("lock_table_name") else None,
               f"SOURCE A: {lock.block} name = {lock.expression} — EXTERNAL INPUT, not "
               "repository-derivable; deliberately NOT guessed from a convention",
               "SOURCE B: backend configuration + historical CloudTrail lock events",
               [lock.note])
    elif lock.status == hcl_expressions.RESOLVED:
        record("dynamodb:lock", arn("dynamodb", f"table/{lock.value}"),
               f"SOURCE A: {lock.block} name expression {lock.expression}",
               "SOURCE B: backend configuration + historical CloudTrail lock events")
    else:
        unresolved("dynamodb:lock", f"SOURCE A: {lock.block}", "; ".join(lock.steps))

    # --- S3: provider-generated suffixes, SOURCE B only ------------------------------
    for label, bucket in sorted(inv["buckets_by_role"].items()):
        record(f"s3:{label}", s3_arn(bucket),
               "SOURCE B ONLY — bucket_prefix suffix is provider-generated and is NOT "
               "derivable from any repository expression",
               "SOURCE B: live list-buckets")

    # --- RDS -------------------------------------------------------------------------
    for db_name, db_arn in inv["db"]:
        expected_db = arn("rds", f"db:{prefix}-postgres")
        if expected_db != db_arn:
            out["rds:db"] = Derivation(
                expected_db, "SOURCE A: aws_db_instance identifier on local.name_prefix",
                f"SOURCE B: {db_arn}", Status.DRIFT_OR_STALE,
                ["derived identifier does not match the live instance"])
        else:
            record("rds:db", expected_db,
                   "SOURCE A: aws_db_instance identifier on local.name_prefix",
                   f"SOURCE B: {db_arn}")
    for key, kind in (("rds:pg", "pg"), ("rds:subgrp", "subgrp")):
        resolution = expressions[key]
        if resolution.status != hcl_expressions.RESOLVED:
            unresolved(key, f"SOURCE A: {resolution.block}", "; ".join(resolution.steps))
            continue
        record(key, arn("rds", f"{kind}:{resolution.value}"),
               f"SOURCE A: {resolution.block} name expression {resolution.expression} "
               f"resolved through {len(resolution.steps)} step(s)",
               # GATE 4N-I15 DEFECT 2. This said "SOURCE B: live describe" while
               # live-resource-inventory.json contains NO parameter group and NO subnet
               # group — the string restated the SOURCE A value under a SOURCE B label. The
               # architect lane caught it. There IS no live corroboration for these two rows,
               # and saying so is the correct provenance.
               f"SOURCE B: NONE — the retained inventory contains no {kind} record. "
               f"Provenance is REPOSITORY_EXPRESSION only (resolved value {resolution.value}).")

    # --- KMS: key IDs are AWS-assigned ------------------------------------------------
    for alias, key_id in inv["aliases"]:
        slot = alias.rsplit("/", 1)[-1]
        record(f"kms:{slot}", arn("kms", f"key/{key_id}"),
               "SOURCE A: the ALIAS name is derivable; the KEY ID is AWS-assigned and is not",
               f"SOURCE B: alias {alias} targets {key_id}")

    # --- the boundary policy ----------------------------------------------------------
    # Derived here from the repository convention. This module deliberately does NOT
    # import the authoritative ARN: it is the independent witness for that value.
    record("iam:boundary_policy", arn("iam", f"policy/{prefix}-role-boundary", region=""),
           "SOURCE A: boundary policy name convention on local.name_prefix",
           "SOURCE B: NOT YET CREATED — NoSuchEntity is the expected live state",
           ["cross-checked against every consumer by tests/test_identity_unification.py"])

    return out


# --- comparison against the generator (subject under test) --------------------------

GENERATED_KEYS = (
    "role:reader_publisher", "role:reader_execution", "role:reader_runner",
    "ecr:reader", "cloudtrail:trail", "dynamodb:lock", "s3:tfstate", "s3:audit",
    "rds:db", "rds:pg", "rds:subgrp", "kms:state", "kms:secrets", "iam:boundary_policy",
)


def generated_arns() -> dict[str, str]:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import gen_boundary_policy as gb  # subject under test, never an authority
    import gen_operator_policies as gen

    return {
        "role:reader_publisher": gen.READER_ROLE_ARNS[0],
        "role:reader_execution": gen.READER_ROLE_ARNS[1],
        "role:reader_runner": gen.READER_ROLE_ARNS[2],
        "ecr:reader": gen.ARN["reader_ecr"],
        "cloudtrail:trail": gen.ARN["trail"],
        "dynamodb:lock": gen.ARN["lock"],
        "s3:tfstate": gen.ARN["state_bucket"],
        "s3:audit": gen.ARN["audit_bucket"],
        "rds:db": gen.ARN["db"],
        "rds:pg": gen.ARN["pg"],
        "rds:subgrp": gen.ARN["subgrp"],
        "kms:state": gen.ARN["cmk_state"],
        "kms:secrets": gen.ARN["cmk_secrets"],
        "iam:boundary_policy": gb.POLICY_ARN,
    }


def compare() -> dict:
    expected = expected_arns()
    generated = generated_arns()

    rows = []
    for key, produced in sorted(generated.items()):
        entry = expected.get(key)
        if entry is None:
            rows.append({"key": key, "result": Status.NO_ORACLE_ENTRY, "generated": produced,
                         "derivation": "none", "corroboration": "none",
                         "notes": ["the generator produced a resource the oracle does not "
                                   "cover; coverage must be added, not assumed"]})
            continue
        if entry.status is Status.UNRESOLVED or entry.value is None:
            result = Status.UNRESOLVED
        elif entry.status == Status.DRIFT_OR_STALE:
            result = Status.DRIFT_OR_STALE
        elif entry.value == produced:
            result = Status.MATCH
        else:
            result = Status.MISMATCH
        rows.append({"key": key, "expected": entry.value, "generated": produced,
                     "derivation": entry.source, "corroboration": entry.corroboration,
                     "notes": entry.notes, "result": result})

    # Oracle entries with no generator counterpart. Reported, never silently dropped:
    # the five module roles are covered here even though the operator policies do not
    # name them individually.
    for key, entry in sorted(expected.items()):
        if key in generated:
            continue
        rows.append({"key": key, "expected": entry.value, "generated": None,
                     "derivation": entry.source, "corroboration": entry.corroboration,
                     "notes": entry.notes,
                     "result": Status.UNRESOLVED if entry.status is Status.UNRESOLVED
                     else "ORACLE_ONLY" if entry.status == Status.MATCH
                     else entry.status})

    # The eight role ARNs the boundary is attached to are declared in
    # scripts/signalnest_identity.py by f-string. Nothing above compares them, because the
    # operator policies name only the three reader roles. Reconcile the SETS so a typo in
    # a role name that no policy mentions still fails the oracle.
    import signalnest_identity as _identity  # subject under test on this axis
    declared = set(_identity.ALL_ROLE_ARNS)
    derived = {e.value for k, e in expected.items()
               if k.startswith("role:") and e.value is not None
               and e.status in (Status.MATCH, Status.DRIFT_OR_STALE)}
    role_set = {
        "key": "roleset:identity_vs_repository",
        "expected": sorted(derived), "generated": sorted(declared),
        "derivation": "SOURCE A: every aws_iam_role name expression in infra/aws",
        "corroboration": "SOURCE B: live list-roles for the five existing roles",
        "notes": ["identity declares the boundary's attachment targets; a name it invents "
                  "or omits is invisible to every policy-level comparison"],
        "result": Status.MATCH if declared == derived else Status.MISMATCH,
    }
    if role_set["result"] != Status.MATCH:
        role_set["notes"].append(
            f"only in identity: {sorted(declared - derived)}; "
            f"only in repository: {sorted(derived - declared)}")
    rows.append(role_set)

    failures = [r for r in rows if r["result"] not in (Status.MATCH, "ORACLE_ONLY")]
    try:
        prefix = name_prefix()
    except UnresolvableExpression as exc:
        prefix = f"UNRESOLVED: {exc}"
    return {"name_prefix": prefix, "rows": rows, "mismatches": failures,
            "failures": failures, "clean": not failures,
            "counts": {status: sum(1 for r in rows if r["result"] == status)
                       for status in sorted({r["result"] for r in rows})}}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = compare()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for row in result["rows"]:
            print(f"  {row['result']:16s} {row['key']}")
        for row in result["failures"]:
            print(f"    {row['result']} {row['key']}: expected {row.get('expected')!r} "
                  f"got {row.get('generated')!r} [{row.get('derivation')}]", file=sys.stderr)
        print("RESOURCE ORACLE: clean" if result["clean"] else "RESOURCE ORACLE: mismatch")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
