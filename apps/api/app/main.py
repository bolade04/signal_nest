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
from app.core.logging import configure_logging, get_logger
from app.core.middleware import CorrelationMiddleware, RateLimitMiddleware
from app.core.tracing import _shutdown_flush, configure_tracing_from_settings

logger = get_logger("signalnest.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Migrations (Alembic) are the authoritative schema path in every mode. We do not
    # call create_all() here; instead we fail fast with a clear instruction if the
    # database has not been migrated yet.
    from sqlalchemy import inspect

    from app.db.session import engine

    tables = inspect(engine).get_table_names()
    if "users" not in tables or "alembic_version" not in tables:
        raise RuntimeError(
            "Database schema is not initialized. Run migrations first:\n"
            "  npm run migrate        # or: npm run demo:setup (migrate + seed)\n"
            f"(database_url={settings.database_url})"
        )
    # Install the configured tracer (no-op unless tracing is enabled). Fails closed:
    # a missing collector/SDK degrades to no-op rather than blocking startup.
    configure_tracing_from_settings(settings)
    logger.info(
        "startup",
        extra={
            "extra_fields": {
                "app_mode": settings.app_mode,
                "environment": settings.environment,
                "llm_provider": settings.llm_provider,
            }
        },
    )
    try:
        yield
    finally:
        # Bounded flush so shutdown always terminates even if the exporter is slow
        # or unreachable; never raises into the lifespan.
        _shutdown_flush(settings.tracing_shutdown_flush_seconds)


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
