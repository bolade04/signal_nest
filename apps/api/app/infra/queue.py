"""Job queue adapter.

Default: in-process synchronous execution so the whole pipeline runs with no worker
or broker. Full mode: enqueue onto Redis for the ``app.jobs.worker`` process.

Handlers are registered by name so the same registry serves both backends.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("signalnest.queue")

JobHandler = Callable[[dict[str, Any]], Any]
_REGISTRY: dict[str, JobHandler] = {}


def register_job(name: str) -> Callable[[JobHandler], JobHandler]:
    def _wrap(fn: JobHandler) -> JobHandler:
        _REGISTRY[name] = fn
        return fn

    return _wrap


def get_handler(name: str) -> JobHandler:
    if name not in _REGISTRY:
        raise KeyError(f"No job handler registered for '{name}'")
    return _REGISTRY[name]


class Queue(Protocol):
    def enqueue(self, job_name: str, payload: dict[str, Any]) -> Any: ...


class InProcessQueue:
    """Runs the handler immediately in the current process (synchronous)."""

    def enqueue(self, job_name: str, payload: dict[str, Any]) -> Any:
        logger.info("job.run", extra={"extra_fields": {"job": job_name, "backend": "inprocess"}})
        return get_handler(job_name)(payload)


def build_queue() -> Queue:
    settings = get_settings()
    if settings.queue_backend == "redis":  # pragma: no cover - full mode only
        import json

        import redis

        client = redis.from_url(settings.redis_url)  # type: ignore[arg-type]

        class RedisQueue:
            stream = "signalnest:jobs"

            def enqueue(self, job_name: str, payload: dict[str, Any]) -> Any:
                client.xadd(self.stream, {"job": job_name, "payload": json.dumps(payload)})
                logger.info("job.enqueue", extra={"extra_fields": {"job": job_name}})
                return None

        return RedisQueue()
    return InProcessQueue()


queue: Queue = build_queue()
