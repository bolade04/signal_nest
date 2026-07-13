"""Domain error types and FastAPI exception handlers."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger, request_id_ctx

logger = get_logger("signalnest.errors")


class SignalNestError(Exception):
    """Base class for expected domain errors."""

    status_code = 400
    code = "error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFoundError(SignalNestError):
    status_code = 404
    code = "not_found"


class PermissionDeniedError(SignalNestError):
    status_code = 403
    code = "permission_denied"


class AuthError(SignalNestError):
    status_code = 401
    code = "unauthorized"


class ConflictError(SignalNestError):
    status_code = 409
    code = "conflict"


class ValidationDomainError(SignalNestError):
    status_code = 422
    code = "validation_error"


class ConfigurationError(SignalNestError):
    """Runtime configuration is invalid or incomplete.

    Raised when a required setting is missing or inconsistent for the selected
    runtime mode. This is an operator-facing fault (misconfiguration), returned as
    503 so orchestrators treat the instance as not-ready rather than as a client
    error.
    """

    status_code = 503
    code = "configuration_error"


class AdapterNotConfiguredError(SignalNestError):
    """A production adapter was requested but is not configured.

    Production placeholders must fail explicitly rather than partially activating or
    silently degrading to a local implementation. Returned as 503 (not-ready).
    """

    status_code = 503
    code = "adapter_not_configured"


class CapabilityUnavailableError(SignalNestError):
    """A runtime capability is unavailable in the current mode/configuration."""

    status_code = 503
    code = "capability_unavailable"


# --- Production data-plane adapter / worker-fleet taxonomy (Phase 3A.4a) ------
#
# Every adapter (PostgreSQL, Redis cache, Redis coordination, object storage) and
# every worker-fleet operation raises one of these instead of leaking a raw
# driver/SDK exception. Each carries a stable ``code``, a **static safe message**
# (a raw exception message is never passed through — it may contain a URL,
# credential, host or customer content), a ``retryable`` hint used by callers and
# readiness probes, a ``log_severity`` (logging level name) and a coarse,
# secret-free ``internal_category`` for internal grouping. The public envelope
# only ever exposes ``code`` + the static ``message``.


class AdapterError(SignalNestError):
    """Base for infrastructure adapter / worker-fleet faults.

    Subclasses set class-level ``code`` / ``status_code`` / ``retryable`` /
    ``log_severity`` / ``internal_category`` and a ``default_message``. Callers
    should construct these with **no** argument (using the safe default) or with
    another *static* safe string — never with a driver/SDK message.
    """

    status_code = 503
    code = "adapter_error"
    retryable = True
    log_severity = "error"
    internal_category = "adapter"
    default_message = "An infrastructure dependency is currently unavailable"

    def __init__(self, message: str | None = None, *, code: str | None = None):
        super().__init__(message or self.default_message, code=code)


class PostgresUnavailableError(AdapterError):
    code = "postgres_unavailable"
    internal_category = "database"
    default_message = "The database is temporarily unavailable"


class RedisUnavailableError(AdapterError):
    # The cache is best-effort, so a Redis outage is a warning, not an error.
    code = "redis_unavailable"
    log_severity = "warning"
    internal_category = "cache"
    default_message = "The cache backend is temporarily unavailable"


class RedisNotifyFailedError(AdapterError):
    # The durable job is already committed to the DB; a lost wake-up signal only
    # delays pickup until the next DB poll, so this is non-fatal and not retried.
    code = "redis_notify_failed"
    retryable = False
    log_severity = "warning"
    internal_category = "coordination"
    default_message = "Failed to publish a job-availability notification"


class ObjectStorageUnavailableError(AdapterError):
    code = "object_storage_unavailable"
    internal_category = "storage"
    default_message = "Object storage is temporarily unavailable"


class InvalidObjectKeyError(AdapterError):
    # A programming/client error, not an outage: reject fast, do not retry.
    status_code = 400
    code = "invalid_object_key"
    retryable = False
    log_severity = "warning"
    internal_category = "storage"
    default_message = "The object key is invalid"


class ObjectTooLargeError(AdapterError):
    status_code = 413
    code = "object_too_large"
    retryable = False
    log_severity = "warning"
    internal_category = "storage"
    default_message = "The object exceeds the maximum permitted size"


class WorkerRegistrationFailedError(AdapterError):
    code = "worker_registration_failed"
    internal_category = "worker"
    default_message = "The worker could not register with the fleet registry"


class WorkerHeartbeatFailedError(AdapterError):
    code = "worker_heartbeat_failed"
    log_severity = "warning"
    internal_category = "worker"
    default_message = "The worker heartbeat could not be recorded"


class WorkerAlreadyActiveError(AdapterError):
    status_code = 409
    code = "worker_already_active"
    retryable = False
    internal_category = "worker"
    default_message = "A worker with this id is already registered as active"


class WorkerStaleError(AdapterError):
    code = "worker_stale"
    retryable = False
    log_severity = "warning"
    internal_category = "worker"
    default_message = "The worker registration is stale"


class AdapterInitializationError(AdapterError):
    retryable = False
    code = "adapter_initialization_failed"
    default_message = "A production adapter failed to initialize"


class ProductionAdapterNotConfiguredError(AdapterNotConfiguredError):
    """A production adapter is required by policy but not configured.

    Kept distinct from :class:`AdapterNotConfiguredError` so operator diagnostics
    can tell "not wired up at all" apart from "explicitly required but missing".
    """

    code = "production_adapter_not_configured"


def _envelope(code: str, message: str, details: object | None = None) -> dict:
    body = {"error": {"code": code, "message": message, "request_id": request_id_ctx.get()}}
    if details is not None:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(SignalNestError)
    async def _domain(_: Request, exc: SignalNestError):
        return JSONResponse(status_code=exc.status_code, content=_envelope(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_envelope("validation_error", "Request validation failed", exc.errors()),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logger.exception("Unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=_envelope("internal_error", "An unexpected error occurred"),
        )
