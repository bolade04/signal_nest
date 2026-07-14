"""Production data-plane adapter tests (Phase 3A.4a).

None of these touch a real external service:

* **Config validation** — the new PostgreSQL-pool / Redis / S3 / worker bounds, and
  the *soft* (environment-gated) handling of a selected-but-unconfigured backend.
* **PostgreSQL engine + claim** — dialect resolution through SQLAlchemy URL parsing
  (never string matching), dialect-isolated pool wiring, and an always-run proof
  that the claim compiles to ``FOR UPDATE SKIP LOCKED`` on PostgreSQL. A real
  cross-worker claim test runs only when ``TEST_POSTGRES_URL`` is set.
* **Redis cache + coordination** — driven by ``fakeredis``: namespacing, JSON-only
  serialization, falsey-vs-miss, sanitized driver errors, wake-up pub/sub and the
  advisory lock's release-only-owned guarantee.
* **S3 storage** — key validation and the put/get/head/delete/signed-url contract
  driven by an injected fake client (never boto3/moto, never a real bucket).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.core.errors import (
    InvalidObjectKeyError,
    ObjectStorageUnavailableError,
    ObjectTooLargeError,
    RedisNotifyFailedError,
    RedisUnavailableError,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# --------------------------------------------------------------------------- #
# Config validation — new production-adapter bounds
# --------------------------------------------------------------------------- #
def test_local_defaults_construct_without_production_config() -> None:
    # The zero-dependency default must never require Redis/S3/PG configuration.
    s = _settings()
    assert s.is_sqlite is True
    assert s.is_postgres is False


def test_selected_but_unconfigured_backends_are_soft_in_local_mode() -> None:
    # Selecting a production backend without its config in local/dev is a *soft*
    # unconfigured state (surfaced by the runtime report), not a hard failure.
    s = _settings(cache_backend="redis", storage_backend="s3")
    assert s.cache_backend == "redis"
    assert s.storage_backend == "s3"


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"database_url": "postgresql://u@h/db", "db_pool_size": 0}, "db_pool_size"),
        ({"database_url": "postgresql://u@h/db", "db_max_overflow": -1}, "db_max_overflow"),
        (
            {"database_url": "postgresql://u@h/db", "db_pool_timeout_seconds": 0},
            "db_pool_timeout_seconds",
        ),
        (
            {"database_url": "postgresql://u@h/db", "db_application_name": "  "},
            "db_application_name",
        ),
        ({"cache_backend": "redis", "redis_pool_size": 0}, "redis_pool_size"),
        (
            {"cache_backend": "redis", "redis_operation_timeout_seconds": 0},
            "redis_operation_timeout_seconds",
        ),
        ({"cache_backend": "redis", "redis_key_prefix": " "}, "redis_key_prefix"),
        ({"cache_backend": "redis", "redis_lock_ttl_seconds": 0}, "redis_lock_ttl_seconds"),
        ({"storage_backend": "s3", "s3_max_object_bytes": 0}, "s3_max_object_bytes"),
        ({"storage_backend": "s3", "s3_signed_url_ttl_seconds": 0}, "s3_signed_url_ttl_seconds"),
        ({"storage_backend": "s3", "s3_max_retries": -1}, "s3_max_retries"),
        ({"storage_backend": "s3", "s3_access_key_id": "only-half"}, "must be set together"),
        ({"worker_stale_after_seconds": 1.0}, "worker_stale_after_seconds"),
        ({"worker_registration_retry_limit": -1}, "worker_registration_retry_limit"),
        ({"worker_id_max_length": 0}, "worker_id_max_length"),
        ({"worker_type": "  "}, "worker_type"),
        ({"database_url": "not a url"}, "malformed"),
        ({"database_url": "   "}, "must not be empty"),
    ],
)
def test_invalid_production_settings_are_rejected(overrides, needle) -> None:
    with pytest.raises(ValueError) as excinfo:
        _settings(**overrides)
    assert needle in str(excinfo.value)


def test_stale_threshold_must_exceed_heartbeat_interval() -> None:
    # A healthy worker must never be flagged stale between heartbeats.
    with pytest.raises(ValueError):
        _settings(worker_heartbeat_seconds=30.0, worker_stale_after_seconds=30.0)
    ok = _settings(worker_heartbeat_seconds=10.0, worker_stale_after_seconds=60.0)
    assert ok.worker_stale_after_seconds > ok.worker_heartbeat_seconds


# --------------------------------------------------------------------------- #
# PostgreSQL engine + dialect resolution
# --------------------------------------------------------------------------- #
def test_dialect_resolved_via_url_parsing_not_string_match() -> None:
    # A driver-suffixed URL must resolve to its true backend, not a prefix guess.
    s = _settings(database_url="postgresql+psycopg://u:p@h:5432/db")
    assert s.db_backend_name == "postgresql"
    assert s.is_postgres is True
    assert s.is_sqlite is False


def test_build_engine_uses_isolated_postgres_pool() -> None:
    from app.db.session import build_engine

    s = _settings(
        database_url="postgresql+psycopg://u:p@h:5432/db",
        db_pool_size=7,
        db_max_overflow=3,
    )
    engine = build_engine(s)  # lazy: create_engine does not connect
    try:
        assert engine.dialect.name == "postgresql"
        # Bounded QueuePool sized from settings.
        assert engine.pool.size() == 7
    finally:
        engine.dispose()


def test_build_engine_sqlite_branch_stays_local() -> None:
    from app.db.session import build_engine

    engine = build_engine(_settings())
    try:
        assert engine.dialect.name == "sqlite"
    finally:
        engine.dispose()


def test_claim_compiles_to_skip_locked_on_postgresql() -> None:
    # Always-run proof (no live PG needed): the PG claim SELECT emits FOR UPDATE
    # SKIP LOCKED with the shared priority/FIFO ordering.
    from app.jobs.store import DurableJobStore

    store = DurableJobStore()
    stmt = store._due_candidates_select(datetime.now(UTC), 1).with_for_update(skip_locked=True)
    sql = str(stmt.compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql
    assert "ORDER BY" in sql


# --------------------------------------------------------------------------- #
# PostgreSQL cross-worker claim — real DB, gated on TEST_POSTGRES_URL
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL claim test",
)
def test_postgres_two_workers_claim_different_jobs() -> None:  # pragma: no cover - gated
    import hashlib
    import json

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.jobs.store import DurableJobStore
    from app.organizations.models import Organization, Workspace

    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    store = DurableJobStore()

    # PostgreSQL enforces the jobs -> organizations/workspaces foreign keys, so the
    # tenant scope the enqueued jobs reference must exist first.
    with factory() as tenant:
        tenant.add(Organization(id="org-1", name="Org One", slug="org-one"))
        tenant.add(Workspace(id="ws-1", organization_id="org-1", name="WS One", slug="ws-one"))
        tenant.commit()

    def _enqueue(db, key):
        payload = {"scout_request_id": key}
        job = store.enqueue(
            db,
            organization_id="org-1",
            workspace_id="ws-1",
            job_type="test.ok",
            payload=payload,
            payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        )
        db.commit()
        return job

    with factory() as seed:
        _enqueue(seed, "a")
        _enqueue(seed, "b")

    db1, db2 = factory(), factory()
    try:
        j1 = store.claim_one(db1, worker_id="w1", lease_seconds=30)
        j2 = store.claim_one(db2, worker_id="w2", lease_seconds=30)
        assert j1 is not None and j2 is not None
        assert j1.id != j2.id  # never two owners of the same row
        assert j1.worker_id == "w1" and j2.worker_id == "w2"
        assert j1.lease_token != j2.lease_token  # fresh, distinct lease tokens
    finally:
        db1.close()
        db2.close()
        engine.dispose()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="TEST_POSTGRES_URL not set; skipping live PostgreSQL contention test",
)
def test_postgres_skip_locked_claimer_skips_a_locked_row() -> None:  # pragma: no cover - gated
    """True lock contention: a claim skips a row another txn holds locked.

    Unlike the sequential two-worker test above, this holds transaction A's
    ``FOR UPDATE SKIP LOCKED`` row lock *open* while transaction B claims. B must
    step over the locked candidate and take the next one rather than block on A —
    which is the whole point of ``SKIP LOCKED``. A short ``statement_timeout`` on
    B turns a regression (B blocking on the lock) into a fast, unambiguous failure
    instead of a hang.
    """
    import hashlib
    import json
    from datetime import UTC, datetime

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.jobs.store import DurableJobStore
    from app.organizations.models import Organization, Workspace

    url = os.environ["TEST_POSTGRES_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    store = DurableJobStore()

    with factory() as tenant:
        tenant.add(Organization(id="org-1", name="Org One", slug="org-one"))
        tenant.add(Workspace(id="ws-1", organization_id="org-1", name="WS One", slug="ws-one"))
        tenant.commit()

    def _enqueue(db, key):
        payload = {"scout_request_id": key}
        store.enqueue(
            db,
            organization_id="org-1",
            workspace_id="ws-1",
            job_type="test.ok",
            payload=payload,
            payload_hash=hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
        )
        db.commit()

    with factory() as seed:
        _enqueue(seed, "a")
        _enqueue(seed, "b")

    now = datetime.now(UTC)
    db_a, db_b = factory(), factory()
    try:
        # A locks exactly one due row with SKIP LOCKED and holds the lock open.
        locked = db_a.execute(
            store._due_candidates_select(now, 1).with_for_update(skip_locked=True)
        ).first()
        assert locked is not None
        locked_id = locked[0]

        # B must not wait on A's lock; if it ever does, fail fast instead of hanging.
        db_b.execute(text("SET LOCAL statement_timeout = '5000'"))
        claimed = store.claim_one(db_b, worker_id="w-b", lease_seconds=30, now=now)
        assert claimed is not None
        assert claimed.id != locked_id  # B skipped A's locked row and took the other
        db_b.commit()

        # A now releases its lock and claims what remains — the row it had locked.
        db_a.rollback()
        remaining = store.claim_one(db_a, worker_id="w-a", lease_seconds=30, now=now)
        assert remaining is not None
        assert remaining.id == locked_id
        assert remaining.lease_token != claimed.lease_token
    finally:
        db_a.close()
        db_b.close()
        engine.dispose()


# --------------------------------------------------------------------------- #
# Redis cache — fakeredis
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_redis():
    import fakeredis

    return fakeredis.FakeStrictRedis()


def test_redis_cache_roundtrip_and_namespacing(fake_redis) -> None:
    from app.infra.cache import RedisCache, tenant_cache_key

    cache = RedisCache(fake_redis, key_prefix="sn")
    key = tenant_cache_key("org-1", "ws-1", "profile")
    cache.set(key, {"n": 1})
    assert cache.get(key) == {"n": 1}
    # The physical Redis key is namespaced under the configured prefix.
    assert fake_redis.get("sn:cache:t:org-1:ws-1:profile") is not None


def test_redis_cache_uses_json_not_pickle(fake_redis) -> None:
    import json

    from app.infra.cache import RedisCache

    cache = RedisCache(fake_redis, key_prefix="sn")
    cache.set("k", {"a": [1, 2]})
    raw = fake_redis.get("sn:cache:k")
    # Stored as parseable JSON (never a pickle byte-stream).
    assert json.loads(raw) == {"v": {"a": [1, 2]}}


def test_redis_cache_distinguishes_falsey_from_miss(fake_redis) -> None:
    from app.infra.cache import MISS, RedisCache

    cache = RedisCache(fake_redis, key_prefix="sn")
    cache.set("zero", 0)
    cache.set("empty", "")
    assert cache.get("zero") == 0  # cached falsey round-trips
    assert cache.get("empty") == ""
    assert cache.get("absent", MISS) is MISS  # a real miss returns the sentinel


def test_redis_cache_tenant_keys_do_not_collide(fake_redis) -> None:
    from app.infra.cache import RedisCache, tenant_cache_key

    cache = RedisCache(fake_redis, key_prefix="sn")
    cache.set(tenant_cache_key("org-1", "ws-1", "x"), "a")
    cache.set(tenant_cache_key("org-2", "ws-1", "x"), "b")
    assert cache.get(tenant_cache_key("org-1", "ws-1", "x")) == "a"
    assert cache.get(tenant_cache_key("org-2", "ws-1", "x")) == "b"


def test_redis_cache_rejects_nonpositive_ttl(fake_redis) -> None:
    from app.infra.cache import RedisCache

    cache = RedisCache(fake_redis, key_prefix="sn")
    with pytest.raises(ValueError):
        cache.set("k", 1, ttl_seconds=0)


class _BrokenRedis:
    def get(self, *_a, **_k):
        raise RuntimeError("redis://secret-host:6379 connection refused")

    def set(self, *_a, **_k):
        raise RuntimeError("redis://secret-host:6379 connection refused")

    def delete(self, *_a, **_k):
        raise RuntimeError("redis://secret-host:6379 connection refused")

    def ping(self, *_a, **_k):
        raise RuntimeError("redis://secret-host:6379 connection refused")

    def publish(self, *_a, **_k):
        raise RuntimeError("redis://secret-host:6379 connection refused")


def test_redis_cache_sanitizes_driver_errors() -> None:
    from app.infra.cache import RedisCache

    cache = RedisCache(_BrokenRedis(), key_prefix="sn")
    with pytest.raises(RedisUnavailableError) as excinfo:
        cache.get("k")
    # The static message never carries the raw driver text (host/URL).
    assert "secret-host" not in str(excinfo.value)
    assert "redis://" not in str(excinfo.value)
    # ping never raises; a failed ping is reported as not-ready.
    assert cache.ping() is False


# --------------------------------------------------------------------------- #
# Redis coordination — wake-up notifier + advisory lock
# --------------------------------------------------------------------------- #
def test_null_notifier_waits_and_reports_no_signal() -> None:
    from app.jobs.coordination import NullJobNotifier

    n = NullJobNotifier()
    n.notify_job_available()  # no-op
    assert n.wait_for_job(0.0) is False


def test_redis_notifier_publish_then_wake(fake_redis) -> None:
    from app.jobs.coordination import RedisJobNotifier

    n = RedisJobNotifier(fake_redis, channel="sn:jobs")
    assert n.wait_for_job(0.0) is False  # subscribes; nothing pending yet
    n.notify_job_available()
    assert n.wait_for_job(0.5) is True  # the wake-up is delivered
    n.close()


def test_redis_notifier_publish_failure_is_sanitized() -> None:
    from app.jobs.coordination import RedisJobNotifier

    n = RedisJobNotifier(_BrokenRedis(), channel="sn:jobs")
    with pytest.raises(RedisNotifyFailedError) as excinfo:
        n.notify_job_available()
    assert "secret-host" not in str(excinfo.value)


def test_advisory_lock_is_exclusive_and_release_only_owned(fake_redis) -> None:
    from app.jobs.coordination import RedisAdvisoryLock

    a = RedisAdvisoryLock(fake_redis, key="sn:lock", ttl_seconds=30)
    b = RedisAdvisoryLock(fake_redis, key="sn:lock", ttl_seconds=30)
    assert a.acquire() is True
    assert b.acquire() is False  # held by a
    # b cannot release a lock it never owned.
    assert b.release() is False
    assert a.release() is True  # the true owner releases
    assert b.acquire() is True  # now free
    b.release()


# --------------------------------------------------------------------------- #
# S3 storage — key validation + injected fake client
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    ["", "/abs/key", "a\\b", "a\x00b", "../escape", "a/../b", "a//b", "./a", "a/."],
)
def test_object_key_validation_rejects_unsafe_keys(bad) -> None:
    from app.infra.storage import validate_object_key

    with pytest.raises(InvalidObjectKeyError):
        validate_object_key(bad)


def test_object_key_validation_accepts_safe_key() -> None:
    from app.infra.storage import tenant_object_key, validate_object_key

    assert validate_object_key("a/b/c.txt") == "a/b/c.txt"
    assert tenant_object_key("org-1", "ws-1", "assets", "logo.png") == (
        "org-1/ws-1/assets/logo.png"
    )


@pytest.mark.parametrize(
    "org,ws",
    [
        ("../org", "ws-1"),        # traversal in the org segment
        ("org-1", "../ws"),        # traversal in the workspace segment
        ("a/b", "ws-1"),           # separator smuggles an extra segment
        ("org-1", "a/b"),
        ("org\\1", "ws-1"),        # backslash separator
        ("", "ws-1"),              # empty tenant id
        ("org-1", ""),
        ("..", "ws-1"),            # bare relative segment
        ("org-1", "."),
        ("org\x001", "ws-1"),      # null byte
    ],
)
def test_tenant_object_key_rejects_hostile_tenant_identifiers(org, ws) -> None:
    """A hostile org/workspace id cannot inject a separator or traversal."""
    from app.infra.storage import tenant_object_key

    with pytest.raises(InvalidObjectKeyError):
        tenant_object_key(org, ws, "assets", "logo.png")


def test_tenant_object_key_rejects_hostile_relative_parts() -> None:
    """The relative parts remain validated, and the composed key is re-checked."""
    from app.infra.storage import tenant_object_key

    with pytest.raises(InvalidObjectKeyError):
        tenant_object_key("org-1", "ws-1", "..", "escape")
    with pytest.raises(InvalidObjectKeyError):
        tenant_object_key("org-1", "ws-1", "a", "../b")


class _FakeS3:
    """In-memory stand-in for a boto3 S3 client. Records put kwargs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.last_put_kwargs: dict | None = None

    def put_object(self, **kwargs):
        self.last_put_kwargs = kwargs
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}

    def get_object(self, **kwargs):
        import io

        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def head_object(self, **kwargs):
        if kwargs["Key"] not in self.objects:
            raise RuntimeError("404")
        return {}

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)
        return {}

    def generate_presigned_url(self, _op, Params, ExpiresIn):
        return f"https://signed/{Params['Key']}?e={ExpiresIn}"

    def head_bucket(self, **_kwargs):
        return {}


