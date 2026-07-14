"""Durable-job wake-up coordination (optional, best-effort).

The database is the **only** authoritative source of queued work: a worker always
discovers and claims jobs by polling the durable store. Redis coordination is a
pure *latency optimization* layered on top — it lets a just-enqueued job wake an
idle worker immediately instead of waiting out the poll interval.

Correctness therefore never depends on Redis:

* **A failed notification cannot lose a job.** The job is already committed to the
  DB before any signal is published; a publish failure is swallowed (surfaced only
  as a warning) and the job is still found by the next bounded DB poll.
* **A duplicate notification cannot double-run a job.** A wake only triggers a DB
  claim, which is atomic and lease-fenced; a spurious wake simply finds nothing.
* **A lost notification cannot stall the queue.** The worker's idle wait is
  bounded (``worker_poll_interval_seconds``), so it re-polls regardless.

Lease fencing stays entirely DB-enforced; nothing here grants ownership.

The optional :class:`RedisAdvisoryLock` is offered for callers that want a
short-lived, best-effort mutual-exclusion hint (e.g. a singleton sweep). It is
**advisory only** — it never gates job ownership — and is safe: acquisition mints
an opaque token with a bounded TTL, and release only deletes the key when the
caller still holds that exact token (compare-and-delete), so a caller can never
release a lock another owner has since acquired.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.errors import RedisNotifyFailedError
from app.core.logging import get_logger

logger = get_logger("signalnest.jobs.coordination")


class JobNotifier(Protocol):
    """Best-effort wake-up channel for durable jobs."""

    def notify_job_available(self) -> None: ...
    def wait_for_job(self, timeout: float) -> bool: ...
    def close(self) -> None: ...


class NullJobNotifier:
    """Local-mode notifier: no coordination backend, DB polling only.

    ``wait_for_job`` performs the bounded idle sleep the worker would otherwise do
    itself, so the worker loop is identical regardless of backend and never
    busy-spins.
    """

    def notify_job_available(self) -> None:
        return None

    def wait_for_job(self, timeout: float) -> bool:
        if timeout > 0:
            time.sleep(timeout)
        return False

    def close(self) -> None:
        return None


class RedisJobNotifier:
    """Redis pub/sub wake-up. Constructed with a client so it is test-injectable.

    All Redis interaction is best-effort: a publish failure raises internally but
    the enqueue seam swallows it (the job is already durable), and a subscribe or
    receive failure degrades to a bounded sleep so the worker keeps polling.
    """

    def __init__(self, client: Any, *, channel: str) -> None:
        self._client = client
        self._channel = channel
        self._pubsub: Any | None = None

    def notify_job_available(self) -> None:
        try:
            self._client.publish(self._channel, "1")
        except Exception as exc:
            # Non-fatal: the job is already committed. Callers treat this as a
            # warning; the worker's DB poll still finds the job.
            raise RedisNotifyFailedError() from exc

    def _ensure_subscribed(self) -> Any | None:
        if self._pubsub is None:
            try:
                self._pubsub = self._client.pubsub()
                self._pubsub.subscribe(self._channel)
            except Exception:
                self._pubsub = None
        return self._pubsub

    def wait_for_job(self, timeout: float) -> bool:
        pubsub = self._ensure_subscribed()
        if pubsub is None:
            # Degrade to a bounded sleep; the worker re-polls the DB after.
            if timeout > 0:
                time.sleep(timeout)
            return False
        try:
            message = pubsub.get_message(
                ignore_subscribe_messages=True, timeout=max(timeout, 0.0)
            )
        except Exception:
            if timeout > 0:
                time.sleep(timeout)
            return False
        return message is not None

    def close(self) -> None:
        try:
            if self._pubsub is not None:
                self._pubsub.close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass


class RedisAdvisoryLock:
    """Optional, advisory, self-expiring Redis lock. Never gates job ownership."""

    def __init__(self, client: Any, *, key: str, ttl_seconds: float) -> None:
        self._client = client
        self._key = key
        self._ttl_ms = int(ttl_seconds * 1000)
        self._token: str | None = None

    def acquire(self) -> bool:
        token = secrets.token_hex(16)
        try:
            acquired = self._client.set(self._key, token, nx=True, px=self._ttl_ms)
        except Exception:
            return False
        if acquired:
            self._token = token
            return True
        return False

    def release(self) -> bool:
        """Delete the lock key only if we still hold our exact token.

        Uses a WATCH/MULTI optimistic transaction so the ownership check and the
        delete are atomic: if the key changed (a new owner) between the read and
        the delete, the transaction aborts and we release nothing.
        """
        token = self._token
        self._token = None
        if token is None:
            return False
        want = token.encode()
        try:
            with self._client.pipeline() as pipe:
                pipe.watch(self._key)
                current = pipe.get(self._key)
                if current not in (token, want):
                    pipe.unwatch()
                    return False
                pipe.multi()
                pipe.delete(self._key)
                pipe.execute()
                return True
        except Exception:
            return False


def build_job_notifier(settings: Settings | None = None) -> JobNotifier:
    """Select the wake-up notifier for the configured queue backend."""
    settings = settings or get_settings()
    if settings.queue_backend == "redis":  # pragma: no cover - full mode only
        from app.infra.cache import build_redis_client

        client = build_redis_client(settings)
        return RedisJobNotifier(client, channel=settings.redis_notify_channel)
    return NullJobNotifier()


__all__ = [
    "JobNotifier",
    "NullJobNotifier",
    "RedisAdvisoryLock",
    "RedisJobNotifier",
    "build_job_notifier",
]
