"""The ``creative.generate`` durable-job handler (Project Phase 5C; registered at W21 CF-2).

The handler is thin, like every handler in :mod:`app.jobs.handlers`: it hands one attempt to
:func:`app.creative.service.execute_generation`, which re-checks every gate (capability,
brief approval, parent, requester, ceilings), generates through the metered LLM service under
the generation locks, screens and reviews the output, and stores the draft and its review with
their audit record. It returns a safe summary (outcome, ids, versions, flags; never generated
text) and never commits: the worker owns the transaction. The usage rows of every model attempt
are handed to the worker as an after-rollback write, so a failed attempt's spend still counts.

The registration import in the freeze-owned :mod:`app.jobs.handlers` is unchanged (CF-2).
"""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.jobs.registry import HandlerContext, register_handler
from app.jobs.status import JobType


@register_handler(JobType.CREATIVE_GENERATE)
def execute_creative_generation(ctx: HandlerContext) -> dict[str, Any]:
    """Generate one draft version for the job's brief (see the module docstring)."""
    # Deferred imports: the creative service imports the enqueue service, which imports the
    # handler modules to register them.
    from app.creative.service import execute_generation
    from app.llm import service as llm_service_module

    return execute_generation(
        ctx, settings=get_settings(), llm=llm_service_module.llm_service
    )


__all__ = ["execute_creative_generation"]
