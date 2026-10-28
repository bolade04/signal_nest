"""P6 platform services: honest, enforced production configuration.

* **P6-PLAT-1** — ``build_index()`` honours ``vector_backend`` and never substitutes
  brute force; a backend this build does not implement is refused by Settings and
  reported NOT_CONFIGURED; the pipeline's dedupe goes through the configured index.
* **P6-PLAT-2** — the dead queue adapter is gone; no setting selects a wake-up
  publisher that no worker subscribes to; production no longer forces
  ``queue_backend``; the ``queue`` readiness entry is neutral and truthful.
* **P6-PLAT-3** — a shared Redis limiter (one atomic ``SET NX EX`` + ``INCR`` per
  request), a bounded per-process limiter, production refusing the per-process one,
  and an explicit, tested behaviour while Redis cannot answer (never unlimited).

The Redis tests use :class:`_FakeRedis`, which implements exactly the commands the
limiter sends (anything else is an ``AttributeError``), plus one run against
``fakeredis`` to pin the redis-py call signatures.
"""

from __future__ import annotations

import importlib.util
import logging

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.config import Settings
from app.core.metrics import DEPENDENCY_OPERATION_TOTAL, InMemoryMetrics, configure_metrics
from app.core.middleware import (
    InMemoryRateLimiter,
    RateLimitMiddleware,
    RedisRateLimiter,
    rate_limit_middleware_options,
)
from app.core.runtime import build_runtime_report
from app.infra.vector import BruteForceIndex, VectorBackendUnavailableError, build_index
from app.privacy.inventory import RetentionClass
from app.system.probes import (
    ProbeStatus,
    _build_probes,
    _check_queue,
    _check_rate_limit,
    _check_vector,
)
from app.tests._platform_support import pgvector_build


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeRedis:
    """The commands :class:`RedisRateLimiter` uses, with Redis semantics.

    ``pipeline(transaction=True)`` queues ``SET`` / ``INCR`` and runs them together in
    ``execute()`` (MULTI/EXEC: nothing interleaves). Keys expire on the injected clock.
    ``fail`` makes the next ``execute()`` raise, as a dropped connection would.
    """

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self._data: dict[str, tuple[int, float | None]] = {}
        self.commands: list[tuple] = []
        self.transaction_flags: list[bool] = []
        self.fail: Exception | None = None

    def _live(self, name: str) -> tuple[int, float | None] | None:
        item = self._data.get(name)
        if item is not None and item[1] is not None and item[1] <= self._clock():
            del self._data[name]
            return None
        return item

    def ttl(self, name: str) -> float | None:
        item = self._live(name)
        return None if item is None or item[1] is None else item[1] - self._clock()

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self, transaction)


class _FakePipeline:
    def __init__(self, server: _FakeRedis, transaction: bool) -> None:
        self._server = server
        self._transaction = transaction
        self._queued: list[tuple] = []

    def __enter__(self) -> _FakePipeline:
        return self

    def __exit__(self, *exc: object) -> None:
        self._queued.clear()

    def set(self, name: str, value: int, ex: int | None = None, nx: bool = False):
        self._queued.append(("SET", name, value, ex, nx))
        return self

    def incr(self, name: str):
        self._queued.append(("INCR", name))
        return self

    def execute(self) -> list:
        server = self._server
        if server.fail is not None:
            raise server.fail
        server.transaction_flags.append(self._transaction)
        results: list = []
        for command in self._queued:
            server.commands.append(command)
            if command[0] == "SET":
                _, name, value, ex, nx = command
                if nx and server._live(name) is not None:
                    results.append(None)
                    continue
                expires = server._clock() + ex if ex is not None else None
                server._data[name] = (int(value), expires)
                results.append(True)
            else:
                _, name = command
                item = server._live(name)
                count = (item[0] if item else 0) + 1
                server._data[name] = (count, item[1] if item else None)
                results.append(count)
        self._queued.clear()
        return results


async def _ok(_request):
    return JSONResponse({"ok": True})


def _client(**middleware_options) -> TestClient:
    app = Starlette(routes=[Route("/", _ok), Route("/health", _ok)])
    app.add_middleware(RateLimitMiddleware, **middleware_options)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# P6-PLAT-3 — per-process limiter: fixed window, bounded
