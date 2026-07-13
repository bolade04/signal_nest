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


# --- Capability registry: classification + secret hygiene -----------------------
def test_local_defaults_are_fully_local_and_configured() -> None:
    report = build_runtime_report(Settings(_env_file=None))
    assert report.is_local_mode is True
    assert report.all_configured is True
    assert report.unconfigured == []
    names = {c.name for c in report.capabilities}
    assert names == {"database", "queue", "cache", "vector", "storage", "llm"}


def test_unconfigured_production_backends_are_flagged_not_healthy() -> None:
    settings = Settings(
        _env_file=None,
        queue_backend="redis",  # no redis_url
        storage_backend="s3",  # no bucket
        vector_backend="pgvector",  # sqlite db
    )
    report = build_runtime_report(settings)
    unconfigured = {c.name for c in report.unconfigured}
    assert unconfigured == {"queue", "storage", "vector"}
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
