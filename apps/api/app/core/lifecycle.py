"""Shared graceful-shutdown helpers for the API and worker processes.

Both long-lived processes must release the same resources on exit — flush
telemetry, close the Redis coordination clients, and dispose the database engine
pool. Every step here is:

* **best-effort** — a failure is logged (secret-free) and swallowed, never raised;
* **bounded** — telemetry flush runs under an explicit time budget so a slow or
  unreachable exporter can never delay exit;
* **isolated** — one failing step never skips the remaining steps;
* **idempotent** — the whole sequence is safe to call more than once (a second
  SIGTERM, or a normal shutdown followed by an atexit hook).

Nothing here performs work that can leak identifiers or credentials.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import flush_metrics
from app.core.tracing import _shutdown_flush

logger = get_logger("signalnest.lifecycle")


def flush_telemetry(settings: Settings) -> None:
    """Flush metrics then traces under a bounded budget. Never raises."""
    try:
        flush_metrics()
    except Exception:  # pragma: no cover - defensive; flush_metrics already isolates
        logger.warning("lifecycle.metrics_flush_failed")
    try:
        # Bounded by the configured budget; the tracer's safe wrapper swallows and
        # counts export failures rather than propagating them.
        _shutdown_flush(settings.tracing_shutdown_flush_seconds)
    except Exception:  # pragma: no cover - defensive; _shutdown_flush is already safe
        logger.warning("lifecycle.trace_flush_failed")


def close_coordination() -> None:
    """Close the process-wide Redis cache + job-notifier clients (if built)."""
    try:
        from app.infra.cache import cache

        cache.close()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.warning("lifecycle.cache_close_failed")
    try:
        from app.jobs.service import shutdown_notifier

        shutdown_notifier()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.warning("lifecycle.notifier_close_failed")


def close_database() -> None:
    """Dispose the SQLAlchemy engine, returning pooled connections. Never raises."""
    try:
        from app.db.session import engine

        engine.dispose()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.warning("lifecycle.db_dispose_failed")


def graceful_shutdown(settings: Settings) -> None:
    """Run the full bounded, idempotent shutdown sequence.

    Order matters: flush telemetry *before* tearing down transports so late spans
    and metrics are exported, then close coordination clients, then the database
    pool last (other steps may still touch it).
    """
    flush_telemetry(settings)
    close_coordination()
    close_database()
