"""Serialization contracts for worker-fleet operator diagnostics.

Operator-only, and deliberately **coarse**: it reports the fleet's aggregate
health (status counts, active/stale totals) and a per-worker lifecycle summary,
but never the worker's stable identity or build provenance. In particular it
omits ``worker_id``, ``build_revision``, ``host_fingerprint`` and
``application_version`` — those are operational identifiers/metadata the fleet
diagnostics surface must not enumerate. It never carries a URL, host, port,
bucket, lease token or raw error.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class WorkerSummaryOut(BaseModel):
    """Coarse per-worker lifecycle summary (no identity or build metadata)."""

    worker_type: str
    status: str
    concurrency: int
    queue_backend: str
    supported_job_types: list[str]
    started_at: datetime | None
    last_heartbeat_at: datetime | None
    stopped_at: datetime | None


class WorkerFleetDiagnosticsOut(BaseModel):
    """Operator fleet snapshot: aggregate health + coarse per-worker summaries."""

    status_counts: dict[str, int]
    active_count: int
    stale_count: int
    workers: list[WorkerSummaryOut]
