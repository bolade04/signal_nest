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