# --------------------------------------------------------------------------- #
class TestInMemoryRateLimiter:
    def test_counts_within_a_window_and_restarts_after_it(self):
        clock = _Clock()
        limiter = InMemoryRateLimiter(window_seconds=60, clock=clock)
        assert [limiter.hit("a") for _ in range(3)] == [1, 2, 3]
        assert limiter.hit("b") == 1  # independent client
        clock.advance(59.9)
        assert limiter.hit("a") == 4  # same window
        clock.advance(0.1)
        assert limiter.hit("a") == 1  # the window closed at exactly 60s

    def test_closed_windows_are_dropped_before_any_open_one(self):
        clock = _Clock()
        limiter = InMemoryRateLimiter(window_seconds=60, max_keys=3, clock=clock)
        limiter.hit("old-1")
        limiter.hit("old-2")
        clock.advance(30)
        limiter.hit("open")
        clock.advance(31)  # old-1/old-2 closed, "open" still has 29s left
        limiter.hit("new")
        assert list(limiter.hits) == ["open", "new"]
        assert limiter.hit("open") == 2  # its count survived

    def test_key_count_never_exceeds_the_bound(self):
        # An address-rotating client cannot grow the table without limit: at the bound
        # the oldest open window is evicted.
        limiter = InMemoryRateLimiter(window_seconds=60, max_keys=50, clock=_Clock())
        for i in range(5_000):
            limiter.hit(f"198.51.100.{i}")
            assert len(limiter.hits) <= 50
        assert len(limiter.hits) == 50
        assert "198.51.100.4999" in limiter.hits
        assert "198.51.100.0" not in limiter.hits

    def test_rejects_unusable_bounds(self):
        with pytest.raises(ValueError):
            InMemoryRateLimiter(window_seconds=0)
        with pytest.raises(ValueError):
            InMemoryRateLimiter(window_seconds=60, max_keys=0)

    def test_middleware_ledger_is_the_bounded_per_process_one(self):
        c = _client(limit=2, window_seconds=60)
        assert [c.get("/").status_code for _ in range(3)] == [200, 200, 429]
        limiter = c.app.middleware_stack
        while not isinstance(limiter, RateLimitMiddleware):
            limiter = limiter.app
        assert limiter.is_shared is False
        assert list(limiter._hits) == ["testclient"]
        limiter._hits.clear()  # the reset the API test helpers rely on
        assert c.get("/").status_code == 200


# --------------------------------------------------------------------------- #
# P6-PLAT-3 — shared Redis limiter
# --------------------------------------------------------------------------- #
class TestRedisRateLimiter:
    def test_one_atomic_set_nx_ex_plus_incr_per_request(self):
        clock = _Clock()
        redis = _FakeRedis(clock)
        limiter = RedisRateLimiter(redis, window_seconds=60, key_prefix="sn")
        assert [limiter.hit("198.51.100.10") for _ in range(3)] == [1, 2, 3]
        key = "sn:ratelimit:198.51.100.10"
        assert redis.commands[:2] == [("SET", key, 0, 60, True), ("INCR", key)]
        assert len(redis.commands) == 6
        assert redis.transaction_flags == [True, True, True]  # MULTI/EXEC every time
        # The expiry is set when the window opens and is not extended by later hits.
        assert redis.ttl(key) == 60

    def test_the_window_closes_on_the_server_clock(self):
        clock = _Clock()
        redis = _FakeRedis(clock)
        limiter = RedisRateLimiter(redis, window_seconds=60, key_prefix="sn")
        limiter.hit("a")
        clock.advance(59)
        assert limiter.hit("a") == 2
        clock.advance(1)
        assert limiter.hit("a") == 1  # expired key: a new window opens

    def test_redis_py_signatures_against_fakeredis(self):
        import fakeredis

        server = fakeredis.FakeStrictRedis()
        limiter = RedisRateLimiter(server, window_seconds=60, key_prefix="sn")
        assert [limiter.hit("2001:db8::1") for _ in range(3)] == [1, 2, 3]
        assert 0 < server.ttl("sn:ratelimit:2001:db8::1") <= 60

    def test_replicas_share_one_budget(self):
        # Two API replicas, one Redis: the limit holds across them (the placeholder
        # allowed limit x replicas).
        redis = _FakeRedis(_Clock())
        replica_a = _client(limit=3, window_seconds=60, redis_client=redis)
        replica_b = _client(limit=3, window_seconds=60, redis_client=redis)
        assert replica_a.get("/").status_code == 200
        assert replica_a.get("/").status_code == 200
        assert replica_b.get("/").status_code == 200
        resp = replica_b.get("/")
        assert resp.status_code == 429
        assert resp.json() == {"error": {"code": "rate_limited", "message": "Too many requests"}}
        assert replica_a.get("/").status_code == 429


