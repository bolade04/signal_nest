"""Phase 3A runtime-foundation unit tests (no DB, no FastAPI).

Covers the four completion guarantees that are testable in isolation:
  * production mode rejects missing required configuration (no silent fallback),
  * the capability registry classifies backends and never exposes secrets,
  * job envelopes are versioned and deterministic (and backward compatible),
  * the tenant execution context makes isolation explicit.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.runtime import build_runtime_report
from app.jobs.context import ExecutionContext, scope_matches
from app.jobs.contracts import CURRENT_CONTRACT_VERSION, JobEnvelope, unwrap, wrap


# --- Configuration: explicit rejection, no silent production fallback -----------
def test_full_mode_rejects_sqlite() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(app_mode="full", database_url="sqlite:///./x.db")
    assert "PostgreSQL" in str(exc.value)


def test_production_rejects_mock_llm() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            app_mode="full",
            environment="production",
            database_url="postgresql://u:p@localhost/db",
            secret_key="a-real-secret",
            llm_provider="mock",
        )
    assert "mock LLM provider is not allowed" in str(exc.value)


def test_production_requires_secret_key() -> None:
    with pytest.raises(ValidationError):
        Settings(
            app_mode="full",
            environment="staging",
            database_url="postgresql://u:p@localhost/db",
            llm_provider="openai",
            llm_api_key="sk-test",
        )  # secret_key left at insecure default -> rejected


def test_real_llm_requires_api_key() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(llm_provider="openai", llm_api_key=None)
    assert "requires llm_api_key" in str(exc.value)


def test_production_rejects_dev_fallback() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(
            app_mode="full",
            environment="production",
            database_url="postgresql://u:p@localhost/db",
            secret_key="a-real-secret",
            llm_provider="openai",
            llm_api_key="sk-test",
            llm_allow_dev_fallback=True,
        )
    assert "llm_allow_dev_fallback must be false" in str(exc.value)


# --- Production requires the production-shaped runtime (follow-up #1) ------------
def _prod_kwargs(**overrides: object) -> dict[str, object]:
    """A valid full/production config; individual fields are overridden per test."""
    base: dict[str, object] = dict(
        _env_file=None,
        app_mode="full",
        environment="production",
        database_url="postgresql://u:p@localhost/db",
        secret_key="a-real-secret",
        queue_backend="redis",
        cache_backend="redis",
        redis_url="redis://localhost:6379/0",
        storage_backend="s3",
        s3_bucket="prod-bucket",
        vector_backend="pgvector",
        llm_provider="openai",
        llm_api_key="sk-test",
    )
    base.update(overrides)
    return base


def test_production_full_config_is_accepted() -> None:
    settings = Settings(**_prod_kwargs())
    assert settings.is_production is True
    assert settings.app_mode == "full"


def test_production_rejects_local_app_mode() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(app_mode="local"))
    assert "environment=production requires app_mode=full" in str(exc.value)


def test_production_rejects_sqlite() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(database_url="sqlite:///./x.db"))
    assert "environment=production requires a PostgreSQL database" in str(exc.value)


def test_production_rejects_inprocess_queue() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(queue_backend="inprocess"))
    assert "durable queue backend" in str(exc.value)


def test_production_rejects_memory_cache() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(cache_backend="memory"))
    assert "shared cache backend" in str(exc.value)


def test_production_rejects_local_storage() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(storage_backend="local"))
    assert "durable object storage" in str(exc.value)


def test_production_rejects_bruteforce_vector() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(vector_backend="bruteforce"))
    assert "persistent vector backend" in str(exc.value)


def test_production_default_local_stack_is_rejected_wholesale() -> None:
    # A production environment left on the local defaults fails during Settings
    # construction, naming every offending capability (no silent fallback).
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, environment="production", secret_key="a-real-secret",
                 llm_provider="openai", llm_api_key="sk-test")
    message = str(exc.value)
    for fragment in ("app_mode=full", "PostgreSQL", "queue", "cache", "storage", "vector"):
        assert fragment in message


def test_s3_storage_requires_bucket_in_production() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(s3_bucket=None))
    assert "storage_backend=s3 requires s3_bucket" in str(exc.value)


def test_production_rejects_blank_secret_key() -> None:
    # A blank/whitespace secret key would sign JWTs with an empty key; it must be
    # rejected in staging/production just like the insecure default.
    with pytest.raises(ValidationError) as exc:
        Settings(**_prod_kwargs(secret_key="   "))
    assert "secret_key must be set" in str(exc.value)


def test_blank_database_url_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url="   ")
    assert "database_url must not be empty" in str(exc.value)


def test_readiness_timeout_bounds_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, readiness_probe_timeout_seconds=0)
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            readiness_probe_timeout_seconds=10.0,
            readiness_total_timeout_seconds=5.0,
        )
    assert "must be <=" in str(exc.value)


def test_error_message_never_contains_secret_values() -> None:
    # Rejecting a production misconfiguration must not echo secret-bearing values.
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            environment="production",
            app_mode="local",
            database_url="postgresql://user:SUPERSECRET@db-host/db",
            secret_key="another-SUPERSECRET-value",
        )
    assert "SUPERSECRET" not in str(exc.value)


# --- Capability registry: classification + secret hygiene -----------------------
def test_local_defaults_are_fully_local_and_configured() -> None:
    report = build_runtime_report(Settings(_env_file=None))
    assert report.is_local_mode is True
    assert report.all_configured is True
    assert report.unconfigured == []
    names = {c.name for c in report.capabilities}
    assert names == {
        "database",
        "queue",
        "durable_queue",
        "cache",
        "vector",
        "storage",
        "llm",
        "worker_registry",
    }


def test_unconfigured_production_backends_are_flagged_not_healthy() -> None:
    settings = Settings(
        _env_file=None,
        queue_backend="redis",  # no redis_url
        storage_backend="s3",  # no bucket
        vector_backend="pgvector",  # sqlite db
    )
    report = build_runtime_report(settings)
    unconfigured = {c.name for c in report.unconfigured}
    # The worker registry coordinates over the queue transport, so selecting Redis
    # without a redis_url leaves both the queue and the fleet coordination
    # unconfigured.
    assert unconfigured == {"queue", "storage", "vector", "worker_registry"}
    assert report.all_configured is False
    assert report.is_local_mode is False


def test_capability_public_dict_never_leaks_secrets() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://user:SECRET@db-host:5432/prod",
        redis_url="redis://user:SECRET@cache-host:6379/0",
        storage_backend="s3",
        s3_bucket="private-bucket-name",
        s3_endpoint_url="https://s3.internal",
    )
    report = build_runtime_report(settings)
    blob = repr(report.to_public_dict())
    for secret in ("SECRET", "db-host", "cache-host", "private-bucket-name", "s3.internal"):
        assert secret not in blob


# --- Job contracts: versioned + deterministic + backward compatible -------------
def _ctx() -> ExecutionContext:
    return ExecutionContext.for_scout_request(
        organization_id="org_1", workspace_id="ws_1", location_id="loc_dallas"
    )


def test_envelope_is_versioned_and_deterministic() -> None:
    a = wrap("run_scout_request", _ctx(), {"scout_request_id": "sr_1"})
    b = wrap("run_scout_request", _ctx(), {"scout_request_id": "sr_1"})
    assert a.contract_version == CURRENT_CONTRACT_VERSION == "1"
    assert a.envelope_hash == b.envelope_hash


def test_envelope_hash_changes_with_scope() -> None:
    dallas = wrap("run_scout_request", _ctx(), {"scout_request_id": "sr_1"})
    london = wrap(
        "run_scout_request",
        ExecutionContext.for_scout_request(
            organization_id="org_1", workspace_id="ws_1", location_id="loc_london"
        ),
        {"scout_request_id": "sr_1"},
    )
    assert dallas.envelope_hash != london.envelope_hash


def test_unwrap_roundtrips_envelope() -> None:
    env = wrap("run_scout_request", _ctx(), {"scout_request_id": "sr_1"})
    context, payload = unwrap(env.to_message())
    assert context == _ctx()
    assert payload == {"scout_request_id": "sr_1"}


def test_unwrap_accepts_legacy_bare_payload() -> None:
    context, payload = unwrap({"scout_request_id": "sr_1"})
    assert context is None
    assert payload == {"scout_request_id": "sr_1"}


def test_unwrap_rejects_unknown_contract_version() -> None:
    bad = {
        "contract_version": "999",
        "job_name": "run_scout_request",
        "context": _ctx().model_dump(),
        "payload": {},
    }
    with pytest.raises(ValueError, match="Unsupported job contract version"):
        unwrap(bad)


def test_envelope_message_is_json_serializable() -> None:
    import json

    env = wrap("run_scout_request", _ctx(), {"scout_request_id": "sr_1"})
    # Round-trips through JSON without error (durable-queue safe).
    assert json.loads(json.dumps(env.to_message())) == env.to_message()


# --- Execution context: explicit isolation --------------------------------------
def test_scope_matches_enforces_tenant() -> None:
    ctx = _ctx()
    assert scope_matches(ctx, organization_id="org_1", workspace_id="ws_1") is True
    assert scope_matches(ctx, organization_id="org_2", workspace_id="ws_1") is False
    assert scope_matches(ctx, organization_id="org_1", workspace_id="ws_2") is False


def test_isolation_key_includes_location() -> None:
    assert _ctx().isolation_key == ("org_1", "ws_1", "loc_dallas")


def test_execution_context_is_frozen() -> None:
    ctx = _ctx()
    with pytest.raises(ValidationError):
        ctx.workspace_id = "ws_other"  # type: ignore[misc]


def test_envelope_is_frozen() -> None:
    env = wrap("run_scout_request", _ctx(), {"scout_request_id": "sr_1"})
    with pytest.raises(ValidationError):
        env.job_name = "other"  # type: ignore[misc]


def test_job_envelope_validates_context_type() -> None:
    with pytest.raises(ValidationError):
        JobEnvelope(job_name="x", context={"organization_id": "org_1"}, payload={})  # type: ignore[arg-type]
