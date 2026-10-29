"""Boot the API with the production profile read from the environment (P6-PLAT-1 Tier 2).

Not a test module. ``python -m app.tests._production_boot_probe`` is run by
``test_postgres_production_boot`` in a clean process, the way a container starts: the
``Settings`` come from the environment only, the lifespan's startup schema gate runs
against the configured PostgreSQL, the readiness probes run, and one JSON line prefixed
``BOOT-PROOF:`` reports what happened (secret-free: probe summaries/details and
settings labels only).

Redis: with ``SN_BOOT_PROOF_REDIS=fakeredis`` (the default - CI has no Redis service)
every redis-py connection pool built from a URL is **doubled by fakeredis** at the
``redis.ConnectionPool.from_url`` seam, which covers the readiness probes
(``redis.from_url``), the rate limiter (``BlockingConnectionPool.from_url``) and the
cache (``ConnectionPool.from_url``). With ``SN_BOOT_PROOF_REDIS=real`` nothing is
patched and ``REDIS_URL`` must name a reachable Redis.
"""

from __future__ import annotations

import json
import os
import sys


def _double_redis_with_fakeredis() -> None:
    import fakeredis
    import redis

    server = fakeredis.FakeServer()

    def pool_from_url(cls, url, **kwargs):  # noqa: ANN001 - redis-py signature
        pool_kwargs = {"connection_class": fakeredis.FakeConnection, "server": server}
        if "max_connections" in kwargs:
            pool_kwargs["max_connections"] = kwargs["max_connections"]
        if issubclass(cls, redis.BlockingConnectionPool) and "timeout" in kwargs:
            pool_kwargs["timeout"] = kwargs["timeout"]
        return cls(**pool_kwargs)

    redis.ConnectionPool.from_url = classmethod(pool_from_url)


def main() -> int:
    mode = os.environ.get("SN_BOOT_PROOF_REDIS", "fakeredis")
    if mode == "fakeredis":
        _double_redis_with_fakeredis()
    elif mode != "real":
        print(f"BOOT-PROOF-ERROR: unknown SN_BOOT_PROOF_REDIS={mode!r}", file=sys.stderr)
        return 2

    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.core.runtime import build_runtime_report
    from app.main import app
    from app.system.probes import run_readiness_probes

    settings = get_settings()
    with TestClient(app) as client:  # enters the lifespan: schema gate, metrics, logs
        health_status = client.get("/health").status_code
        report = run_readiness_probes(settings)
        runtime = build_runtime_report(settings)

    vector = next(c for c in runtime.capabilities if c.name == "vector")
    payload = {
        "redis": "doubled by fakeredis" if mode == "fakeredis" else "real",
        "environment": settings.environment,
        "app_mode": settings.app_mode,
        "migration_mode": settings.migration_mode,
        "vector_backend": settings.vector_backend,
        "vector_inprocess_max_signals_per_run": settings.vector_inprocess_max_signals_per_run,
        "vector_inprocess_production_approved": settings.vector_inprocess_production_approved,
        "health_status": health_status,
        "ready": report.ready,
        "blocking": [r.name for r in report.blocking],
        "probes": [r.to_operator_dict() for r in report.results],
        "runtime_all_configured": runtime.all_configured,
        "runtime_vector": vector.to_public_dict(),
    }
    print("BOOT-PROOF: " + json.dumps(payload, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
