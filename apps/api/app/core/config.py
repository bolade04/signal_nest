"""Application configuration.

Two runtime modes are supported:

* ``local`` (default): zero-dependency mode using SQLite, an in-process job queue,
  an in-memory cache, local-file object storage, a brute-force vector index and the
  deterministic mock LLM provider. Runs with no external services.
* ``full``: production-shaped mode backed by PostgreSQL + pgvector, Redis and
  S3-compatible storage. A real LLM provider is required.

Configuration is validated at startup; ``full``/``production`` deployments fail fast
when required credentials or services are not configured.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppMode = Literal["local", "full"]
Environment = Literal["development", "staging", "production", "test"]
LLMProvider = Literal["mock", "openai", "anthropic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Never echo raw input (which may carry secret-bearing values such as
        # database_url or api keys) into ValidationError output / logs.
        hide_input_in_errors=True,
    )

    # --- General -------------------------------------------------------------
    app_name: str = "SignalNest API"
    app_mode: AppMode = "local"
    environment: Environment = "development"
    debug: bool = True
    api_prefix: str = "/api/v1"

    # --- Security ------------------------------------------------------------
    # Secret-bearing fields set repr=False so they never appear in model reprs,
    # tracebacks or pydantic ValidationError output (config errors are logged).
    secret_key: str = Field(default="dev-insecure-change-me", repr=False)
    access_token_expire_minutes: int = 60 * 12
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Database ------------------------------------------------------------
    # Default: local SQLite file next to the api app.
    database_url: str = Field(default="sqlite:///./signalnest.db", repr=False)

    # --- Full-mode services (optional in local mode) -------------------------
    redis_url: str | None = Field(default=None, repr=False)
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str | None = Field(default=None, repr=False)
    s3_endpoint_url: str | None = Field(default=None, repr=False)
    local_storage_dir: str = "./storage"

    # --- Vector search -------------------------------------------------------
    vector_backend: Literal["bruteforce", "pgvector"] = "bruteforce"
    embedding_dim: int = 256

    # --- Jobs ----------------------------------------------------------------
    queue_backend: Literal["inprocess", "redis"] = "inprocess"
    cache_backend: Literal["memory", "redis"] = "memory"

    # --- LLM -----------------------------------------------------------------
    llm_provider: LLMProvider = "mock"
    llm_model: str | None = None
    llm_api_key: str | None = Field(default=None, repr=False)
    llm_timeout_seconds: int = 30
    llm_max_retries: int = 2
    llm_temperature: float = 0.0
    llm_mock_seed: str = "signalnest-dev"
    llm_allow_dev_fallback: bool = False

    # --- Readiness probes ----------------------------------------------------
    #: Wall-clock budget for a single readiness probe. Bounded so a hung backend
    #: cannot stall the readiness endpoint. Must be > 0 and <= the total budget.
    readiness_probe_timeout_seconds: float = 2.0
    #: Total wall-clock budget for the whole readiness sweep across all probes.
    readiness_total_timeout_seconds: float = 5.0

    # --- Derived helpers -----------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production_like(self) -> bool:
        return self.environment in ("staging", "production")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def _validate_runtime(self) -> Settings:
        errors: list[str] = []

        if self.app_mode == "full":
            if self.is_sqlite:
                errors.append("app_mode=full requires a PostgreSQL database_url")
            if self.queue_backend == "redis" and not self.redis_url:
                errors.append("queue_backend=redis requires redis_url")
            if self.cache_backend == "redis" and not self.redis_url:
                errors.append("cache_backend=redis requires redis_url")

        # Production must run on the production-shaped runtime end to end. Local
        # zero-dependency backends are development conveniences and must never be
        # silently active in production: each is rejected by name (no secrets are
        # referenced) so misconfiguration fails during Settings construction,
        # before the process can serve traffic.
        if self.is_production:
            if self.app_mode != "full":
                errors.append("environment=production requires app_mode=full")
            if self.is_sqlite:
                errors.append(
                    "environment=production requires a PostgreSQL database "
                    "(SQLite is a local-only backend)"
                )
            if self.queue_backend == "inprocess":
                errors.append(
                    "environment=production requires a durable queue backend "
                    "(queue_backend=inprocess is local-only)"
                )
            if self.cache_backend == "memory":
                errors.append(
                    "environment=production requires a shared cache backend "
                    "(cache_backend=memory is local-only)"
                )
            if self.storage_backend == "local":
                errors.append(
                    "environment=production requires durable object storage "
                    "(storage_backend=local is local-only)"
                )
            if self.vector_backend == "bruteforce":
                errors.append(
                    "environment=production requires a persistent vector backend "
                    "(vector_backend=bruteforce is local-only)"
                )

        if self.is_production_like:
            if self.secret_key == "dev-insecure-change-me" or not self.secret_key.strip():
                errors.append(
                    "secret_key must be set to a strong, non-empty value in "
                    "staging/production"
                )
            if self.llm_provider == "mock":
                errors.append(
                    "mock LLM provider is not allowed in staging/production; "
                    "configure llm_provider=openai|anthropic"
                )
            if self.llm_allow_dev_fallback:
                errors.append("llm_allow_dev_fallback must be false in staging/production")
            if self.storage_backend == "s3" and not self.s3_bucket:
                errors.append("storage_backend=s3 requires s3_bucket")

        # A database is required in every mode; an empty/whitespace URL is never
        # valid and must fail fast rather than surfacing as a runtime error.
        if not self.database_url.strip():
            errors.append("database_url must not be empty")

        if self.llm_provider in ("openai", "anthropic") and not self.llm_api_key:
            errors.append(f"llm_provider={self.llm_provider} requires llm_api_key")

        if self.readiness_probe_timeout_seconds <= 0:
            errors.append("readiness_probe_timeout_seconds must be greater than 0")
        if self.readiness_total_timeout_seconds <= 0:
            errors.append("readiness_total_timeout_seconds must be greater than 0")
        if self.readiness_probe_timeout_seconds > self.readiness_total_timeout_seconds:
            errors.append(
                "readiness_probe_timeout_seconds must be <= "
                "readiness_total_timeout_seconds"
            )

        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
