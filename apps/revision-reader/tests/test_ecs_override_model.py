"""Gate 4J — the gate's most load-bearing fact, machine-checked against AWS's own model.

Everything in this gate rests on one claim: ECS `ContainerOverride` has no `entryPoint`
member, so a fixed exec-form ENTRYPOINT cannot be replaced at RunTask time. That claim is
repeated in the Dockerfile, the reader module, both READMEs and the migrations runbook —
and until this file existed it was asserted in prose everywhere and verified nowhere. If
AWS ever added the member, every one of those documents would silently become wrong while
all the other tests kept passing, because they only exercise the CONSEQUENCE (argv
rejection), which would still hold for a program nobody could reach.

This reads the shape definitions out of botocore's bundled ECS service model — the same
data the AWS SDK and CLI use to construct the request — so a model update that introduced
the member fails here on the next dependency bump.

botocore is a HARD test dependency (declared in the dev extra), imported directly rather
than via importorskip: a check that quietly skips to green is worse than no check. If the
model cannot be inspected, this file ERRORS rather than passing.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

CONTAINER_OVERRIDE_MEMBERS = {
    "name", "command", "environment", "environmentFiles",
    "cpu", "memory", "memoryReservation", "resourceRequirements",
}

# The task-level override. It carries taskRoleArn AND executionRoleArn, which is exactly
# why exact-ARN iam:PassRole is the only real prevention in the rest of the override
# family, and why the reader has no task role at all.
TASK_OVERRIDE_ROLE_MEMBERS = {"taskRoleArn", "executionRoleArn"}


def _load_ecs_model() -> dict:
    import botocore  # HARD dependency: ImportError -> collection error, never a skip.

    base = Path(botocore.__file__).parent / "data" / "ecs"
    candidates = sorted(base.glob("*/service-2.json*")) if base.is_dir() else []
    assert candidates, f"no ECS service model found under {base} — cannot verify the claim"
    path = candidates[-1]
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def shapes() -> dict:
    return _load_ecs_model()["shapes"]


def test_container_override_has_no_entrypoint_member(shapes):
    """THE claim. If this ever fails, the prevention story collapses to detection and
    every document asserting it must be rewritten before the reader is trusted again."""
    members = set(shapes["ContainerOverride"]["members"])
    assert "entryPoint" not in members, (
        "ECS ContainerOverride now exposes entryPoint. A RunTask caller can replace the "
        "image's fixed ENTRYPOINT, so the reader's override PREVENTION is reduced to "
        "detection. Update the Dockerfile, reader.py, the revision_reader module and both "
        "READMEs before relying on this image again."
    )


def test_container_override_member_set_is_exactly_what_the_docs_claim(shapes):
    """Pinning the WHOLE set, not just the absence of one member: a newly added member is
    a new caller-controlled channel, and this gate's threat model enumerates them by name.
    A failure here means the documented list is stale, not necessarily that anything is
    unsafe — read the diff and re-reason."""
    assert set(shapes["ContainerOverride"]["members"]) == CONTAINER_OVERRIDE_MEMBERS


def test_task_override_still_carries_both_role_arns(shapes):
    """The converse risk: if these ever vanished, the exact-ARN PassRole reasoning would
    be over-cautious rather than wrong — but the docs would still be inaccurate."""
    members = set(shapes["TaskOverride"]["members"])
    assert TASK_OVERRIDE_ROLE_MEMBERS <= members


def test_overrides_is_a_caller_supplied_request_member(shapes):
    """`overrides` is a member of RunTaskRequest — i.e. a value the caller supplies.

    NOTE ON WHAT THIS DOES NOT PROVE: botocore's service model carries no IAM condition-key
    metadata, so this test CANNOT establish that IAM lacks a condition key for override
    contents. That absence is a documented assumption drawn from the AWS Service
    Authorization Reference, recorded in the module README, not proven here. This test only
    pins that the override payload is caller-supplied at all — which is why the reader's
    destination controls live in the image, not in IAM."""
    assert "overrides" in set(shapes["RunTaskRequest"]["members"])
