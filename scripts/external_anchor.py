#!/usr/bin/env python3
"""Load the EXTERNAL identity anchor and join it against repository derivation (Gate 4N-I8).

THE DEFECT THIS CLOSES (Gate 4N-I7 Defect 1). The boundary ARN was de-duplicated to a single
construction site, and I reported that as closing the defect. The adversarial lane disproved
it: replacing the account across `scripts/signalnest_identity.py`,
`infra/aws/live-resource-inventory.json` and two test literal files made EVERY generated ARN
name a foreign account (111199998888) while the whole suite stayed green. Both of the
supposedly independent sources were repository-controlled, so one `sed` moved the expectation
and the value together. De-duplication is not anchoring.

The anchor lives at ~/.signalnest/anchor/, OUTSIDE the git repository, written once at mode
400 from AWS-signed evidence retained before this feature branch existed. No repository edit
can reach it, so a repository-wide account replacement now produces a MISMATCH.

FAIL-CLOSED IS THE WHOLE POINT. If the anchor is missing, unreadable, malformed, or
disagrees, this module raises. It never falls back to a repository value and callers must
never convert that raise into a skip — a skipped anchor check is indistinguishable from a
passing one, which is the exact failure mode this file exists to prevent.

Usage:
    python3 scripts/external_anchor.py [--json]
Exit: 0 iff every account-sensitive identity agrees with the anchor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# GATE 4N-I13 DEFECT 1. This was Path.home()/".signalnest"/... — invisible on a CI runner
# with an empty HOME, which is why five guard scripts and nine test files failed there while
# the "clean checkout" reported 933 passed by silently reading the developer machine.
#
# The anchor now comes from scripts/anchor_loader.py against an EXPLICITLY DECLARED tier.
# SIGNALNEST_ANCHOR_PATH may still point at retained operator evidence, but it must be
# supplied deliberately: there is no discovery and no fallback.
import anchor_loader  # noqa: E402

ANCHOR_PATH = None  # retained for compatibility; resolution is via anchor_loader


class AnchorUnavailable(Exception):
    """The external anchor could not be loaded. Never downgrade this to a skip."""


class AnchorMismatch(Exception):
    """Repository derivation disagrees with the external anchor."""


def load_anchor(path: Path | None = None, *, tier: str | None = None,
                env: dict | None = None) -> dict:
    # Resolved at CALL time, not definition time. A default of `ANCHOR_PATH` binds the value
    # when the module is imported, so reassigning the module attribute would not change
    # behaviour — the fail-closed path would then be untestable, which is precisely the kind
    # of unexercised safety code this gate exists to eliminate.
    if path is not None:
        # Explicit path supplied by a caller (tests, or an operator naming retained evidence).
        path = Path(path)
        if not path.exists():
            raise AnchorUnavailable(f"external identity anchor missing at {path}")
        try:
            anchor = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AnchorUnavailable(f"anchor at {path} is not valid JSON: {exc}") from exc
    else:
        try:
            resolved = anchor_loader.load(tier or anchor_loader.declared_tier(env), env=env)
        except anchor_loader.AnchorError as exc:
            raise AnchorUnavailable(str(exc)) from exc
        anchor = resolved.anchor

    for field in ("approved_account_id", "partition", "approved_region", "role_name_prefix"):
        if not anchor.get(field):
            raise AnchorUnavailable(f"anchor is missing required field {field!r}")
    if not re.fullmatch(r"\d{12}", anchor["approved_account_id"]):
        raise AnchorUnavailable(
            f"anchor account {anchor['approved_account_id']!r} is not a 12-digit account id")
    return anchor


def anchor_sha256(path: Path | None = None, *, tier: str | None = None,
                  env: dict | None = None) -> str:
    if path is not None:
        path = Path(path)
        if not path.exists():
            raise AnchorUnavailable(f"anchor missing at {path}")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        return anchor_loader.load(tier or anchor_loader.declared_tier(env),
                                  env=env).canonical_sha256
    except anchor_loader.AnchorError as exc:
        raise AnchorUnavailable(str(exc)) from exc


def _account_of(arn: str) -> str | None:
    """The account segment of an ARN, or None for account-less ARNs (e.g. s3 bucket ARNs)."""
    parts = arn.split(":")
    if len(parts) < 6 or parts[0] != "arn":
        return None
    return parts[4] or None


def _partition_of(arn: str) -> str | None:
    parts = arn.split(":")
    return parts[1] if len(parts) > 1 and parts[0] == "arn" else None


def account_sensitive_identities() -> dict[str, str]:
    """Every account-bearing ARN the design generates, gathered from its real producers."""
    import gen_boundary_policy as gb
    import gen_operator_policies as gen
    import signalnest_identity as identity

    out: dict[str, str] = {
        "identity.BOUNDARY_POLICY_ARN": identity.BOUNDARY_POLICY_ARN,
        "boundary_generator.POLICY_ARN": gb.POLICY_ARN,
    }
    for name, arn in zip(identity.ALL_ROLE_NAMES, identity.ALL_ROLE_ARNS):
        out[f"role:{name}"] = arn
    for key in ("trail", "lock", "cmk_state", "cmk_secrets", "db", "pg", "subgrp",
                "reader_ecr", "reader_log_group", "distribution", "oac"):
        out[f"operator.ARN[{key}]"] = gen.ARN[key]
    # Boundary-generator internals. Gate 4N-I7 Defect 7: these were rebuilt inside the
    # generator with no witness at all.
    for attr in ("SECRETS_CMK", "STATE_CMK", "LOCK_TABLE", "READER_EXECUTION_ROLE"):
        if hasattr(gb, attr):
            out[f"boundary_generator.{attr}"] = getattr(gb, attr)
    return out


def join(*, tier: str | None = None, env: dict | None = None) -> dict:
    """Compare every account-sensitive identity against the EXTERNAL anchor.

    TIER SEMANTICS (Gate 4N-I13, corrected by Gate 4N-I27L). Under TIER_2_PROTECTED the
    anchor is the real one and a clean join CERTIFIES that the repository names the approved
    account. A Tier-1 run must never be read as production certification, and
    `certifies_production` below is what says so.

    This docstring used to claim that under TIER_1_SYNTHETIC the join is EXPECTED to
    mismatch, which is what justified discarding the real verdict in `main()`. It is not
    true: the identity layer resolves its account FROM the anchor, so a Tier-1 join is clean
    (executed at I27K: clean=true, 0 mismatches). A Tier-1 mismatch therefore means a real
    identity genuinely disagrees with the anchor, and it must fail the run.
    """
    resolved = None
    if tier is not None or env is not None:
        import anchor_loader as _al
        resolved = _al.load(tier or _al.declared_tier(env), env=env)
        anchor = resolved.anchor
    else:
        anchor = load_anchor(tier=tier, env=env)
        try:
            import anchor_loader as _al
            resolved = _al.load(_al.declared_tier(env), env=env)
        except Exception:  # noqa: BLE001  tier not declared; treat as uncertified
            resolved = None  # resolves ANCHOR_PATH at call time; raises if unavailable
    approved_account = anchor["approved_account_id"]
    approved_partition = anchor["partition"]
    approved_prefix = anchor["role_name_prefix"]

    rows, mismatches = [], []
    for label, arn in sorted(account_sensitive_identities().items()):
        account = _account_of(arn)
        partition = _partition_of(arn)
        problems = []
        if account is not None and account != approved_account:
            problems.append(f"account {account} != anchor {approved_account}")
        if partition is not None and partition != approved_partition:
            problems.append(f"partition {partition} != anchor {approved_partition}")
        if label.startswith("role:") and approved_prefix not in arn:
            problems.append(f"role ARN does not carry the anchored prefix {approved_prefix!r}")
        row = {"identity": label, "arn": arn, "account": account,
               "result": "MATCH" if not problems else "MISMATCH", "problems": problems}
        rows.append(row)
        if problems:
            mismatches.append(row)

    # An account-bearing set that somehow contains no account at all would pass vacuously.
    with_account = [r for r in rows if r["account"] is not None]
    if len(with_account) < 10:
        mismatches.append({"identity": "<coverage>", "result": "MISMATCH",
                           "problems": [f"only {len(with_account)} account-bearing ARNs were "
                                        "joined; the check would be vacuous"]})

    return {
        "anchor_source": "resolved via scripts/anchor_loader.py (explicit tier)",
        "tier": resolved.tier if resolved else "UNDECLARED",
        "certifies_production": bool(resolved and resolved.certifies_production),
        "anchor_sha256": resolved.canonical_sha256 if resolved else None,
        "approved_account_last4": approved_account[-4:],
        "identities_checked": len(rows),
        "account_bearing": len(with_account),
        "rows": rows,
        "mismatches": mismatches,
        "clean": not mismatches,
    }



def _mechanism_probe_detects_a_foreign_account(result: dict) -> bool:
    """Inject an account the anchor does not approve and require the join to notice.

    Returns True only if the comparison actually reported the injected divergence. A probe
    that cannot fail proves nothing, so the injection is asserted to have changed something
    before the detection is believed.
    """
    import sys as _sys

    module = _sys.modules[__name__]
    # The join redacts the account to its last four digits, so the probe resolves the full
    # value from the anchor it just used rather than reconstructing it.
    approved = load_anchor()["approved_account_id"]
    foreign = "444444444444" if approved != "444444444444" else "555555555555"
    real = module.account_sensitive_identities()
    tampered = {label: arn.replace(approved, foreign) for label, arn in real.items()}
    if tampered == real:
        return False  # the injection changed nothing; the probe is not exercising anything

    original = module.account_sensitive_identities
    try:
        module.account_sensitive_identities = lambda: tampered  # noqa: E731
        probed = join()
    finally:
        module.account_sensitive_identities = original
    return bool(probed["mismatches"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = join()
    except AnchorUnavailable as exc:
        print(f"  ANCHOR UNAVAILABLE: {exc}", file=sys.stderr)
        print("EXTERNAL ANCHOR: fail-closed")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"  identities checked {result['identities_checked']} "
              f"(account-bearing {result['account_bearing']})")
        for row in result["mismatches"]:
            print(f"    MISMATCH {row['identity']}: {row['problems']}", file=sys.stderr)
        if not result["certifies_production"]:
            print(f"  tier {result['tier']}: MECHANISM CHECK ONLY — this run certifies "
                  "nothing about production identity")
            # GATE 4N-I18, SEC-1. This used to read "a synthetic anchor is EXPECTED to disagree
            # with the real identity layer" and treated any mismatch as proof the join works.
            # That only held because the account was a HARD-CODED literal in the identity
            # module; once it became tier-resolved the two legitimately agreed and the guard
            # declared its own mechanism broken. Incidental disagreement was never evidence.
            # The mechanism is now proven by INJECTING a foreign account and requiring the
            # join to report it — a probe that fails if the comparison ever stops happening.
            # GATE 4N-I27L. This branch used to `return 0 if detected else 1`, discarding
            # result["clean"] and result["mismatches"]. Gate 4N-I27K pointed the boundary
            # POLICY_ARN at a foreign account: the join reported the mismatch, this command
            # PRINTED it, and exited 0. The tier docstring's claim that a Tier-1 mismatch is
            # expected was the stated justification, and it is false — see join() above.
            # Both checks are now mandatory and are combined with AND, never OR.
            detected = _mechanism_probe_detects_a_foreign_account(result)
            print(f"  mechanism probe: {'detected a foreign account' if detected else 'FAILED to detect a foreign account'}")
            ok = detected and result["clean"]
            # Name what actually failed. "mismatch" when the mechanism broke and every
            # identity agreed would misattribute the failure just as surely as exiting 0
            # misreported it.
            if ok:
                verdict = "mechanism verified"
            elif detected:
                verdict = "mismatch"
            elif result["clean"]:
                verdict = "mechanism FAILED to detect a foreign account"
            else:
                verdict = "mismatch AND mechanism FAILED to detect a foreign account"
            print(f"EXTERNAL ANCHOR: {verdict}")
            return 0 if ok else 1
        print("EXTERNAL ANCHOR: clean" if result["clean"] else "EXTERNAL ANCHOR: mismatch")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
