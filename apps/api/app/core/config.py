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

    # --- Observability (Phase 3A.4b Batch 2) ---------------------------------
    #: Stable, non-secret service identifier stamped onto every structured log
    #: record and metric (never a hostname, tenant, or credential).
    service_name: str = "signalnest-api"
    #: Structured-log output format. ``auto`` resolves to ``console`` in
    #: development (human-readable) and ``json`` everywhere else (production sinks).
    log_format: Literal["auto", "json", "console"] = "auto"
    #: Master switch for metric emission. Off by default (and in tests) so metrics
    #: are strictly opt-in; core code always depends on the abstraction, never a
    #: hosted vendor. A disabled backend is a no-op, never an error.
    metrics_enabled: bool = False

    # --- Distributed tracing (Phase 3A.4b Batch 3) ---------------------------
    #: Master switch for span emission. Off by default (and in tests) so tracing
    #: is strictly opt-in; core code depends only on the provider-neutral seam,
    #: never a hosted vendor. Disabled → a no-op tracer, never an error.
    tracing_enabled: bool = False
    #: Span exporter selection. ``none`` installs the no-op tracer; ``memory`` the
    #: in-memory test exporter; ``otlp`` an optional, import-guarded OTLP exporter
    #: (only usable if the OpenTelemetry packages are separately installed).
    tracing_exporter: Literal["none", "memory", "otlp"] = "none"
    #: Head sampling ratio for root spans (parent decisions are always honored).
    #: Bounded 0.0–1.0; conservative default keeps trace volume low in production.
    tracing_sample_ratio: float = 0.05
    #: OTLP collector endpoint (only read when ``tracing_exporter=otlp``). Secret-
    #: bearing/host-identifying, so repr=False and never logged in full.
    otlp_endpoint: str | None = Field(default=None, repr=False)
    #: Bounded per-export timeout for the exporter (seconds). Must be > 0.
    tracing_export_timeout_seconds: float = 10.0
    #: Bounded flush budget on shutdown (seconds). Must be > 0 so shutdown always
    #: terminates even if the collector is slow or unreachable.
    tracing_shutdown_flush_seconds: float = 5.0
    #: Upper bound on buffered spans before the processor drops (never blocks the
    #: caller). Must be >= 1.
    tracing_max_queue_size: int = 2048
    #: Wire format for trace-context propagation. Only W3C ``tracecontext`` is
    #: implemented (the ``traceparent`` header / persisted job trace context).
    tracing_propagation: Literal["tracecontext"] = "tracecontext"

    # --- Security ------------------------------------------------------------
    # Secret-bearing fields set repr=False so they never appear in model reprs,
    # tracebacks or pydantic ValidationError output (config errors are logged).
    secret_key: str = Field(default="dev-insecure-change-me", repr=False)
    access_token_expire_minutes: int = 60 * 12
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Database ------------------------------------------------------------
    # Default: local SQLite file next to the api app.
    database_url: str = Field(default="sqlite:///./signalnest.db", repr=False)

    # --- PostgreSQL connection pool (full mode only) -------------------------
    # These tune the SQLAlchemy QueuePool used for a PostgreSQL engine. They are
    # ignored for SQLite (which uses SQLAlchemy's default single-connection pool).
    #: Steady-state pooled connections held open to PostgreSQL.
    db_pool_size: int = 5
    #: Extra connections opened past the pool under burst load (returned after use).
    db_max_overflow: int = 10
    #: Seconds a caller waits for a pooled connection before failing.
    db_pool_timeout_seconds: float = 30.0
    #: Recycle a pooled connection after this many seconds (defeats stale/idle
    #: server-side disconnects). Must be > 0.
    db_pool_recycle_seconds: float = 1800.0
    #: TCP connect timeout for a new PostgreSQL connection (seconds).
    db_connect_timeout_seconds: float = 10.0
    #: application_name reported to PostgreSQL for observability. Non-secret.
    db_application_name: str = "signalnest-api"

    # --- Full-mode services (optional in local mode) -------------------------
    redis_url: str | None = Field(default=None, repr=False)
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str | None = Field(default=None, repr=False)
    s3_endpoint_url: str | None = Field(default=None, repr=False)
    s3_region: str | None = None
    #: Whether the S3 client uses TLS. Only turn off for a local MinIO over http.
    s3_use_ssl: bool = True
    #: Optional explicit S3 credentials. When unset the SDK's default credential
    #: chain (IAM role, env, profile) is used. Secret-bearing so repr=False.
    s3_access_key_id: str | None = Field(default=None, repr=False)
    s3_secret_access_key: str | None = Field(default=None, repr=False)
    #: Reject an upload whose body exceeds this many bytes (checked before put).
    s3_max_object_bytes: int = 25 * 1024 * 1024
    #: Bounded lifetime for a generated pre-signed URL (seconds).
    s3_signed_url_ttl_seconds: int = 900
    #: Bounded SDK socket/connect timeout and retry ceiling for S3 calls.
    s3_operation_timeout_seconds: float = 10.0
    s3_max_retries: int = 3
    local_storage_dir: str = "./storage"

    # --- Redis tuning (cache + queue coordination, full mode only) -----------
    #: Bounded Redis connection pool size shared by the cache and coordination.
    redis_pool_size: int = 10
    #: Socket/connect timeout for a single Redis operation (seconds).
    redis_operation_timeout_seconds: float = 2.0
    #: Namespace prefix applied to every Redis key this app writes.
    redis_key_prefix: str = "signalnest"
    #: Pub/sub channel used to signal "a durable job is available". Coordination
    #: only — the database remains the authoritative source of queued work.
    redis_notify_channel: str = "signalnest:jobs:available"
    #: Bounded TTL for an optional Redis advisory lock (seconds). Must be > 0.
    redis_lock_ttl_seconds: float = 30.0

    # --- Vector search -------------------------------------------------------
    vector_backend: Literal["bruteforce", "pgvector"] = "bruteforce"
    embedding_dim: int = 256

    # --- Jobs ----------------------------------------------------------------
    queue_backend: Literal["inprocess", "redis"] = "inprocess"
    cache_backend: Literal["memory", "redis"] = "memory"

    # --- Durable job execution / worker (Phase 3A.3) -------------------------
    #: Durable-queue backend. Only the local SQLite-backed store is implemented
    #: in this slice; PostgreSQL/Redis-native adapters are future work.
    job_queue_backend: Literal["local"] = "local"
    #: Stable identity for a worker process. When unset the worker derives a
    #: unique id (host + pid + random suffix) at startup.
    worker_id: str | None = None
    #: Number of jobs a single worker executes concurrently. Bounded; 1 = serial.
    worker_concurrency: int = 1
    #: How long a worker sleeps between empty polls (seconds). Must be > 0 so the
    #: loop never becomes a busy-spin.
    worker_poll_interval_seconds: float = 1.0
    #: Lease duration granted on claim. A claimed/running job whose lease expires
    #: is recovered (re-queued or dead-lettered) by the lease sweep.
    worker_lease_seconds: float = 30.0
    #: Heartbeat cadence while executing. Must be < the lease so a healthy worker
    #: renews its lease before it can expire.
    worker_heartbeat_seconds: float = 10.0
    #: Grace period a worker allows in-flight jobs to finish on shutdown before it
    #: stops. Bounded so shutdown always terminates.
    worker_shutdown_grace_seconds: float = 10.0
    #: Shortened grace applied after a *second* shutdown signal (an operator asking
    #: to exit now). In-flight jobs still running past this window lose their lease
    #: on forced exit and are recovered by the next worker. Bounded, <= the normal
    #: grace.
    worker_force_shutdown_grace_seconds: float = 1.0
    #: Default attempt ceiling for a job when the enqueuer does not specify one.
    job_default_max_attempts: int = 5
    #: Exponential-backoff base and cap for retryable failures (seconds).
    job_retry_base_seconds: float = 2.0
    job_retry_max_seconds: float = 300.0
    #: Reject an enqueue whose serialized payload exceeds this many bytes.
    job_max_payload_bytes: int = 64 * 1024
    #: Upper bound on jobs claimed per poll (bounded batch). 1 = one at a time.
    job_claim_batch_size: int = 1
    #: Optional TTL for a cached readiness result (seconds). 0 disables caching.
    readiness_cache_ttl_seconds: float = 0.0

    # --- Worker fleet registry (Phase 3A.4a) ---------------------------------
    #: Logical worker class recorded in the registry (e.g. "durable-jobs").
    worker_type: str = "durable-jobs"
    #: A registration whose heartbeat is older than this is considered stale.
    #: Must be strictly greater than worker_heartbeat_seconds so a healthy worker
    #: is never flagged stale between beats.
    worker_stale_after_seconds: float = 60.0
    #: How many times worker startup retries a failed registry write before
    #: giving up. Bounded; 0 means a single attempt with no retry.
    worker_registration_retry_limit: int = 3
    #: Delay between registration retries (seconds). Must be >= 0.
    worker_registration_retry_delay_seconds: float = 1.0
    #: Upper bound on the length of a worker id (guards the registry column and
    #: rejects absurd operator-supplied identifiers). Must be >= 1.
    worker_id_max_length: int = 128
    #: When true, API readiness treats a missing/stale worker fleet as a failing
    #: (required) condition. Default false: worker presence is informational and
    #: API liveness never depends on a worker being up.
    require_worker_fleet: bool = False
    #: Non-secret build identifiers recorded in the worker registry for support.
    application_version: str = "0.0.0"
    build_revision: str | None = None

    # --- Scouting connectors (Phase 3B) --------------------------------------
    #: Master switch for the live RSS/news connector. Off by default: the sandbox
    #: fixture path stays authoritative until a product owner turns a specific
    #: connector on (Phase 3 entry criterion: connector policy/legal confirmed).
    connector_rss_enabled: bool = False
    #: Markets the RSS connector is cleared to serve. Empty ⇒ no jurisdiction
    #: restriction beyond the request's own market scoping.
    connector_rss_markets: list[str] = Field(default_factory=list)
    #: Token-bucket rate-limit for RSS fetches: burst capacity and steady refill.
    connector_rss_rate_capacity: int = 5
    connector_rss_rate_refill_per_second: float = 1.0
    #: Bounded retry attempts (incl. the first) for a transient RSS fetch fault.
    connector_rss_max_attempts: int = 3

    # --- Scouting schedules (Phase 3B, SB-B) ---------------------------------
    #: Master switch for recurring scouting schedules (dark by default). While
    #: off, schedule ticks are inert no-ops that self-terminate the chain: no
    #: scheduled scouting run is ever enqueued. Turning this on is the only path
    #: that lets a schedule tick fan out into a scout-request execution.
    scout_scheduling_enabled: bool = False

    # --- Opportunity feedback (Phase 3C, 3C-B) -------------------------------
    #: Master switch for opportunity feedback capture (dark by default). While
    #: off, no feedback write path is exposed to customers: the 3C-B persistence
    #: foundation ships fully inert behind this flag. Capture-only by design —
    #: enabling this never influences scoring, ranking, or model training.
    opportunity_feedback_enabled: bool = False

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
    def db_backend_name(self) -> str:
        """SQLAlchemy dialect backend name for ``database_url``.

        Uses ``make_url`` rather than string prefix matching so that URLs with a
        driver suffix (``postgresql+psycopg://``) resolve to their true backend
        (``postgresql``). Returns ``""`` for an unparseable URL, which the
        validator rejects.
        """
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError

        try:
            return make_url(self.database_url).get_backend_name()
        except (ArgumentError, ValueError):
            return ""

    @property
    def is_sqlite(self) -> bool:
        return self.db_backend_name == "sqlite"

    @property
    def is_postgres(self) -> bool:
        return self.db_backend_name == "postgresql"

    @property
    def effective_log_format(self) -> str:
        """Resolve ``log_format=auto`` to ``console`` in development, else ``json``."""
        if self.log_format != "auto":
            return self.log_format
        return "console" if self.environment == "development" else "json"

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
        elif not self.db_backend_name:
            # Non-empty but unparseable by SQLAlchemy's make_url (malformed URL).
            errors.append("database_url is malformed and cannot be parsed")

        # PostgreSQL connection-pool bounds. These apply whenever a PostgreSQL
        # engine will be built; guard against pools that cannot serve traffic.
        if self.is_postgres:
            if self.db_pool_size < 1:
                errors.append("db_pool_size must be >= 1")
            if self.db_max_overflow < 0:
                errors.append("db_max_overflow must be >= 0")
            if self.db_pool_timeout_seconds <= 0:
                errors.append("db_pool_timeout_seconds must be greater than 0")
            if self.db_pool_recycle_seconds <= 0:
                errors.append("db_pool_recycle_seconds must be greater than 0")
            if self.db_connect_timeout_seconds <= 0:
                errors.append("db_connect_timeout_seconds must be greater than 0")
            if not self.db_application_name.strip():
                errors.append("db_application_name must not be empty")

        if self.llm_provider in ("openai", "anthropic") and not self.llm_api_key:
            errors.append(f"llm_provider={self.llm_provider} requires llm_api_key")

        # Distributed-tracing bounds. Sampling must be a probability; timeouts and
        # queue bounds must be positive so tracing can never stall or busy-block the
        # caller. The OTLP endpoint is only required when tracing is actually enabled
        # with the OTLP exporter (a disabled/memory tracer needs no collector).
        if not 0.0 <= self.tracing_sample_ratio <= 1.0:
            errors.append("tracing_sample_ratio must be between 0.0 and 1.0")
        if self.tracing_export_timeout_seconds <= 0:
            errors.append("tracing_export_timeout_seconds must be greater than 0")
        if self.tracing_shutdown_flush_seconds <= 0:
            errors.append("tracing_shutdown_flush_seconds must be greater than 0")
        if self.tracing_max_queue_size < 1:
            errors.append("tracing_max_queue_size must be >= 1")
        if self.tracing_enabled and self.tracing_exporter == "otlp" and not self.otlp_endpoint:
            errors.append("tracing_exporter=otlp requires otlp_endpoint")

        if self.readiness_probe_timeout_seconds <= 0:
            errors.append("readiness_probe_timeout_seconds must be greater than 0")
        if self.readiness_total_timeout_seconds <= 0:
            errors.append("readiness_total_timeout_seconds must be greater than 0")
        if self.readiness_probe_timeout_seconds > self.readiness_total_timeout_seconds:
            errors.append(
                "readiness_probe_timeout_seconds must be <= "
                "readiness_total_timeout_seconds"
            )

        # Durable job / worker bounds. Each guards against a configuration that
        # would break the lease invariant or spin the worker loop.
        if self.worker_poll_interval_seconds <= 0:
            errors.append("worker_poll_interval_seconds must be greater than 0")
        if self.worker_lease_seconds <= 0:
            errors.append("worker_lease_seconds must be greater than 0")
        if self.worker_heartbeat_seconds <= 0:
            errors.append("worker_heartbeat_seconds must be greater than 0")
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            errors.append(
                "worker_heartbeat_seconds must be < worker_lease_seconds so a "
                "healthy worker renews its lease before it expires"
            )
        if self.worker_shutdown_grace_seconds < 0:
            errors.append("worker_shutdown_grace_seconds must be >= 0")
        if self.worker_force_shutdown_grace_seconds < 0:
            errors.append("worker_force_shutdown_grace_seconds must be >= 0")
        if self.worker_force_shutdown_grace_seconds > self.worker_shutdown_grace_seconds:
            errors.append(
                "worker_force_shutdown_grace_seconds must be <= "
                "worker_shutdown_grace_seconds"
            )
        if self.worker_concurrency < 1:
            errors.append("worker_concurrency must be >= 1")
        if self.job_claim_batch_size < 1:
            errors.append("job_claim_batch_size must be >= 1")
        if self.job_default_max_attempts < 1:
            errors.append("job_default_max_attempts must be >= 1")
        if self.job_retry_base_seconds <= 0:
            errors.append("job_retry_base_seconds must be greater than 0")
        if self.job_retry_max_seconds < self.job_retry_base_seconds:
            errors.append("job_retry_max_seconds must be >= job_retry_base_seconds")
        if self.job_max_payload_bytes <= 0:
            errors.append("job_max_payload_bytes must be greater than 0")
        if self.readiness_cache_ttl_seconds < 0:
            errors.append("readiness_cache_ttl_seconds must be >= 0")

        # Redis tuning bounds. Only enforced when Redis is actually selected for
        # the cache or queue coordination, since the values are otherwise unused.
        # Presence of ``redis_url`` itself is a soft, environment-gated concern
        # (enforced above for full mode); selecting Redis without a URL in local
        # or development is surfaced as an *unconfigured* capability by the runtime
        # report rather than failing Settings construction.
        redis_selected = self.cache_backend == "redis" or self.queue_backend == "redis"
        if redis_selected:
            if self.redis_pool_size < 1:
                errors.append("redis_pool_size must be >= 1")
            if self.redis_operation_timeout_seconds <= 0:
                errors.append("redis_operation_timeout_seconds must be greater than 0")
            if not self.redis_key_prefix.strip():
                errors.append("redis_key_prefix must not be empty")
            if not self.redis_notify_channel.strip():
                errors.append("redis_notify_channel must not be empty")
            if self.redis_lock_ttl_seconds <= 0:
                errors.append("redis_lock_ttl_seconds must be greater than 0")

        # S3 object-storage bounds. Only enforced when S3 is selected. Presence of
        # ``s3_bucket`` is a soft, environment-gated concern (enforced above for
        # staging/production); selecting S3 without a bucket in local/development
        # is surfaced as an *unconfigured* capability, not a construction failure.
        if self.storage_backend == "s3":
            if self.s3_max_object_bytes <= 0:
                errors.append("s3_max_object_bytes must be greater than 0")
            if self.s3_signed_url_ttl_seconds <= 0:
                errors.append("s3_signed_url_ttl_seconds must be greater than 0")
            if self.s3_operation_timeout_seconds <= 0:
                errors.append("s3_operation_timeout_seconds must be greater than 0")
            if self.s3_max_retries < 0:
                errors.append("s3_max_retries must be >= 0")
            # Credentials are all-or-nothing: supplying only one half cannot work
            # and silently falling back to the ambient chain would be surprising.
            if bool(self.s3_access_key_id) != bool(self.s3_secret_access_key):
                errors.append(
                    "s3_access_key_id and s3_secret_access_key must be set together"
                )

        # Worker fleet registry bounds.
        if self.worker_stale_after_seconds <= self.worker_heartbeat_seconds:
            errors.append(
                "worker_stale_after_seconds must be > worker_heartbeat_seconds so a "
                "healthy worker is never flagged stale between heartbeats"
            )
        if self.worker_registration_retry_limit < 0:
            errors.append("worker_registration_retry_limit must be >= 0")
        if self.worker_registration_retry_delay_seconds < 0:
            errors.append("worker_registration_retry_delay_seconds must be >= 0")
        if self.worker_id_max_length < 1:
            errors.append("worker_id_max_length must be >= 1")
        if self.worker_id is not None and len(self.worker_id) > self.worker_id_max_length:
            errors.append(
                f"worker_id must be <= worker_id_max_length ({self.worker_id_max_length})"
            )
        if not self.worker_type.strip():
            errors.append("worker_type must not be empty")

        # Scouting connector bounds. A rate limiter that cannot pass a single
        # token, or a retry policy with no attempts, would silently starve the
        # connector, so reject those at construction.
        if self.connector_rss_rate_capacity < 1:
            errors.append("connector_rss_rate_capacity must be >= 1")
        if self.connector_rss_rate_refill_per_second <= 0:
            errors.append("connector_rss_rate_refill_per_second must be greater than 0")
        if self.connector_rss_max_attempts < 1:
            errors.append("connector_rss_max_attempts must be >= 1")

        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
