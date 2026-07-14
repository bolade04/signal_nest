"""Runtime capability model.

A read-only view over :class:`~app.core.config.Settings` that describes which
backend each infrastructure capability is bound to and whether that backend is
correctly configured. It exists so health/readiness reporting and future
operational tooling can reason about the runtime *without* re-deriving the
selection logic and *without ever touching secrets*.

Design rules for this module:

* **Secret-free.** Never surface ``database_url``, ``redis_url``, ``llm_api_key``,
  bucket names or endpoints. Only non-sensitive labels (backend name, booleans).
* **Pure.** No network, no I/O, no adapter construction. It reads settings only.
* **Explicit not-configured state.** A production backend that lacks its required
  configuration reports ``configured=False`` rather than appearing healthy.

The configuration *validator* in ``config.py`` still fails fast at startup for
invalid full/production combinations; this module is the runtime introspection
counterpart used after the process is up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings, get_settings

# Backends that run with zero external dependencies (the default local mode).
_LOCAL_BACKENDS = {
    "database": {"sqlite"},
    "queue": {"inprocess"},
    "durable_queue": {"local"},
    "cache": {"memory"},
    "vector": {"bruteforce"},
    "storage": {"local"},
    "llm": {"mock"},
    "worker_registry": {"inprocess"},
}


@dataclass(frozen=True)
class CapabilityStatus:
    """Non-sensitive status of a single runtime capability."""

    name: str
    backend: str
    configured: bool
    is_local: bool
    requires_external: bool
    #: Secret-free, human-readable note explaining a not-configured state.
    detail: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        """Serialize to a secret-free mapping suitable for API responses."""
        return {
            "name": self.name,
            "backend": self.backend,
            "configured": self.configured,
            "is_local": self.is_local,
            "requires_external": self.requires_external,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RuntimeReport:
    """Aggregate runtime view built from the current settings."""

    app_mode: str
    environment: str
    llm_provider: str
    is_local_mode: bool
    capabilities: list[CapabilityStatus] = field(default_factory=list)

    @property
    def all_configured(self) -> bool:
        return all(c.configured for c in self.capabilities)

    @property
    def unconfigured(self) -> list[CapabilityStatus]:
        return [c for c in self.capabilities if not c.configured]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "app_mode": self.app_mode,
            "environment": self.environment,
            "llm_provider": self.llm_provider,
            "is_local_mode": self.is_local_mode,
            "all_configured": self.all_configured,
            "capabilities": [c.to_public_dict() for c in self.capabilities],
        }

    def to_summary_dict(self) -> dict[str, object]:
        """Coarse, non-privileged runtime summary.

        Safe for any authenticated customer: it reports only the runtime mode
        and an aggregate readiness flag and deliberately omits the per-capability
        backend topology (which is operator-only introspection).
        """
        return {
            "app_mode": self.app_mode,
            "environment": self.environment,
            "is_local_mode": self.is_local_mode,
            "all_configured": self.all_configured,
        }


def _status(
    name: str, backend: str, *, configured: bool, detail: str | None = None
) -> CapabilityStatus:
    is_local = backend in _LOCAL_BACKENDS.get(name, set())
    return CapabilityStatus(
        name=name,
        backend=backend,
        configured=configured,
        is_local=is_local,
        requires_external=not is_local,
        detail=detail,
    )


def build_runtime_report(settings: Settings | None = None) -> RuntimeReport:
    """Derive the runtime capability report from settings (no secrets, no I/O)."""
    s = settings or get_settings()

    database_backend = "sqlite" if s.is_sqlite else "postgresql"
    caps = [
        # SQLite is always ready; a non-sqlite URL is considered configured by
        # virtue of being set (credentials themselves are never surfaced here).
        _status("database", database_backend, configured=True),
        _status(
            "queue",
            s.queue_backend,
            configured=s.queue_backend != "redis" or s.redis_url is not None,
            detail=None if s.queue_backend != "redis" or s.redis_url else "redis_url is not set",
        ),
        # Durable job execution store. Only the local SQLite-backed backend is
        # implemented; it is always available once the schema is migrated.
        _status("durable_queue", s.job_queue_backend, configured=True),
        _status(
            "cache",
            s.cache_backend,
            configured=s.cache_backend != "redis" or s.redis_url is not None,
            detail=None if s.cache_backend != "redis" or s.redis_url else "redis_url is not set",
        ),
        _status(
            "vector",
            s.vector_backend,
            configured=s.vector_backend != "pgvector" or not s.is_sqlite,
            detail=(
                "pgvector requires a PostgreSQL database"
                if s.vector_backend == "pgvector" and s.is_sqlite
                else None
            ),
        ),
        _status(
            "storage",
            s.storage_backend,
            configured=s.storage_backend != "s3" or s.s3_bucket is not None,
            detail=None if s.storage_backend != "s3" or s.s3_bucket else "s3_bucket is not set",
        ),
        _status(
            "llm",
            s.llm_provider,
            configured=s.llm_provider == "mock" or s.llm_api_key is not None,
            detail=(
                "llm_api_key is not set"
                if s.llm_provider != "mock" and not s.llm_api_key
                else None
            ),
        ),
        # Worker fleet coordination. The registry itself lives in the primary
        # database (always available once migrated); the capability's backend
        # reflects the wake-up transport the fleet coordinates over — in-process
        # locally, Redis in full mode (which requires a redis_url to be usable).
        _status(
            "worker_registry",
            s.queue_backend,
            configured=s.queue_backend != "redis" or s.redis_url is not None,
            detail=(
                "redis_url is not set"
                if s.queue_backend == "redis" and not s.redis_url
                else None
            ),
        ),
    ]

    is_local_mode = all(c.is_local for c in caps)
    return RuntimeReport(
        app_mode=s.app_mode,
        environment=s.environment,
        llm_provider=s.llm_provider,
        is_local_mode=is_local_mode,
        capabilities=caps,
    )
