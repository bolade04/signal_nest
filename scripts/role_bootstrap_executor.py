#!/usr/bin/env python3
"""Bounded RoleBootstrap executor — ONE role per invocation (Gate 4N-I12, Defect 1).

THE DEFECT THIS CLOSES. `iam:CreateRole` accepts the AssumeRolePolicyDocument and AWS
provides no condition key comparing it to an approved hash, so the design is
detect-and-revert. Through Gate 4N-I11 that was all it was: an IAM policy granting
create / read-back / rollback, plus prose asserting the read-back happens "immediately".
Nothing performed the sequence, nothing timed it, and nothing owned it. The Gate 4N-I10
security lane rejected exactly that — "a design whose acceptability rests on a timing
property that is nowhere enforced in code is not a bounded residual risk; it is an unbounded
one dressed as a bounded one."

This file is that property, in code. It performs

    CreateRole -> GetRole -> canonicalize -> compare -> DeleteRole on mismatch

as ONE non-interactive invocation, under a monotonic deadline, and fails closed on every
path. It refuses to continue to a second role after any failure.

WHAT IT STILL IS NOT. It is detect-and-revert, not prevent. Between CreateRole returning and
the comparison completing, an incorrect trust document exists in the account. What changed is
that the window is now MEASURED and BOUNDED rather than asserted: exceed the bound and the
run fails and rolls back, instead of quietly taking as long as it takes.

NO AWS CALL IS MADE BY THIS GATE. The client is injected; tests supply a mock. Running it
against a real client is a future, separately authorized operation.

Usage (future, authorized operator context only):
    python3 scripts/role_bootstrap_executor.py --manifest <path> --role <name>
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import iam_eval  # noqa: E402

UTC = datetime.timezone.utc

# --- PHASE C: the bounds, in seconds -----------------------------------------------------
#
# Monotonic for elapsed time (a wall-clock step or an NTP correction must not widen the
# window); UTC only for the evidence record.
FIRST_READBACK_DEADLINE = 2.0      # CreateRole response -> first GetRole attempt
COMPARISON_DEADLINE = 15.0         # CreateRole response -> comparison concluded
ROLLBACK_START_DEADLINE = 2.0      # mismatch determined -> DeleteRole issued
TOTAL_EXPOSURE_TARGET = 30.0       # CreateRole response -> role confirmed gone
DELETION_VERIFY_DEADLINE = 30.0
READBACK_RETRY_LIMIT = 5
READBACK_RETRY_INTERVAL = 0.25


class ExecutorRefusal(Exception):
    """Refused before any AWS call. Nothing was created."""


class ExposureBoundExceeded(Exception):
    """A timing bound was breached. NEVER downgraded to a warning."""


@dataclass
class Outcome:
    role_name: str
    status: str                       # SUCCESS | MISMATCH_ROLLED_BACK | FAILED
    created: bool = False
    trust_matched: bool | None = None
    rolled_back: bool = False
    deletion_verified: bool = False
    exposure_seconds: float | None = None
    bound_exceeded: bool = False
    timestamps_utc: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {**self.__dict__}


def canonical(document: dict) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def canonical_hash(document: dict) -> str:
    return hashlib.sha256(canonical(document)).hexdigest()


def normalize_returned_trust(raw: object) -> dict:
    """AWS returns AssumeRolePolicyDocument URL-ENCODED in most SDK paths.

    Comparing the encoded form against a canonical hash would mismatch every time and send
    every run down the rollback path — a control that always fires is as useless as one that
    never does.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ExposureBoundExceeded(f"malformed AssumeRolePolicyDocument: {type(raw).__name__}")
    text = raw.strip()
    if not text.startswith("{"):
        text = urllib.parse.unquote(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExposureBoundExceeded(f"AssumeRolePolicyDocument is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExposureBoundExceeded("AssumeRolePolicyDocument is not an object")
    return parsed


# --- PHASE E: manifest binding ------------------------------------------------------------


REQUIRED_MANIFEST_FIELDS = (
    "candidate_id", "expiry_utc", "approved_account_id", "partition", "roles")
REQUIRED_ROLE_FIELDS = (
    "role_name", "trust_policy_path", "canonical_sha256", "file_byte_sha256",
    "boundary_arn", "tags")


def load_manifest(path: Path, *, role_name: str, candidate_id: str | None = None,
                  now: datetime.datetime | None = None) -> dict:
    """Refuse anything the reviewed manifest does not exactly describe."""
    if not path.exists():
        raise ExecutorRefusal(f"manifest not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExecutorRefusal(f"manifest is not valid JSON: {exc}") from exc

    for field_name in REQUIRED_MANIFEST_FIELDS:
        if field_name not in manifest:
            raise ExecutorRefusal(f"manifest is missing {field_name!r}")

    if candidate_id is not None and manifest["candidate_id"] != candidate_id:
        raise ExecutorRefusal(
            f"candidate mismatch: manifest is {manifest['candidate_id']!r}, "
            f"caller expected {candidate_id!r}")

    deadline = iam_eval.parse_iam_date(manifest["expiry_utc"], what="manifest expiry")
    moment = now or datetime.datetime.now(UTC)
    if moment.tzinfo is None:
        raise ExecutorRefusal("refusing a timezone-naive current time")
    if moment >= deadline:
        raise ExecutorRefusal(
            f"manifest expired at {manifest['expiry_utc']}; refusing to create anything")

    entry = next((r for r in manifest["roles"] if r.get("role_name") == role_name), None)
    if entry is None:
        raise ExecutorRefusal(
            f"role {role_name!r} is not in the reviewed manifest — this executor does not "
            "accept arbitrary role names")
    for field_name in REQUIRED_ROLE_FIELDS:
        if field_name not in entry:
            raise ExecutorRefusal(f"manifest role {role_name!r} is missing {field_name!r}")

    boundary = entry["boundary_arn"]
    parts = boundary.split(":")
    if len(parts) < 6 or parts[1] != manifest["partition"]:
        raise ExecutorRefusal(f"boundary ARN partition does not match the manifest: {boundary}")
    if parts[4] != manifest["approved_account_id"]:
        raise ExecutorRefusal("boundary ARN account does not match the approved account")
    if not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", role_name):
        raise ExecutorRefusal(f"role name {role_name!r} is not a valid IAM role name")

    return {"manifest": manifest, "role": entry}


def load_trust_document(entry: dict, *, base: Path) -> dict:
    """Load the reviewed bytes and verify BOTH hashes before anything is created."""
    path = (base / entry["trust_policy_path"]).resolve()
    if not path.exists():
        raise ExecutorRefusal(f"trust policy file not found: {path}")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["file_byte_sha256"]:
        raise ExecutorRefusal(
            f"trust policy file {path.name} does not match its reviewed file-byte hash — "
            "the file was altered after review")
    document = json.loads(raw.decode("utf-8"))
    if canonical_hash(document) != entry["canonical_sha256"]:
        raise ExecutorRefusal(
            f"trust policy {path.name} does not match its reviewed canonical hash")
    return document


# --- PHASE B/C: the bounded sequence -------------------------------------------------------


def execute_one(client, *, manifest_path: Path, role_name: str,
                candidate_id: str | None = None, base: Path | None = None,
                now: datetime.datetime | None = None,
                monotonic=time.monotonic) -> Outcome:
    """CreateRole -> GetRole -> compare -> DeleteRole on mismatch, under a deadline.

    `client` is injected. This gate makes NO AWS call; tests supply a mock.
    """
    base = base or manifest_path.parent
    outcome = Outcome(role_name=role_name, status="FAILED")

    loaded = load_manifest(manifest_path, role_name=role_name,
                           candidate_id=candidate_id, now=now)
    entry = loaded["role"]
    expected = load_trust_document(entry, base=base)
    expected_hash = entry["canonical_sha256"]

    def stamp(label: str) -> None:
        outcome.timestamps_utc[label] = datetime.datetime.now(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    # --- create ---------------------------------------------------------------------------
    stamp("create_requested")
    client.create_role(RoleName=role_name,
                       AssumeRolePolicyDocument=json.dumps(expected),
                       PermissionsBoundary=entry["boundary_arn"],
                       Tags=[{"Key": k, "Value": v} for k, v in sorted(entry["tags"].items())])
    created_at = monotonic()
    outcome.created = True
    stamp("create_returned")

    def elapsed() -> float:
        return monotonic() - created_at

    def rollback(reason: str) -> Outcome:
        outcome.problems.append(reason)
        started = monotonic()
        stamp("rollback_requested")
        try:
            client.delete_role(RoleName=role_name)
            outcome.rolled_back = True
        except Exception as exc:  # noqa: BLE001
            outcome.problems.append(f"DeleteRole FAILED: {exc}")
        if monotonic() - started > ROLLBACK_START_DEADLINE + DELETION_VERIFY_DEADLINE:
            outcome.bound_exceeded = True
            outcome.problems.append("rollback exceeded its deadline")
        # Verify the deletion; "we asked" is not "it is gone".
        try:
            client.get_role(RoleName=role_name)
            outcome.problems.append(
                "role still present after DeleteRole — deletion NOT verified")
        except Exception:  # noqa: BLE001  the expected NoSuchEntity path
            outcome.deletion_verified = True
        stamp("rollback_verified")
        outcome.exposure_seconds = elapsed()
        if outcome.exposure_seconds > TOTAL_EXPOSURE_TARGET:
            outcome.bound_exceeded = True
            outcome.problems.append(
                f"total exposure {outcome.exposure_seconds:.2f}s exceeded the "
                f"{TOTAL_EXPOSURE_TARGET}s target")
        outcome.status = ("MISMATCH_ROLLED_BACK"
                          if outcome.rolled_back and outcome.deletion_verified else "FAILED")
        return outcome

    # --- read back ------------------------------------------------------------------------
    returned = None
    attempts = 0
    first_attempt_at = None
    while attempts < READBACK_RETRY_LIMIT:
        if elapsed() > COMPARISON_DEADLINE:
            outcome.bound_exceeded = True
            return rollback(
                f"comparison deadline {COMPARISON_DEADLINE}s exceeded before a usable "
                f"read-back ({attempts} attempt(s))")
        attempts += 1
        if first_attempt_at is None:
            first_attempt_at = monotonic()
            stamp("first_readback")
            if first_attempt_at - created_at > FIRST_READBACK_DEADLINE:
                outcome.bound_exceeded = True
                return rollback(
                    f"first read-back began {first_attempt_at - created_at:.2f}s after "
                    f"CreateRole, over the {FIRST_READBACK_DEADLINE}s bound")
        try:
            response = client.get_role(RoleName=role_name)
        except Exception as exc:  # noqa: BLE001  eventual consistency, or a real failure
            outcome.problems.append(f"GetRole attempt {attempts}: {exc}")
            time.sleep(READBACK_RETRY_INTERVAL) if READBACK_RETRY_INTERVAL else None
            continue
        raw = (response or {}).get("Role", {}).get("AssumeRolePolicyDocument")
        if raw is None:
            outcome.problems.append(f"GetRole attempt {attempts}: no AssumeRolePolicyDocument")
            continue
        returned = raw
        break

    if returned is None:
        outcome.bound_exceeded = True
        return rollback(f"no usable read-back after {attempts} attempt(s)")

    # --- compare --------------------------------------------------------------------------
    try:
        normalized = normalize_returned_trust(returned)
    except ExposureBoundExceeded as exc:
        return rollback(str(exc))

    stamp("comparison_complete")
    if elapsed() > COMPARISON_DEADLINE:
        outcome.bound_exceeded = True
        return rollback(f"comparison concluded at {elapsed():.2f}s, over the "
                        f"{COMPARISON_DEADLINE}s deadline")

    actual_hash = canonical_hash(normalized)
    outcome.trust_matched = actual_hash == expected_hash
    if not outcome.trust_matched:
        return rollback(
            "returned trust document does not match the reviewed canonical hash")

    outcome.exposure_seconds = elapsed()
    outcome.status = "SUCCESS"
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--role", required=True)
    parser.add_argument("--candidate-id")
    parser.parse_args()
    print("REFUSING TO RUN: this gate authorizes no AWS call. The executor is exercised "
          "only through tests with an injected mock client.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
