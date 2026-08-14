#!/usr/bin/env python3
"""Bounded temporary-expiry authorization (Gate 4N-I19, ADV-A, Phases M-R).

THE DEFECT THIS CLOSES. Gate 4N-I17's adversarial lane showed that the stamped expiry was
bound by NOTHING. `require_valid_expiry` checked SYNTAX only, so all of these generated cleanly
with the full suite green:

    2020-01-01T00:00:00Z   already expired before it was issued
    2099-12-31T23:59:59Z   a 73-year "temporary" grant
    the superseded stamp   silently reverted

The expiry is the ONLY temporal control on the temporary Stage-A operator grant, so "it parses"
was the entire bound on how long that authority could live.

TWO LAYERS, DELIBERATELY SEPARATE. They answer different questions and are tested separately:

  AUTHORIZATION  (this module) — is this window one an operator was ALLOWED to grant?
                 Needs an ISSUANCE instant, a maximum duration, and a minimum.
  IAM RUNTIME    (scripts/iam_eval.py) — given a stamped policy, does DateLessThan admit a
                 request at instant T?

A policy can be flawless at the second layer and still be a 73-year grant. Gate 4N-I17 had ten
passing runtime boundary tests and an unbounded window at the same time, which is exactly why
runtime correctness is not accepted as evidence of authorization validity.

WHY ISSUANCE IS AN INPUT AND NOT THE WALL CLOCK. Reading `now()` inside generation would make
every generated artifact depend on when it was generated: the same inputs would produce
different bytes, byte-exactness checks would be meaningless, and the duration bound could be
satisfied or violated by nothing more than the passage of time between two runs. Issuance is
supplied explicitly and reviewed, so the authorization is a property of the DECLARED window.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

UTC = datetime.timezone.utc

# The authorized maximum for a temporary operator grant in this gate chain. Stated once, here,
# and consumed by every generator — the Gate 4N-I17 finding was that no single source existed.
MAX_DURATION = datetime.timedelta(hours=24)

# A window shorter than this cannot complete the work it exists for, so it is refused as an
# operator error rather than silently stamped.
MIN_DURATION = datetime.timedelta(minutes=15)

# THE ACTIVE REVIEWED PAIR. Both halves are explicit and live together, so an expiry can never
# again float free of the window it belongs to. 17h, within the 24-hour maximum.
#
# RESTAMPED AT GATE 4N-I26A. The superseded pair (2026-08-01T00:00:00Z / 2026-08-01T16:00:00Z,
# 16 hours) was still valid and still inside the maximum when it was replaced — it was replaced
# because only ~86 minutes remained, below the 180-minute floor the I26 remediation needs. A
# window that is authorized but too short to finish the work in is an operator decision, never
# an automatic extension: restamping on expiry pressure is how a "temporary" grant becomes
# permanent one renewal at a time. The pair below was supplied by the operator, not derived.
#
# RESTAMPED AT GATE 4N-I27M-A. The superseded pair (2026-08-01T14:45:00Z / 2026-08-02T06:00:00Z,
# 15h15m) was still valid when replaced, but Gate 4N-I27M's preflight found ~314 minutes
# remaining against that gate's 300-minute pre-modification floor — the same
# too-short-to-finish shape I26A retired its predecessor over, caught at the gate boundary
# instead of discovered mid-work. The pair below was supplied by the operator, not derived.
#
# RESTAMPED AT GATE 4N-I27U-A. The superseded pair (2026-08-02T01:00:00Z / 2026-08-02T18:00:00Z,
# 17h) was still valid and still inside the maximum when it was replaced. It was replaced
# because Gate 4N-I27T's six-reviewer assessment REJECTED the candidate 5 PASS / 1 FAIL, and
# the four adversarial findings in failure_propagation.py need a remediation window the old
# pair could not hold. Restamping is not an extension of the work that was authorized: the
# rejection ended that gate, and this pair authorizes the next one. Supplied by the operator,
# not derived. 22h, within the 24-hour maximum.
#
# RESTAMPED AT GATE 4N-I28R. The superseded pair (2026-08-03T08:23:37Z / 2026-08-04T06:23:37Z,
# 22h) was still valid and still inside the maximum when it was replaced, with ~144 minutes
# left. It was replaced because Gate 4N-I28Q's six-reviewer assessment REJECTED the candidate
# 4 FAIL / 2 PASS on a command-root defect, and the rejection ENDED the gate that pair
# authorized. The remaining window is deliberately not spent on remediation: the bounded
# command-root work plus the separate read-only pre-freeze validation that must follow it do
# not fit inside 144 minutes, and starting work that cannot finish inside its own window is
# how a window stops bounding anything. This pair authorizes the NEXT gate, not more of the
# last one. Supplied by the operator, not derived. 22h, within the 24-hour maximum.
#
# RESTAMPED AT GATE 4N-I28AD. The superseded pair (2026-08-04T03:59:30Z / 2026-08-05T01:59:30Z,
# 22h) was still valid when replaced, with roughly 405 minutes left. It was replaced because
# Gate 4N-I28AC's read-only validation returned REMEDIATION REQUIRED on ACC-I28AC-01: a
# `sitecustomize.py` staged into the repository's own scripts/ directory is auto-imported at
# interpreter startup by the graded command's PYTHONPATH, and pre-seeding
# sys.modules["pytest_session_guard"] there makes the guard and its verifier resolve the same
# substituted module. The measured exploit left the suite green at 2906 passed / 0 failed with
# four assurance-control modules absent. That work — a bounded interpreter-bootstrap and
# executed-code-provenance remediation, followed by the separate read-only pre-freeze
# validation that must follow it — does not fit in 405 minutes, and starting work that cannot
# finish inside its own window is how a window stops bounding anything. This pair authorizes
# the NEXT gate, not more of the last one. Supplied by the operator, not derived. 22h, within
# the 24-hour maximum.
# RESTAMPED AT GATE 4N-I28BA. The superseded pair (2026-08-05T11:32:43Z / 2026-08-06T09:32:43Z,
# 22h) was still valid when replaced, with roughly 477 minutes left. It was replaced because
# Gate 4N-I28AX rejected candidate 4N-I28AX-CANDIDATE-1 at PASS 3 / FAIL 3, and the decisive
# blocker ADV-I28AX-01 — a leading `exec` removes the following command word from BOTH
# command-position derivations while the parse reports COMPLETE, trustworthy, and zero errors,
# so an unclassified executable in a graded release-blocking step goes undetected and an
# `exec docker run --privileged` call site is never adjudicated — needs a command-position
# transfer grammar, independent disagreement-sensitive oracles, and a full falsification pass.
# Gate 4N-I28AY measured that work at roughly 880 minutes against 477 remaining and stopped
# BEFORE any edit rather than start what could not finish, because starting work that cannot
# finish inside its own window is how a window stops bounding anything. This pair authorizes
# the NEXT gate, not more of the last one. Supplied by the operator, not derived. 22h, within
# the 24-hour maximum.
# RESTAMPED AT GATE 4N-I28BF-A4S. The superseded pair (2026-08-06T01:35:35Z /
# 2026-08-06T23:35:35Z, 22h) was still valid when replaced, with roughly 485 minutes left. It
# was replaced because the first I28BF-A4 attempt was killed by filesystem exhaustion (ENOSPC)
# and Gate 4N-I28BF-A4R's recovery revalidation left the remaining window with roughly 15
# minutes of reserve over the calibrated I28BF-A4 ceiling with required margin (~473 minutes) —
# insufficient for a trustworthy gate spanning three isolated end-to-end graded-session attacks,
# ten assertion mutations, graded-session A–F, structural self-protection, result-shape,
# AC-23, the I28AM sandbox regression, the full suite, and evidence generation. Starting work
# that cannot finish inside its own window is how a window stops bounding anything. This pair
# authorizes the NEXT gate, not more of the last one. Supplied by the operator, not derived.
# 22h, within the 24-hour maximum.
#
# RESTAMPED AT GATE 4N-I28BH-B-R. The superseded pair (2026-08-07T08:14:44Z / 2026-08-08T06:14:44Z,
# 22h) was still valid when replaced. It was replaced because Gate 4N-I28BH-A's collection-
# classification-and-oracle-design phase closed and the reconciliation (4N-I28BH-A-R) fixed the
# authoritative BH-B workload at 161 uncovered security collections; that implementation program does
# not fit in the remaining window, and a restamp before BH-B is an operator decision, not an automatic
# extension of the work already authorized. This pair authorizes the NEXT gate. Supplied by the
# operator, not derived. 22h, within the 24-hour maximum.
#
# RESTAMPED AT GATE 4N-I28BH-B0w-R2-R. The superseded pair (2026-08-08T03:51:41Z /
# 2026-08-09T01:51:41Z, 22h) was still valid and still inside the maximum when it was replaced. It
# was replaced because the immediately preceding Gate 4N-I28BH-B0w-R2 attempt stopped at its
# mandated go/no-go with OPERATOR ACTION REQUIRED — RESTAMP REQUIRED, before any P1-P9 integration,
# agent launch, or canonical write: the captured runway (~9.43h remaining against a 03:51:41Z
# issuance) was insufficient for the complete final witness-trust integration — the five-way merge
# into one candidate, the re-authored fail-closed battery, the full six-adversary wave with its
# remediation cycles, the P8 independent non-circular sweep, and the signed CLASS_CLOSED proof —
# which the prior 12.2h gate could not complete even the start of, defects recurring mid-fix.
# Starting an integration that cannot finish inside its own window is how a window stops bounding
# anything. This pair authorizes the NEXT gate, not more of the last one. Supplied by the operator,
# not derived. 22h, within the 24-hour maximum.
#
# RESTAMPED AT GATE 4N-I28BH-B0w-R2-SLICE1. The superseded pair (2026-08-08T16:45:59Z /
# 2026-08-09T14:45:59Z, 22h) was still valid and inside the maximum when replaced. It was replaced
# because the preceding mega-closeout attempt (4N-VAL-I28AX-01-MEGA-CLOSEOUT) stopped at its go/no-go
# with OPERATOR ACTION REQUIRED — FULL CLOSEOUT RESTAMP REQUIRED before any substantive work: the
# master objective (RM8 -> Stage-2 P6 -> P1-P9 CLASS_CLOSED -> B0a-LANDING -> BH-B1..B-FINAL -> BH-C
# -> final VAL closure) exceeds ANY single window by SCOPE, not merely the ~15.6h then remaining. The
# operator re-scoped to Slice 1 — the witness-trust prerequisite alone (RM8 -> Stage-2 P6 -> signed
# P1-P9 CLASS_CLOSED, STOP before B0a) — and restamped a fresh full window for it. This pair
# authorizes THAT slice, not more of the last one. Supplied by the operator, not derived. 22h, within
# the 24-hour maximum.
#
# RESTAMPED AT GATE 4N-I28BH-B0w-R2-SLICE1-CLOSED-CAPABILITY-REDESIGN. The superseded pair
# (2026-08-08T23:19:50Z / 2026-08-09T21:19:50Z, 22h) was still valid and inside the maximum when
# replaced. It was replaced because the SLICE-1 witness-trust closure was execution-falsified as
# structurally NON-CONVERGENT under enumerate-and-govern (three gates: 5 escapes -> closed -> 12 new
# escapes of the same open classes), and the operator authorized a from-scratch CLOSED-CAPABILITY
# architecture redesign (design + prototype + attack + migrate P1-P9 + integrate + multi-wave
# falsification + convergence proof + independent sign) — a fresh-full-window effort for which the
# ~14.7h remaining on the prior pair was insufficient. This gate explicitly authorizes ONE initial
# restamp. This pair authorizes THAT redesign. Supplied by the operator, not derived. 22h, within the
# 24-hour maximum.
# RESTAMPED AT GATE 4N-I28BH-B0a-SLICE2. The superseded pair (2026-08-09T06:36:36Z /
# 2026-08-10T04:36:36Z, 22h) was still valid and inside the maximum when replaced. It was replaced
# because Slice 1 (the P1-P9 closed-capability + faithfulness witness-trust prerequisite) is now
# independently SIGNED CLASS_CLOSED, unblocking Slice 2 — the large canonical landing of the signed
# framework + certificate-backed consumer wiring + fresh discovery/classification + maximum BH-B
# completeness-consumer implementation. That work exceeds the ~11.4h then remaining on the prior
# pair. This gate explicitly authorizes ONE initial restamp. Supplied by the operator, not derived.
# 22h, within the 24-hour maximum.
# RESTAMPED AT GATE 4N-I28BH-B-SLICE3. The superseded pair (2026-08-09T17:00:00Z /
# 2026-08-10T15:00:00Z, 22h) was still valid and inside the maximum when replaced. It was replaced
# because Slice 3 — continuing certificate-backed BH-B consumer implementation from the certified
# B1-B4 golden pattern (remaining-authority taxonomy + reusable non-circular derivation families +
# PARTITION::boundary_deny + argument-taking/AST/cross-module authorities + integration waves toward
# uncovered=0 + BH-B-FINAL if reached) — is a large 16-agent multi-wave canonical effort exceeding
# the ~14.5h then remaining. This gate explicitly authorizes ONE initial restamp. Supplied by the
# operator, not derived. 22h, within the 24-hour maximum.
# RESTAMPED AT GATE 4N-I28BH-B-ARCHITECTURAL-ADJUDICATION. The superseded pair (2026-08-10T00:00:00Z
# / 2026-08-10T22:00:00Z, 22h) was still valid when replaced. It was replaced because this gate — a
# closed property-specific security-assurance taxonomy resolving the ~138 structurally-blocked BH-B
# collections (membership-completeness where independent authority exists; review-pinned integrity /
# exclusion-policy / generated-contract / runtime-invariant controls where the collection is the
# authority) + a new validator architecture + full mutation/false-assurance/self-attestation batteries
# + BH-B-FINAL — is a large 16-agent effort exceeding the ~15.4h then remaining. This gate authorizes
# ONE initial restamp. Supplied by the operator, not derived. 22h, within the 24-hour maximum.
# RESTAMPED AT the Phase-4 EXPIRY AUTHORIZATION PIN REMEDIATION gate. The superseded pair
# (2026-08-10T06:00:00Z / 2026-08-11T04:00:00Z, 22h) was authored for the Gate 4N-I28BH-B closeout and
# had already fallen out of its window by wall clock: its latest authorizable expiry (issuance + 24h)
# was in the past, so gen_bootstrap_operator_policy could no longer produce a CURRENT, usable B-1
# boundary-bootstrap executor policy — the EXPIRY AUTHORIZATION BASELINE STALE condition. This restamp
# advances the reviewed window to a current instant so the B-1 Phase-A executor policy can be generated
# and provisioned within it. Supplied by the operator, not derived. 22h, within the 24-hour maximum.
# RESTAMPED AT the INFRA-9 B-1 Phase-B authorization-window restamp. The superseded pair
# (2026-08-12T05:00:00Z / 2026-08-13T03:00:00Z, 22h) authorized the B-1 Phase-A executor
# materialization, which COMPLETED inside it: the SignalNestBoundaryBootstrapOp permission set
# was created, its reviewed inline policy attached byte-exactly (canonical 6dad91ac…), the
# operator assigned and the reserved role materialized. Every Allow in that executor policy
# lapses at the old expiry, so Phase B — the separately authorized boundary create + attach —
# cannot run inside the window that Phase A consumed. This pair authorizes THAT next gate,
# not more of the last one. Supplied by the operator, not derived. 22h, within the 24-hour
# maximum.
# RESTAMPED AT the INFRA-9 B-2 authorization-window restamp. The superseded pair
# (2026-08-13T01:00:00Z / 2026-08-13T23:00:00Z, 22h) authorized B-1 Phase B and the OP-15
# executor retirement, both of which COMPLETED inside it: the reviewed boundary policy was
# created and attached to all five module roles byte-exactly, and the temporary executor
# permission set was retired with the boundary attachments preserved. That pair has lapsed by
# wall clock, and an expired window is never reused. B-2 — the separately authorized
# role-bootstrap lifecycle under the shortened canonical names — needs its own current reviewed
# window. This pair authorizes THAT next gate, not more of the last one. Supplied by the
# operator, not derived. 22h, within the 24-hour maximum.
ACTIVE_ISSUANCE_UTC = "2026-08-14T12:00:00Z"
ACTIVE_EXPIRY_UTC = "2026-08-15T10:00:00Z"

PURPOSES = ("stage_a_operator", "role_bootstrap", "boundary_bootstrap", "readonly_verifier")


class ExpiryAuthorizationError(ValueError):
    """Fail-closed. A window outside the authorized envelope is never stamped.

    Deliberately a ValueError: an unauthorized window IS a bad value, and every existing
    caller and test that already required generation to raise ValueError on a placeholder or
    malformed stamp keeps working unchanged. Introducing a parallel exception hierarchy would
    have silently widened those contracts.
    """


def _parse(value: object, *, what: str) -> datetime.datetime:
    """Parse a canonical RFC 3339 UTC instant. Delegates to the IAM date parser.

    Using the SAME parser the runtime layer uses means a value cannot be authorized under one
    interpretation and evaluated under another — the two layers are separate in what they
    check, never in how they read an instant.
    """
    import iam_eval

    if value is None or value == "":
        raise ExpiryAuthorizationError(f"{what} is REQUIRED; there is no default and no discovery")
    if not isinstance(value, str):
        raise ExpiryAuthorizationError(f"{what} must be a string, got {type(value).__name__}")
    if "<" in value or ">" in value:
        raise ExpiryAuthorizationError(f"refusing a placeholder {what}: {value!r}")
    if not value.endswith("Z"):
        raise ExpiryAuthorizationError(
            f"{what} must be canonical UTC ending in 'Z', got {value!r}. An offset form names "
            "the same instant but is not the canonical representation this contract stamps.")
    try:
        parsed = iam_eval.parse_iam_date(value, what=what)
    except Exception as exc:
        # A malformed instant is a REFUSAL by this contract, not a parser crash escaping to the
        # caller. Phase P requires generation to fail cleanly before any policy output.
        raise ExpiryAuthorizationError(f"{what} is not a valid RFC 3339 UTC instant: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        raise ExpiryAuthorizationError(f"{what} did not resolve to a timezone-aware UTC instant")
    return parsed


def authorize(*, issuance: str, expiry: str, purpose: str,
              max_duration: datetime.timedelta | None = None,
              min_duration: datetime.timedelta | None = None) -> dict:
    """Return the authorized window, or raise. Called at GENERATION time, before any output."""
    if purpose not in PURPOSES:
        raise ExpiryAuthorizationError(
            f"unknown policy purpose {purpose!r}; expected one of {PURPOSES}")

    if max_duration is None:
        max_duration = MAX_DURATION
    elif max_duration > MAX_DURATION:
        # A caller may tighten the bound; it may never loosen it. Silently accepting a longer
        # maximum would put the bound back under the control of the thing being bounded.
        raise ExpiryAuthorizationError(
            f"a caller may not raise the authorized maximum above {MAX_DURATION}; "
            f"got {max_duration}")
    if min_duration is None:
        min_duration = MIN_DURATION

    issued = _parse(issuance, what="issuance")
    expires = _parse(expiry, what="expiry")
    duration = expires - issued

    if duration <= datetime.timedelta(0):
        raise ExpiryAuthorizationError(
            f"expiry {expiry} is not after issuance {issuance} (duration {duration}). An expiry "
            "at or before issuance is either an already-expired stamp or a zero-length window.")
    if duration < min_duration:
        raise ExpiryAuthorizationError(
            f"window {duration} is shorter than the authorized minimum {min_duration}")
    if duration > max_duration:
        raise ExpiryAuthorizationError(
            f"window {duration} EXCEEDS the authorized maximum {max_duration}. This is the check "
            "Gate 4N-I17 lacked: a 2099 expiry parsed cleanly and produced a 73-year grant.")

    return {
        "issuance_utc": issuance,
        "expiry_utc": expiry,
        "purpose": purpose,
        "duration_seconds": int(duration.total_seconds()),
        "duration_hours": round(duration.total_seconds() / 3600, 4),
        "max_duration_seconds": int(max_duration.total_seconds()),
        "min_duration_seconds": int(min_duration.total_seconds()),
        "authorized": True,
    }


def active_pair() -> dict:
    """The single reviewed issuance/expiry pair, validated on every access."""
    return authorize(issuance=ACTIVE_ISSUANCE_UTC, expiry=ACTIVE_EXPIRY_UTC,
                     purpose="stage_a_operator")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--issuance", default=ACTIVE_ISSUANCE_UTC)
    parser.add_argument("--expiry", default=ACTIVE_EXPIRY_UTC)
    parser.add_argument("--purpose", default="stage_a_operator")
    args = parser.parse_args()
    try:
        result = authorize(issuance=args.issuance, expiry=args.expiry, purpose=args.purpose)
    except ExpiryAuthorizationError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("EXPIRY AUTHORIZATION: refused")
        return 2
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(f"  issuance {result['issuance_utc']}  expiry {result['expiry_utc']}  "
              f"duration {result['duration_hours']}h  max "
              f"{result['max_duration_seconds'] // 3600}h")
        print("EXPIRY AUTHORIZATION: authorized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