# --------------------------------------------------------------------------- #
# P6-PLAT-3 — Redis unavailable: explicit, never unlimited
# --------------------------------------------------------------------------- #
class TestRedisUnavailable:
    @pytest.fixture()
    def metrics(self):
        backend = InMemoryMetrics()
        configure_metrics(backend)
        try:
            yield backend
        finally:
            configure_metrics(None)

    def test_local_mode_keeps_limiting_per_process_and_reports_it(self, metrics, caplog):
        clock = _Clock()
        redis = _FakeRedis(clock)
        redis.fail = ConnectionError("Error connecting to cache-host.internal:6379")
        c = _client(limit=2, window_seconds=60, redis_client=redis, clock=clock)
        with caplog.at_level(logging.WARNING, logger="signalnest.rate_limit"):
            codes = [c.get("/").status_code for _ in range(4)]
        # Never unlimited: the per-process counter still enforces the limit.
        assert codes == [200, 200, 429, 429]
        records = [
            r for r in caplog.records if r.getMessage() == "rate_limit.shared_store_unavailable"
        ]
        assert len(records) == 1  # once per retry interval, not once per request
        assert records[0].extra_fields["error_class"] == "ConnectionError"
        assert records[0].extra_fields["outcome"] == "degraded"
        assert "cache-host" not in caplog.text
        assert metrics.counter_value(
            DEPENDENCY_OPERATION_TOTAL, dependency="redis", operation="rate_limit",
            outcome="degraded",
        ) == 4

    def test_redis_is_retried_only_after_the_interval(self, metrics):
        clock = _Clock()
        redis = _FakeRedis(clock)
        redis.fail = ConnectionError("down")
        c = _client(
            limit=100, window_seconds=60, redis_client=redis, clock=clock,
            retry_after_failure_seconds=5.0,
        )
        assert c.get("/").status_code == 200  # tried Redis, failed, fell back
        redis.fail = None
        clock.advance(4.9)
        assert c.get("/").status_code == 200
        assert redis.commands == []  # still inside the retry interval
        clock.advance(0.1)
        assert c.get("/").status_code == 200
        assert [cmd[0] for cmd in redis.commands] == ["SET", "INCR"]  # back on Redis

    def test_deny_mode_answers_503_until_redis_answers(self, metrics, caplog):
        clock = _Clock()
        redis = _FakeRedis(clock)
        redis.fail = TimeoutError("timed out")
        c = _client(
            limit=100, window_seconds=60, redis_client=redis, clock=clock,
            failure_mode="deny", retry_after_failure_seconds=5.0,
        )
        with caplog.at_level(logging.WARNING, logger="signalnest.rate_limit"):
            resp = c.get("/")
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == "rate_limit_unavailable"
        assert c.get("/").status_code == 503
        assert metrics.counter_value(
            DEPENDENCY_OPERATION_TOTAL, dependency="redis", operation="rate_limit",
            outcome="denied",
        ) == 2
        record = next(
            r for r in caplog.records if r.getMessage() == "rate_limit.shared_store_unavailable"
        )
        assert record.extra_fields["outcome"] == "denied"
        redis.fail = None
        clock.advance(5)
        assert c.get("/").status_code == 200

    def test_deny_mode_keeps_liveness_answering(self, metrics):
        # ALB and container health checks probe /health: denying it would make ECS
        # replace every task during a Redis outage. It is limited per process instead.
        redis = _FakeRedis(_Clock())
        redis.fail = ConnectionError("down")
        c = _client(limit=2, window_seconds=60, redis_client=redis, failure_mode="deny")
        assert c.get("/").status_code == 503
        assert [c.get("/health").status_code for _ in range(3)] == [200, 200, 429]
        assert c.get("/").status_code == 503

    def test_unknown_failure_mode_is_refused(self):
        with pytest.raises(ValueError):
            RateLimitMiddleware(Starlette(), failure_mode="allow")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# P6-PLAT-3 — configuration and wiring