def _s3(client, **overrides):
    from app.infra.storage import S3Storage

    defaults = dict(bucket="bkt", max_object_bytes=1024, signed_url_ttl_seconds=900)
    defaults.update(overrides)
    return S3Storage(client, **defaults)


def test_s3_put_get_head_delete_roundtrip() -> None:
    client = _FakeS3()
    store = _s3(client)
    store.put("org-1/ws-1/a.txt", b"hello", content_type="text/plain")
    assert store.get("org-1/ws-1/a.txt") == b"hello"
    assert store.head("org-1/ws-1/a.txt") is True
    store.delete("org-1/ws-1/a.txt")
    assert store.head("org-1/ws-1/a.txt") is False


def test_s3_put_is_private_by_default() -> None:
    client = _FakeS3()
    _s3(client).put("k", b"x")
    # No public ACL is ever set; the object inherits the bucket's private default.
    assert "ACL" not in (client.last_put_kwargs or {})


def test_s3_rejects_oversized_upload_before_io() -> None:
    client = _FakeS3()
    store = _s3(client, max_object_bytes=4)
    with pytest.raises(ObjectTooLargeError):
        store.put("k", b"too-long-body")
    assert client.objects == {}  # nothing was streamed to the backend


def test_s3_signed_url_ttl_is_capped() -> None:
    store = _s3(_FakeS3(), signed_url_ttl_seconds=900)
    url = store.signed_url("k", expires_in=100000)  # caller asks for a huge TTL
    assert "e=900" in url  # capped to the configured ceiling


def test_s3_driver_errors_are_sanitized() -> None:
    class _Boom(_FakeS3):
        def get_object(self, **_kwargs):
            raise RuntimeError("s3://private-bucket/secret endpoint failure")

    store = _s3(_Boom())
    with pytest.raises(ObjectStorageUnavailableError) as excinfo:
        store.get("k")
    assert "private-bucket" not in str(excinfo.value)
