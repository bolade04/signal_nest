#!/usr/bin/env python3
"""Timezone-aware candidate time arithmetic (Gate 4N-I11, Defect 18).

THE DEFECT. FROZEN-CANDIDATE.json recorded `minutes_remaining_at_freeze: 169` while the real
delta between `candidate_utc` and `stamped_expiry_utc` was 229.6 minutes — a ~60-minute
error, caught by the AWS-permissions lane. The cause was mixing `time.mktime`, which
interprets a struct as LOCAL time, with a UTC `gmtime` struct. Nothing in the policy bytes was
affected, but this is the one number that describes the temporary-permission model, and it was
wrong by a timezone offset.

Everything here uses timezone-aware datetimes and derives the display value from the SAME
parsed instant the policy expiry tests use, via iam_eval.parse_iam_date — so the metadata and
the enforcement cannot drift apart.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iam_eval  # noqa: E402

UTC = datetime.timezone.utc


def now_utc() -> datetime.datetime:
    """Timezone-aware. `datetime.utcnow()` returns a NAIVE value and is the trap here."""
    return datetime.datetime.now(UTC)


def to_iso(moment: datetime.datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("refusing to format a timezone-naive datetime")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def remaining(expiry: str, *, at: datetime.datetime | None = None) -> dict:
    """Exact seconds and minutes between `at` and `expiry`.

    `expiry` is parsed by iam_eval.parse_iam_date — the identical function the artifact-byte
    expiry tests use — so a candidate's recorded window and its enforced window are computed
    from one instant.
    """
    moment = at if at is not None else now_utc()
    if moment.tzinfo is None:
        raise ValueError("refusing to compute a window from a timezone-naive datetime")
    deadline = iam_eval.parse_iam_date(expiry, what="candidate expiry")
    delta = deadline - moment.astimezone(UTC)
    seconds = delta.total_seconds()
    return {
        "at_utc": to_iso(moment),
        "expiry_utc": to_iso(deadline),
        "seconds_remaining": int(seconds),
        "minutes_remaining": int(seconds // 60),
        "expired": seconds <= 0,
    }


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--expiry", required=True)
    args = parser.parse_args()
    print(json.dumps(remaining(args.expiry), indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