# --------------------------------------------------------------------------- #
def _prod(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        _env_file=None,
        app_mode="full",
        environment="production",
        database_url="postgresql://u:p@localhost/db",
        secret_key="a-real-secret",
        cache_backend="redis",
        redis_url="redis://localhost:6379/0",
        rate_limit_backend="redis",
        storage_backend="s3",
        s3_bucket="prod-bucket",
        vector_backend="pgvector",
        llm_provider="openai",
        llm_api_key="sk-test",
        mail_backend="ses",
        public_web_origin="https://app.example.com",
        mail_from_address="no-reply@example.com",
        mail_ses_region="us-east-1",
        # Retention durations have no default and production requires every one
        # (P6-PRIV-1): inert test values, not a policy.
        **{c.setting_name: 30 for c in RetentionClass},
    )
    base.update(overrides)
    return base


class TestRateLimitSettings:
    def test_defaults_are_per_process_and_degrade_locally(self):
        s = Settings(_env_file=None)
        assert (s.rate_limit_backend, s.rate_limit_redis_failure_mode) == ("memory", "local")

    def test_production_refuses_the_per_process_limiter(self):
        with pytest.raises(ValidationError) as exc:
            Settings(**_prod(rate_limit_backend="memory"))
        assert "environment=production requires a shared rate-limit backend" in str(exc.value)
        with pgvector_build():
            assert Settings(**_prod()).rate_limit_backend == "redis"

    def test_redis_limiter_requires_redis_url(self):
        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None, rate_limit_backend="redis")
        assert "rate_limit_backend=redis requires redis_url" in str(exc.value)

    def test_redis_limiter_enforces_redis_tuning_bounds(self):
        with pytest.raises(ValidationError) as exc:
            Settings(
                _env_file=None, rate_limit_backend="redis",
                redis_url="redis://localhost:6379/0", redis_operation_timeout_seconds=0,
            )
        assert "redis_operation_timeout_seconds must be greater than 0" in str(exc.value)

    def test_failure_mode_is_a_closed_set(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, rate_limit_redis_failure_mode="allow")

    def test_migration_actor_needs_no_limiter(self):
        # The one-shot migration actor serves no HTTP: neither rule applies to it.
        s = Settings(
            _env_file=None, environment="staging", migration_mode=True,
            database_url="postgresql://u:p@localhost/db", rate_limit_backend="redis",
        )
        assert s.redis_url is None

    def test_options_build_a_redis_client_only_for_redis(self):
        import redis

        assert rate_limit_middleware_options(Settings(_env_file=None)) == {}
        options = rate_limit_middleware_options(
            Settings(
                _env_file=None, rate_limit_backend="redis",
                redis_url="redis://cache.invalid:6379/0", redis_key_prefix="sn",
                rate_limit_redis_failure_mode="deny",
            )
        )
        try:
            assert options["key_prefix"] == "sn"
            assert options["failure_mode"] == "deny"
            pool = options["redis_client"].connection_pool
            # Bounded, and callers wait briefly for a connection instead of failing.
            assert isinstance(pool, redis.BlockingConnectionPool)
            assert (pool.max_connections, pool.timeout) == (10, 2.0)
        finally:
            options["redis_client"].connection_pool.disconnect()

    def test_create_app_mounts_the_configured_limiter(self, monkeypatch):
        import app.main as main_mod

        settings = Settings(
            _env_file=None, rate_limit_backend="redis",
            redis_url="redis://cache.invalid:6379/0", rate_limit_redis_failure_mode="deny",
        )
        monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
        built = main_mod.create_app()
        node = built.build_middleware_stack()
        while not isinstance(node, RateLimitMiddleware):
            node = node.app
        try:
            assert node.is_shared is True
            assert node.failure_mode == "deny"
            assert built.state.rate_limit_redis_client is not None
        finally:
            built.state.rate_limit_redis_client.connection_pool.disconnect()


