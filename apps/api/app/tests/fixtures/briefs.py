"""Brief-world helpers for tests (Project Phase 5A-W4; W21 lane ADV5AB)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import app.briefs.service as briefs_service
from app.opportunities.models import ValidationEvidence

#: Bound on every wait in a hold, so a missed release fails a test instead of hanging it.
HOLD_SECONDS = 60.0


def add_validation_evidence(
    s, *, workspace_id: str, opportunity_id: str, key: str, count: int
) -> list[str]:
    """``count`` validation-evidence rows on one opportunity; returns their ids in order."""
    ids = []
    for n in range(count):
        evidence_id = f"ev-{key}-{n}"
        s.add(
            ValidationEvidence(
                id=evidence_id,
                workspace_id=workspace_id,
                opportunity_id=opportunity_id,
                source_type="rss_news",
                detail=f"evidence {key} {n}",
            )
        )
        ids.append(evidence_id)
    s.flush()
    return ids


class CaptureHold:
    """Holds one brief capture open after its brief row is flushed.

    ``create_brief`` locks the opportunity row (``_next_version``), flushes the brief, then
    writes its audit row. The hold wraps that audit write: in the thread named
    :attr:`thread_name` it signals :attr:`holding` and waits for :attr:`release`, so the
    capture's transaction -- and the opportunity lock it holds -- stays open until the test
    releases it. Every other thread passes straight through.
    """

    def __init__(self, thread_name: str):
        self.thread_name = thread_name
        self.holding = threading.Event()
        self.release = threading.Event()

    def wrap(self, record_audit):
        def held(*args, **kwargs):
            result = record_audit(*args, **kwargs)
            if threading.current_thread().name == self.thread_name:
                self.holding.set()
                assert self.release.wait(HOLD_SECONDS), "the held capture was never released"
            return result

        return held


@contextmanager
def capture_hold(thread_name: str) -> Iterator[CaptureHold]:
    """Install a :class:`CaptureHold` on ``app.briefs.service.record_audit``."""
    hold = CaptureHold(thread_name)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(briefs_service, "record_audit", hold.wrap(briefs_service.record_audit))
        try:
            yield hold
        finally:
            hold.release.set()


__all__ = ["HOLD_SECONDS", "CaptureHold", "add_validation_evidence", "capture_hold"]
