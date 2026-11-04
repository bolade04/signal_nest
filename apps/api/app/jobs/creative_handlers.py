"""The ``creative.generate`` durable-job handler (Project Phase 5C; registered at W21 CF-2).

CF-2 registers the job type and this **stub**: it performs no generation, calls no model and
writes nothing. It refuses every attempt with a non-retryable ``POLICY`` error, so a job that
reached a worker by any path could never silently succeed or burn retries. The 5C seam lane
replaces the body (it owns this module, Phase 5C plan §5); the registration import in the
freeze-owned ``app.jobs.handlers`` stays as it is.
"""

from __future__ import annotations

from typing import Any

from app.jobs.registry import HandlerContext, register_handler
from app.jobs.status import JobErrorCode, JobExecutionError, JobType

#: The stable summary of a refused attempt (no payload, no content).
NOT_IMPLEMENTED = "creative generation is not implemented at CF-2"


@register_handler(JobType.CREATIVE_GENERATE)
def execute_creative_generation(ctx: HandlerContext) -> dict[str, Any]:
    """Stub: refuse, non-retryably, before touching anything."""
    raise JobExecutionError(JobErrorCode.POLICY, NOT_IMPLEMENTED)


__all__ = ["NOT_IMPLEMENTED", "execute_creative_generation"]