class TestRateLimitReadiness:
    def test_per_process_limiter_is_healthy_only_outside_staging_and_production(self):
        status, _summary, _detail, _retry = _check_rate_limit(Settings(_env_file=None))
        assert status is ProbeStatus.HEALTHY
        staging = Settings.model_construct(environment="staging", rate_limit_backend="memory")
        status, summary, _detail, _retry = _check_rate_limit(staging)
        assert status is ProbeStatus.DEGRADED
        assert "not shared" in summary

    def test_redis_limiter_is_probed(self, monkeypatch):
        import redis

        class _Pong:
            def ping(self):
                return True

        monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Pong())
        s = Settings(_env_file=None, rate_limit_backend="redis", redis_url="redis://x.invalid/0")
        status, _summary, _detail, _retry = _check_rate_limit(s)
        assert status is ProbeStatus.HEALTHY

    def test_probe_blocks_readiness_only_in_deny_mode(self):
        def required(**kw: object) -> bool:
            s = Settings(_env_file=None, redis_url="redis://x.invalid/0", **kw)
            return next(p for p in _build_probes(s) if p.name == "rate_limit").required

        assert required() is False
        assert required(rate_limit_backend="redis") is False
        assert required(rate_limit_backend="redis", rate_limit_redis_failure_mode="deny") is True

    def test_runtime_report_lists_the_limiter(self):
        local = {c.name: c for c in build_runtime_report(Settings(_env_file=None)).capabilities}
        assert local["rate_limit"].backend == "memory" and local["rate_limit"].is_local
        shared = build_runtime_report(
            Settings(_env_file=None, rate_limit_backend="redis", redis_url="redis://x.invalid/0")
        )
        cap = {c.name: c for c in shared.capabilities}["rate_limit"]
        assert (cap.backend, cap.configured, cap.requires_external) == ("redis", True, True)


# --------------------------------------------------------------------------- #
# P6-PLAT-2 — no dead queue path; nothing selected that no one consumes
# --------------------------------------------------------------------------- #
class TestQueueRealignment:
    def test_the_dead_queue_adapter_is_gone(self):
        # RedisQueue xadd-ed to a stream nothing read; InProcessQueue had no caller.
        assert importlib.util.find_spec("app.infra.queue") is None

    def test_scout_runs_go_through_the_durable_job_store(self):
        from app.jobs.registry import builtin_job_types

        assert "scout_request.execute" in builtin_job_types()

    def test_no_setting_selects_the_unsubscribed_wake_up_publisher(self, monkeypatch):
        # The worker polls (it never calls wait_for_job), so a Redis publish would
        # reach no subscriber: the factory takes no settings, and even a process whose
        # settings say queue_backend=redis with a redis_url gets the null notifier.
        import inspect

        from app.jobs import coordination

        redis_settings = Settings(
            _env_file=None, queue_backend="redis", redis_url="redis://x.invalid/0"
        )
        monkeypatch.setattr("app.core.config.get_settings", lambda: redis_settings)
        assert inspect.signature(coordination.build_job_notifier).parameters == {}
        assert isinstance(coordination.build_job_notifier(), coordination.NullJobNotifier)

    def test_enqueue_records_no_redis_signal_without_a_transport(self, monkeypatch):
        from app.core.metrics import REDIS_NOTIFY_TOTAL
        from app.jobs import service
        from app.jobs.coordination import NullJobNotifier

        class _Job:
            status = "pending"

        backend = InMemoryMetrics()
        configure_metrics(backend)
        monkeypatch.setattr(service, "_notifier", NullJobNotifier())
        try:
            service._notify_available(_Job())
        finally:
            configure_metrics(None)
        for outcome in ("success", "failure"):
            assert backend.counter_value(
                REDIS_NOTIFY_TOTAL, dependency="redis", outcome=outcome
            ) == 0

    @pytest.mark.parametrize("queue_backend", ["inprocess", "redis"])
    def test_queue_readiness_is_neutral_and_truthful(self, queue_backend):
        s = Settings(_env_file=None, queue_backend=queue_backend, redis_url="redis://x.invalid/0")
        status, summary, detail, _retry = _check_queue(s)
        assert status is ProbeStatus.HEALTHY  # nothing is selected, so nothing degrades
        assert "job store" in summary and "poll" in summary
        if queue_backend == "redis":
            assert "selects nothing" in (detail or "")
        cap = {c.name: c for c in build_runtime_report(s).capabilities}["queue"]
        assert (cap.configured, cap.requires_external) == (True, False)


