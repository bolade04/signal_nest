#!/usr/bin/env python3
"""Fail-closed assertion: every IAM role in the composition sets a permissions boundary.

Gate 4N-I3. A role created without a permissions boundary can be granted any policy the
creating principal can write, which is the transitive privilege escalation recorded in
Gates 4N-I1 and 4N-I2. This check makes the boundary structurally mandatory: adding a new
`aws_iam_role` without `permissions_boundary` fails CI rather than silently shipping an
unbounded role.

Deliberately static — it parses the HCL text and needs no `tofu init`, no provider
download, no backend, and no AWS credentials, so it runs in any context.

Exit codes:
  0  every role sets permissions_boundary
  1  one or more roles omit it
  2  the scan found no roles at all (fail closed: a silent glob change must not pass)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = REPO_ROOT / "infra" / "aws"

ROLE_RE = re.compile(r'^resource\s+"aws_iam_role"\s+"([A-Za-z0-9_]+)"\s*\{', re.MULTILINE)
BOUNDARY_ATTR = "permissions_boundary"

# The bootstrap root is a separate stack applied by a different permission set. It is
# scanned too: exempting a directory by default is how an unbounded role slips through.
SCAN_DIRS = [MODULE_ROOT]


def find_role_blocks(text: str) -> list[tuple[str, int, str]]:
    """Return (role_name, line_number, block_body) for each aws_iam_role in the file."""
    blocks: list[tuple[str, int, str]] = []
    for match in ROLE_RE.finditer(text):
        start = match.end()
        depth = 1
        idx = start
        while idx < len(text) and depth:
            char = text[idx]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            idx += 1
        line_no = text.count("\n", 0, match.start()) + 1
        blocks.append((match.group(1), line_no, text[start : idx - 1]))
    return blocks


def main() -> int:
    missing: list[str] = []
    found = 0

    files = sorted(
        path
        for scan_dir in SCAN_DIRS
        for path in scan_dir.rglob("*.tf")
        if ".terraform" not in path.parts
    )

    for path in files:
        text = path.read_text(encoding="utf-8")
        for role_name, line_no, body in find_role_blocks(text):
            found += 1
            rel = path.relative_to(REPO_ROOT)
            if BOUNDARY_ATTR not in body:
                missing.append(f"{rel}:{line_no} aws_iam_role.{role_name}")

    if not found:
        print(
            "FAIL: no aws_iam_role resources found. The scan is misconfigured; "
            "refusing to report success.",
            file=sys.stderr,
        )
        return 2

    if missing:
        print(
            f"FAIL: {len(missing)} of {found} IAM roles do not set {BOUNDARY_ATTR}:",
            file=sys.stderr,
        )
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        print(
            "\nEvery role must set permissions_boundary so a created role cannot exceed "
            "the boundary. Pass var.role_permissions_boundary_arn through the module.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: all {found} IAM roles set {BOUNDARY_ATTR}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
