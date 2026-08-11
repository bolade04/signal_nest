#!/usr/bin/env python3
"""Semantic ARN model (Gate 4N-I12, Defect 4).

THE DEFECT. The critical-ARN audit was a LINE-BASED REGEX SCAN, and the Gate 4N-I10
adversarial lane evaded it in one line:

    SECRETS_CMK = "arn:aws:kms:us-east-1:...:key/" + "548ef" + "eee-..." + "fb"

One wrong final hex digit, split across concatenations. The audit passed 15/15, the suite
passed 933/1, and the shipped boundary then fenced `DenyKmsUseOutsideSecretsCmk` at a
nonexistent key — which, per the Gate 4N-H4 finding that NotResource fences CONFINE rather
than deny, places the REAL secrets CMK inside the Deny and breaks task startup. The external
anchor reported the forged ARN as MATCH because its join only checked account, partition and
role-name prefix.

A regex knows what a string looks like. It does not know what an ARN IS. This module parses
ARNs into components and compares them component by component, so a one-character corruption
in the resource identifier is a MISMATCH on the identifier — not a string that still "looks
like an ARN".
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class MalformedArn(ValueError):
    """Not a parseable ARN. Never silently treated as 'no opinion'."""


@dataclass(frozen=True)
class Arn:
    partition: str
    service: str
    region: str
    account: str
    resource_type: str
    resource_id: str
    separator: str          # "/" or ":" or "" — the form the ARN actually used
    path: tuple[str, ...]   # intermediate path segments, IAM-style
    raw: str

    def components(self) -> dict:
        return {"partition": self.partition, "service": self.service, "region": self.region,
                "account": self.account, "resource_type": self.resource_type,
                "resource_id": self.resource_id, "separator": self.separator,
                "path": list(self.path)}

    def differs_from(self, other: "Arn") -> list[str]:
        """Every component that disagrees, named. Not a boolean."""
        out = []
        for field_name, mine in self.components().items():
            theirs = other.components()[field_name]
            if mine != theirs:
                out.append(f"{field_name}: {mine!r} != {theirs!r}")
        return out


# Services whose resource segment carries no type prefix at all.
_TYPELESS = {"s3"}
# Services that use a colon separator between type and id.
_COLON_SEPARATED = {"secretsmanager", "rds", "cloudwatch", "sns", "sqs", "logs"}


def parse(arn: str) -> Arn:
    """Parse an ARN into components, respecting per-service separator conventions.

    The separator is RECORDED rather than normalised: `repository/name` and
    `repository:name` are different ARNs, and the Gate 4N-I2 reader-ECR defect was exactly a
    slash written as a hyphen. Flattening the form would hide that class.
    """
    if not isinstance(arn, str):
        raise MalformedArn(f"expected a string, got {type(arn).__name__}")
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        raise MalformedArn(f"{arn!r} is not a 6-segment ARN")
    _, partition, service, region, account, resource = parts
    if not partition:
        raise MalformedArn(f"{arn!r} has an empty partition")
    if not service:
        raise MalformedArn(f"{arn!r} has an empty service")

    resource_type, separator, resource_id, path = "", "", resource, ()
    if service in _TYPELESS:
        resource_id = resource
    elif service in _COLON_SEPARATED and ":" in resource:
        resource_type, _, resource_id = resource.partition(":")
        separator = ":"
    elif "/" in resource:
        resource_type, _, rest = resource.partition("/")
        separator = "/"
        segments = rest.split("/")
        resource_id = segments[-1]
        path = tuple(segments[:-1])
    elif ":" in resource:
        resource_type, _, resource_id = resource.partition(":")
        separator = ":"
    return Arn(partition, service, region, account, resource_type, resource_id,
               separator, path, arn)


def compare(expected: str, actual: str) -> dict:
    """Component-wise comparison. A one-character identifier change is a MISMATCH."""
    try:
        left, right = parse(expected), parse(actual)
    except MalformedArn as exc:
        return {"expected": expected, "actual": actual, "result": "MALFORMED",
                "differences": [str(exc)]}
    differences = left.differs_from(right)
    return {"expected": expected, "actual": actual,
            "result": "MATCH" if not differences else "MISMATCH",
            "differences": differences,
            "expected_components": left.components(),
            "actual_components": right.components()}


def critical_arns() -> dict[str, str]:
    """Every critical identity, from the authoritative layer."""
    import signalnest_identity as identity

    return {k: v for k, v in identity.critical_resources().items()
            if isinstance(v, str) and v.startswith("arn:")}


def model() -> dict:
    rows = {}
    for name, arn in sorted(critical_arns().items()):
        try:
            rows[name] = {"arn": arn, "parsed": parse(arn).components(), "result": "PARSED"}
        except MalformedArn as exc:
            rows[name] = {"arn": arn, "result": "MALFORMED", "problem": str(exc)}
    malformed = [k for k, v in rows.items() if v["result"] != "PARSED"]
    return {"count": len(rows), "rows": rows, "malformed": malformed,
            "clean": not malformed}


def main() -> int:
    result = model()
    if "--json" in sys.argv:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for name, row in result["rows"].items():
            print(f"  {row['result']:10s} {name}")
        for name in result["malformed"]:
            print(f"    MALFORMED {name}: {result['rows'][name]['problem']}", file=sys.stderr)
        print("SEMANTIC ARN MODEL: clean" if result["clean"]
              else "SEMANTIC ARN MODEL: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