# --------------------------------------------------------------------------- #
# P6-PLAT-1 — the vector backend is honoured, never silently substituted
# --------------------------------------------------------------------------- #
class TestVectorBackend:
    def test_bruteforce_builds_the_in_process_index(self):
        assert isinstance(build_index(Settings(_env_file=None)), BruteForceIndex)

    def test_pgvector_is_never_silently_brute_force(self):
        pg = Settings.model_construct(
            vector_backend="pgvector", database_url="postgresql://u:p@db.invalid/d"
        )
        with pytest.raises(VectorBackendUnavailableError):
            build_index(pg)

    @pytest.mark.parametrize("environment", ["development", "test", "staging", "production"])
    def test_settings_refuse_pgvector_in_every_environment(self, environment):
        with pytest.raises(ValidationError) as exc:
            Settings(_env_file=None, environment=environment, vector_backend="pgvector")
        assert "vector_backend=pgvector is not implemented in this build" in str(exc.value)

    def test_the_build_fact_is_the_only_thing_refusing_production_pgvector(self):
        with pgvector_build():
            assert Settings(**_prod()).vector_backend == "pgvector"

    def test_readiness_reports_the_backend_that_runs(self):
        status, summary, _detail, _retry = _check_vector(Settings(_env_file=None))
        assert status is ProbeStatus.HEALTHY
        assert "brute-force" in summary
        pg = Settings.model_construct(
            vector_backend="pgvector", database_url="postgresql://u:p@db.invalid/d"
        )
        status, summary, _detail, _retry = _check_vector(pg)
        assert status is ProbeStatus.NOT_CONFIGURED
        assert "not implemented" in summary

    def test_runtime_report_flags_unimplemented_pgvector(self):
        pg = Settings.model_construct(
            vector_backend="pgvector", database_url="postgresql://u:p@db.invalid/d"
        )
        cap = {c.name: c for c in build_runtime_report(pg).capabilities}["vector"]
        assert cap.configured is False
        assert cap.detail == "pgvector is not implemented in this build"


@pytest.fixture()
def seeded_with_index_spy(tmp_path, monkeypatch):
    """Run the four-market demo seed with a spy around the pipeline's dedupe index.

    The spy checks every dedupe decision against the pre-P6-PLAT-1 rule ("duplicate
    when any kept signal of the run has cosine >= threshold"), so the routing through
    ``build_index()`` is proven behaviour-preserving on real pipeline data.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import seed as seed_mod
    from app.db.models import Base
    from app.infra.vector import cosine
    from app.jobs import pipeline

    built: list[_SpyIndex] = []

    class _SpyIndex(BruteForceIndex):
        def __init__(self) -> None:
            super().__init__()
            self.decisions: list[tuple[bool, bool]] = []

        def search(self, vector, top_k=5):
            result = super().search(vector, top_k)
            new_rule = bool(result) and result[0][1] >= pipeline.DEDUPE_THRESHOLD
            old_rule = any(
                cosine(vector, kept) >= pipeline.DEDUPE_THRESHOLD
                for kept in self._items.values()
            )
            self.decisions.append((new_rule, old_rule))
            return result

    real_build_index = pipeline.build_index

    def _spy_build_index(*args, **kwargs):
        assert isinstance(real_build_index(*args, **kwargs), BruteForceIndex)
        built.append(_SpyIndex())
        return built[-1]

    monkeypatch.setattr(pipeline, "build_index", _spy_build_index)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'seeded.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        seed_mod, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False, future=True)
    )
    seed_mod.seed(reset=True)
    try:
        yield built
    finally:
        engine.dispose()


def test_pipeline_dedupe_goes_through_the_configured_index(seeded_with_index_spy):
    built = seeded_with_index_spy
    assert built, "the pipeline never built a vector index"
    decisions = [d for index in built for d in index.decisions]
    assert decisions, "the pipeline never consulted the index"
    assert all(new == old for new, old in decisions)
    assert any(new for new, _old in decisions), "the seed should exercise a duplicate"
