#!/usr/bin/env python3
"""Reviewer lane retrieval states — Gate 4N-I28B, finding I28A-04.

WHY THIS EXISTS. Twice — Gate 4N-I27T and Gate 4N-I27Y — all six reviewer lanes finished with
verdicts in hand and not one reached the coordinator; both times a premature OPERATOR ACTION
REQUIRED was recorded, and both times the verdicts surfaced only when relayed by hand. The cause
was never the reviewers. It was that retrieval depended ENTIRELY on message delivery: no lane was
ever told to write its verdict to a known path, so silence could not be told apart from loss.

Gate 4N-I27Z wrote the prospective protocol with four states. Gate 4N-I28A found the fifth
missing: a lane that DIED is not the same as a lane that finished and wrote nothing. Collapsing
them is what makes "no verdict" look like a reviewer decision instead of an infrastructure fault.

THE PRECEDENCE RULE, in order, and it is the whole point:

    1. a valid verdict artifact          — if one exists, the verdict is RECOVERED from bytes
    2. the process completion state      — only consulted when no artifact exists
    3. the transport state               — never decides anything on its own

Transport failure does not imply execution failure. A lane whose process exited non-zero but
which left a complete, correctly bound artifact is COMPLETED_WITH_ARTIFACT: the work is done and
the verdict is on disk, whatever happened to the shell afterwards.
"""
from __future__ import annotations

RUNNING = "RUNNING"
COMPLETED_WITH_ARTIFACT = "COMPLETED_WITH_ARTIFACT"
COMPLETED_NO_ARTIFACT = "COMPLETED_NO_ARTIFACT"
TRANSPORT_UNDELIVERED = "TRANSPORT_UNDELIVERED"
FAILED = "FAILED"

STATES = (RUNNING, COMPLETED_WITH_ARTIFACT, COMPLETED_NO_ARTIFACT, TRANSPORT_UNDELIVERED, FAILED)

#: What each state means, and what it does NOT mean.
DEFINITIONS = {
    RUNNING: "the reviewer process is still active; no final artifact is required yet",
    COMPLETED_WITH_ARTIFACT: (
        "a valid verdict artifact exists and binds the expected candidate, tree and raw packet "
        "digest. Reached regardless of process exit status or transport outcome"),
    COMPLETED_NO_ARTIFACT: (
        "the reviewer process completed SUCCESSFULLY and no valid verdict artifact exists — the "
        "only state that may be described as a lost verdict"),
    TRANSPORT_UNDELIVERED: (
        "message delivery failed or never surfaced, while the process ran to completion and left "
        "no artifact. Describes the CHANNEL, never the reviewer's judgement"),
    FAILED: (
        "the reviewer execution terminated unsuccessfully and no valid verdict artifact exists. "
        "An infrastructure fault, not a reviewer decision"),
}

#: A lane in one of these states must never be automatically relaunched.
NEVER_RELAUNCH = (RUNNING, COMPLETED_WITH_ARTIFACT)

#: Legal transitions. A lane only ever leaves RUNNING.
TRANSITIONS = {
    RUNNING: (COMPLETED_WITH_ARTIFACT, COMPLETED_NO_ARTIFACT, TRANSPORT_UNDELIVERED, FAILED),
    COMPLETED_WITH_ARTIFACT: (),
    COMPLETED_NO_ARTIFACT: (COMPLETED_WITH_ARTIFACT,),   # a late artifact still wins
    TRANSPORT_UNDELIVERED: (COMPLETED_WITH_ARTIFACT,),
    FAILED: (COMPLETED_WITH_ARTIFACT,),
}


class RetrievalStateError(ValueError):
    """Fail-closed. An unclassifiable lane is never assumed to have passed."""


def classify(*, process_alive: bool, process_exit: int | None,
             artifact_valid: bool, transport_delivered: bool) -> str:
    """Which state a lane is in.

    `artifact_valid` means the file exists, parses, and binds the expected candidate id, tree
    hash and raw packet digest — not merely that a file is present.
    """
    if artifact_valid:
        # PRECEDENCE 1. Bytes on disk outrank every other signal, including a non-zero exit.
        return COMPLETED_WITH_ARTIFACT
    if process_alive:
        return RUNNING
    if process_exit is None:
        raise RetrievalStateError(
            "a lane that is not running must have an exit status; without one its state cannot "
            "be established and it must not be treated as complete")
    # PRECEDENCE 2. No artifact: the process outcome decides.
    if process_exit != 0:
        return FAILED
    # PRECEDENCE 3. Completed cleanly with nothing on disk — transport only distinguishes how it
    # was reported, never whether a verdict exists.
    return COMPLETED_NO_ARTIFACT if transport_delivered else TRANSPORT_UNDELIVERED


def may_relaunch(state: str) -> bool:
    if state not in STATES:
        raise RetrievalStateError(f"{state!r} is not a defined retrieval state")
    return state not in NEVER_RELAUNCH


def is_lost_verdict(state: str) -> bool:
    """Only ONE state may be reported as a verdict that was produced and lost."""
    if state not in STATES:
        raise RetrievalStateError(f"{state!r} is not a defined retrieval state")
    return state == COMPLETED_NO_ARTIFACT


def contract() -> dict:
    return {"states": list(STATES), "definitions": DEFINITIONS,
            "precedence": ["valid verdict artifact", "process completion state", "transport state"],
            "never_relaunch": list(NEVER_RELAUNCH), "transitions":
                {k: list(v) for k, v in TRANSITIONS.items()},
            "transport_failure_implies_execution_failure": False,
            "verdict_fabrication_permitted": False,
            "premature_status_correction": "append-only"}


def main() -> int:
    import json
    print(json.dumps(contract(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
