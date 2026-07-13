"""Versioned, deterministic job envelopes.

A job envelope wraps a handler's raw payload with:

* an explicit **contract version** so a durable queue can reject or migrate messages
  it does not understand, and
* the **tenant execution context** (see :mod:`app.jobs.context`), so isolation travels
  with the work.

The envelope is deterministic: the same (version, job, context, payload) always produces
the same canonical serialization and ``envelope_hash``. This gives idempotency keys and
reproducible tests without any external service.

Backward compatibility: :func:`unwrap` accepts both a modern enveloped payload and the
legacy bare ``{"scout_request_id": ...}`` dict, so existing callers and in-flight
messages keep working.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.jobs.context import ExecutionContext

#: The only contract version understood by this build. Bump (and add migration
#: handling) when the envelope shape changes incompatibly.
CURRENT_CONTRACT_VERSION = "1"


class JobEnvelope(BaseModel):
    """Deterministic, versioned wrapper around a job payload."""

    model_config = {"frozen": True}

    contract_version: Literal["1"] = CURRENT_CONTRACT_VERSION
    job_name: str
    context: ExecutionContext
    payload: dict[str, Any] = Field(default_factory=dict)

    def canonical_json(self) -> str:
        """Stable serialization independent of key order (for hashing/idempotency)."""
        return json.dumps(
            {
                "contract_version": self.contract_version,
                "job_name": self.job_name,
                "context": self.context.model_dump(),
                "payload": self.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @property
    def envelope_hash(self) -> str:
        """Deterministic idempotency key for this envelope."""
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def to_message(self) -> dict[str, Any]:
        """JSON-serializable form suitable for any queue backend."""
        return json.loads(self.canonical_json())


def wrap(job_name: str, context: ExecutionContext, payload: dict[str, Any]) -> JobEnvelope:
    return JobEnvelope(job_name=job_name, context=context, payload=payload)


def unwrap(data: dict[str, Any]) -> tuple[ExecutionContext | None, dict[str, Any]]:
    """Return ``(context, payload)`` from either an enveloped or a legacy dict.

    * Enveloped: ``{"contract_version": "1", "job_name", "context", "payload"}``.
    * Legacy: any other dict is treated as a bare payload with no context.

    An unrecognized (future) contract version raises, so a durable queue never silently
    processes a message it cannot interpret.
    """
    if "contract_version" in data:
        version = data.get("contract_version")
        if version != CURRENT_CONTRACT_VERSION:
            raise ValueError(
                f"Unsupported job contract version {version!r}; "
                f"this build understands {CURRENT_CONTRACT_VERSION!r}"
            )
        envelope = JobEnvelope.model_validate(data)
        return envelope.context, envelope.payload
    return None, data


__all__ = [
    "CURRENT_CONTRACT_VERSION",
    "JobEnvelope",
    "wrap",
    "unwrap",
]
