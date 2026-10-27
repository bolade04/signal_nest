"""P6-PLAT-1 Tier 2 proof: the exact build boots with ``ENVIRONMENT=production`` (strategy B).

Operator decision 2026-10-27: production runs the explicitly acknowledged, bounded
in-process per-run similarity (N_max = 100, hard ceiling 100). This module proves the
production boot contract against a disposable PostgreSQL, in clean subprocesses that
read their configuration from the environment only - the way the containers start:

1. the **migration actor** (``python -m app.db.migrate``) runs with
   ``ENVIRONMENT=production SN_MIGRATION_MODE=1`` and **no ``REDIS_URL``** although
   the task environment selects the Redis backends (the unnecessary Redis requirement
   is fixed without weakening ordinary runtime validation - see
   ``test_migrate_upgrade_verify``), and leaves the database at the code head;
2. the **API** boots the production full profile (``APP_MODE=full``, Redis cache and
   limiter, S3 storage, SES mail, a real-provider placeholder key that is never used,
   ``VECTOR_INPROCESS_PRODUCTION_APPROVED=true``) through its lifespan, serves
   ``/health``, and ``run_readiness_probes()`` reports ``ready`` with the ``vector``
   probe HEALTHY and stating the bound.

Redis: CI has no Redis service (``ci.yml`` services = ``postgres:16`` only), so the boot
is labelled **"Redis doubled by fakeredis"** - every redis-py pool built from a URL is
backed by fakeredis at the ``redis.ConnectionPool.from_url`` seam, inside the booted
process only. Set ``TEST_REDIS_URL`` to a reachable Redis to run the same proof against
a real one (no double). No probe is weakened: the cache and rate-limit probes still ping.

Gate: :func:`require_postgres` - with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI
(``CI=true``) and skip only on a developer machine. The PostgreSQL DSN never reaches the
logs; every subprocess's output is checked for it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.tests._auth2_support import pg_database, require_postgres
from app.tests.test_postgres_migration_proof import HEAD, _version

API_DIR = Path(__file__).resolve().parents[2]

#: Application variables stripped from the inherited environment so each subprocess
#: sees exactly the profile under test (CI exports APP_MODE/ENVIRONMENT/VECTOR_BACKEND/
#: LLM_PROVIDER for the ordinary suite).
_SCRUB_PREFIXES = (
    "APP_MODE", "ENVIRONMENT", "DATABASE_URL", "SN_MIGRATION_MODE", "SECRET_KEY",
    "REDIS_", "CACHE_", "QUEUE_", "RATE_LIMIT_", "STORAGE_", "S3_", "VECTOR_",
    "EMBEDDING_", "LLM_", "ANTHROPIC_", "OPENAI_", "MAIL_", "PUBLIC_WEB_ORIGIN",
    "GEOCODER_", "METRICS_", "TRACING_", "SIGNALNEST_", "CORS_", "FORWARDED_",
    "REQUIRE_WORKER_FLEET", "SN_BOOT_PROOF_REDIS",
)

#: Non-secret production backend selections. The ECS migration task today carries only
#: ENVIRONMENT + SN_MIGRATION_MODE (+ DATABASE_URL) because staging has no backend rules;
#: a production migration task must also carry these four (P6-INF-1 composition).
_PRODUCTION_SELECTIONS = {
    "APP_MODE": "full",
    "CACHE_BACKEND": "redis",
    "QUEUE_BACKEND": "redis",
    "RATE_LIMIT_BACKEND": "redis",
    "STORAGE_BACKEND": "s3",
    "VECTOR_BACKEND": "bruteforce",
    "VECTOR_INPROCESS_PRODUCTION_APPROVED": "true",
}

#: Placeholder (non-secret, never used) values that satisfy the full-profile presence
#: rules: no provider, mail or S3 call is made at boot (the probes are config-only).
_PRODUCTION_FULL_PROFILE = {
    **_PRODUCTION_SELECTIONS,
    "SECRET_KEY": "production-boot-proof-placeholder-secret-0123456789",
    "S3_BUCKET": "boot-proof-placeholder-bucket",
    "LLM_PROVIDER": "anthropic",
    "LLM_API_KEY": "placeholder-never-sent-no-call-is-made",
    "MAIL_BACKEND": "ses",
    "MAIL_FROM_ADDRESS": "no-reply@example.com",
    "MAIL_SES_REGION": "us-east-1",
    "PUBLIC_WEB_ORIGIN": "https://app.example.com",
}


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def pg_url() -> Iterator[str]:
    with pg_database("sn_plat1") as url:
        yield url


def _base_env(url: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_SCRUB_PREFIXES)}
    env["ENVIRONMENT"] = "production"
    env["DATABASE_URL"] = url
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], cwd=API_DIR, env=env, capture_output=True, text=True
    )


def _assert_no_dsn(url: str, out: str) -> None:
    assert url not in out and "signalnest@" not in out, "the DSN must never reach the logs"


def _migrate_as_production_actor(url: str) -> str:
    env = _base_env(url)
    env.update(_PRODUCTION_SELECTIONS)
    env["SN_MIGRATION_MODE"] = "1"
    assert "REDIS_URL" not in env and "SECRET_KEY" not in env and "LLM_API_KEY" not in env
    result = _run(["-m", "app.db.migrate"], env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    _assert_no_dsn(url, out)
    return out


# --------------------------------------------------------------------------- #
# 1. Migration actor: production, migration mode, no REDIS_URL
# --------------------------------------------------------------------------- #
def test_production_migration_actor_runs_without_redis_url(pg_url):
    out = _migrate_as_production_actor(pg_url)
    assert "migrate.upgrade_verify.done" in out
    assert _version(pg_url) == [HEAD]
    # ``check`` with the same environment (still no Redis) reports compatible.
    env = _base_env(pg_url)
    env.update(_PRODUCTION_SELECTIONS)
    env["SN_MIGRATION_MODE"] = "1"
    check = _run(["-m", "app.db.migrate", "check"], env)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "migrate.check" in check.stdout + check.stderr


def test_production_migration_actor_still_refuses_without_the_vector_acknowledgement(pg_url):
    # The exemption relaxes only the Redis presence rules: an unacknowledged production
    # migration actor fails Settings construction, before touching the database.
    env = _base_env(pg_url)
    env.update({**_PRODUCTION_SELECTIONS, "VECTOR_INPROCESS_PRODUCTION_APPROVED": "false"})
    env["SN_MIGRATION_MODE"] = "1"
    result = _run(["-m", "app.db.migrate"], env)
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "VECTOR_INPROCESS_PRODUCTION_APPROVED=true" in out
    _assert_no_dsn(pg_url, out)


# --------------------------------------------------------------------------- #
# 2. API boot: production full profile, lifespan, readiness ready + vector HEALTHY
# --------------------------------------------------------------------------- #
def _boot_production_api(url: str) -> dict:
    env = _base_env(url)
    env.update(_PRODUCTION_FULL_PROFILE)
    real_redis = os.environ.get("TEST_REDIS_URL")
    if real_redis:
        env["REDIS_URL"] = real_redis
        env["SN_BOOT_PROOF_REDIS"] = "real"
    else:
        # Never connected: every pool is doubled by fakeredis inside the process.
        env["REDIS_URL"] = "redis://redis.invalid:6379/0"
        env["SN_BOOT_PROOF_REDIS"] = "fakeredis"
    env.pop("SN_MIGRATION_MODE", None)
    result = _run(["-m", "app.tests._production_boot_probe"], env)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    _assert_no_dsn(url, out)
    assert "redis.invalid" not in out and "placeholder-never-sent" not in out
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("BOOT-PROOF: ")]
    assert len(lines) == 1, out
    return json.loads(lines[0][len("BOOT-PROOF: "):])


def test_production_full_profile_boots_and_is_ready(pg_url):
    _migrate_as_production_actor(pg_url)
    proof = _boot_production_api(pg_url)
    expected_redis = "real" if os.environ.get("TEST_REDIS_URL") else "doubled by fakeredis"
    assert proof["redis"] == expected_redis
    assert (proof["environment"], proof["app_mode"], proof["migration_mode"]) == (
        "production", "full", False,
    )
    assert proof["vector_backend"] == "bruteforce"
    assert proof["vector_inprocess_production_approved"] is True
    assert proof["vector_inprocess_max_signals_per_run"] == 100
    assert proof["health_status"] == 200
    assert proof["ready"] is True, proof["probes"]
    assert proof["blocking"] == []

    probes = {p["name"]: p for p in proof["probes"]}
    vector = probes["vector"]
    assert vector["status"] == "healthy" and vector["required"] is True
    assert "N_max=100 signals per run" in vector["summary"]
    assert vector["detail"] == (
        "in-process per-run similarity; bound N_max=100 signals per run "
        "(engineering capacity bound); no cross-run, cross-workspace or history-wide "
        "similarity"
    )
    # The required backends were actively verified against the disposable PostgreSQL
    # (and the Redis double / real Redis): never a config-only green.
    for name in ("database", "durable_queue", "cache", "queue"):
        assert probes[name]["status"] == "healthy", probes[name]
    assert probes["rate_limit"]["status"] == "healthy", probes["rate_limit"]
    # External providers are config-only and degraded, not healthy, with no live call.
    assert probes["storage"]["status"] == "degraded"
    assert probes["llm"]["status"] == "degraded"

    assert proof["runtime_all_configured"] is True
    assert proof["runtime_vector"]["backend"] == "bruteforce"
    assert proof["runtime_vector"]["configured"] is True
    assert "no cross-run" in proof["runtime_vector"]["detail"]
