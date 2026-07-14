"""FastAPI application factory.

Wires structured logging, correlation + rate-limit middleware, CORS, domain routers and
global exception handlers. Alembic migrations are the authoritative schema path in every
mode; startup fails fast if the database has not been migrated.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.lifecycle import graceful_shutdown
from app.core.logging import configure_logging, get_logger
from app.core.metrics import SERVICE_SHUTDOWNS_TOTAL, SERVICE_STARTUPS_TOTAL, get_metrics
from app.core.middleware import CorrelationMiddleware, RateLimitMiddleware
from app.core.tracing import configure_tracing_from_settings

logger = get_logger("signalnest.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Ordered startup. Config + logging are already applied in create_app(); the
    # tracer is installed first so a startup failure below is still traced. It is a
    # no-op unless tracing is enabled, and fails closed (a missing collector/SDK
    # degrades to no-op) so it can never block startup.
    configure_tracing_from_settings(settings)

    # Schema-compatibility gate (verify, never mutate). Migrations are owned by a
    # single actor (``python -m app.db.migrate``); replicas never migrate. Fails
    # fast with an actionable message if the live schema is uninitialized or behind.
    from app.db.schema import require_startup_schema
    from app.db.session import engine

    compat = require_startup_schema(engine, settings=settings)

    metrics = get_metrics()
    metrics.increment(
        SERVICE_STARTUPS_TOTAL,
        service=settings.service_name,
        environment=settings.environment,
        outcome="ready",
    )
    logger.info(
        "startup",
        extra={
            "extra_fields": {
                "app_mode": settings.app_mode,
                "environment": settings.environment,
                "llm_provider": settings.llm_provider,
                "schema_state": compat.state.value,
            }
        },
    )
    try:
        yield
    finally:
        # Bounded, idempotent drain: count the shutdown, then flush telemetry and
        # close the Redis clients + database pool. Every step is best-effort so a
        # slow exporter or backend can never prevent the process from exiting.
        logger.info("shutdown", extra={"extra_fields": {"environment": settings.environment}})
        metrics.increment(
            SERVICE_SHUTDOWNS_TOTAL,
            service=settings.service_name,
            environment=settings.environment,
            outcome="clean",
        )
        graceful_shutdown(settings)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        docs_url=f"{settings.api_prefix}/docs",
        lifespan=lifespan,
    )

    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(CorrelationMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )

    register_exception_handlers(app)

    @app.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "mode": settings.app_mode}

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
