"""Distributed-tracing tests (Phase 3A.4b Batch 3).

Everything here is collector-free: the seam is a pure-Python, OTel-compatible
tracer with a :class:`~app.core.tracing.NoOpTracer` default and an
:class:`~app.core.tracing.InMemoryTracer` for assertions, so no hosted vendor or
OpenTelemetry SDK is required to exercise it.

Layers covered:

* the span-name + attribute allow-lists (low-cardinality policy),
* strict W3C ``traceparent`` parse/format propagation,
* deterministic, parent-based sampling (incl. reduced-rate low-value spans),
* safe exception recording (class only, never a message),
* trace/log correlation (only a *recording* span drives ``trace_id``),
* runtime export/flush failure isolation (swallowed + counted, caller unaffected),
* ``configure_tracing_from_settings`` (disabled default; memory; OTLP fail-closed),
* HTTP request spans end-to-end through the middleware,
* durable-job trace propagation enqueue -> claim -> execute -> complete,
* Redis + S3 dependency spans.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from app.core import tracing
from app.core.config import Settings
from app.core.log_context import trace_id_ctx
from app.core.middleware import CorrelationMiddleware
from app.core.tracing import (
    DATABASE_TRANSACTION,
    HTTP_REQUEST,
    JOB_COMPLETE,
    JOB_EXECUTE,
    READINESS_CHECK,
    REDIS_CACHE,
    STORAGE_SIGN_URL,
    STORAGE_UPLOAD,
    InMemoryTracer,
    NoOpTracer,
    SpanContext,
    TraceError,
    _decide_sampled,
    _SafeTracer,
    configure_tracer,
    configure_tracing_from_settings,
    extract_context,
    format_traceparent,
    get_tracer,
    inject_context,
    last_export_failure_category,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    start_span,
    trace_export_failure_count,
    tracing_exporter_status,
    validate_span,
)
from app.db.base import Base
from app.jobs.context import ExecutionContext
from app.jobs.registry import HandlerContext, register_handler
from app.jobs.service import enqueue_job
from app.jobs.worker import JobRunner

# Importing the app registers every ORM model on the shared Base.
from app.main import app as _real_app  # noqa: F401

ORG = "org-trace-0001"
WS = "ws-trace-0001"


@pytest.fixture()
def restore_tracer():
    """Save/restore the process-global tracer so a test never leaks its tracer."""
    previous = get_tracer()
    try:
        yield
    finally:
        configure_tracer(previous)


@pytest.fixture()
def memory_tracer(restore_tracer) -> InMemoryTracer:
    tracer = InMemoryTracer(sample_ratio=1.0)
    configure_tracer(tracer)
    return tracer


# --------------------------------------------------------------------------- #
# Span-name + attribute allow-lists
# --------------------------------------------------------------------------- #
def test_valid_span_name_and_attributes_pass() -> None:
    validate_span(HTTP_REQUEST, {"component": "api", "http.request.method": "GET"})


def test_unknown_span_name_raises() -> None:
    with pytest.raises(TraceError):
        validate_span("job.teleport", {})


def test_forbidden_attribute_raises() -> None:
    # A raw id / URL / message is never an allowed span attribute.
    with pytest.raises(TraceError):
        validate_span(HTTP_REQUEST, {"job_id": "abc123"})


def test_set_attribute_rejects_forbidden_key(memory_tracer) -> None:
    with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}) as span:
        with pytest.raises(TraceError):
            span.set_attribute("organization_id", "org-1")


# --------------------------------------------------------------------------- #
# W3C traceparent propagation
# --------------------------------------------------------------------------- #
def test_traceparent_round_trips() -> None:
    ctx = SpanContext(trace_id=new_trace_id(), span_id=new_span_id(), sampled=True)
    parsed = parse_traceparent(format_traceparent(ctx))
    assert parsed is not None
    assert parsed.trace_id == ctx.trace_id
    assert parsed.span_id == ctx.span_id
    assert parsed.sampled is True
    assert parsed.remote is True


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "not-a-traceparent",
        "01-" + "a" * 32 + "-" + "b" * 16 + "-01",  # wrong version
        "00-" + "a" * 31 + "-" + "b" * 16 + "-01",  # short trace id
        "00-" + "0" * 32 + "-" + "b" * 16 + "-01",  # all-zero trace id
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",  # all-zero span id
        "00-" + "a" * 32 + "-" + "b" * 16 + "-01\nx",  # newline injection
    ],
)
def test_parse_traceparent_rejects_malformed(header) -> None:
    assert parse_traceparent(header) is None


def test_unsampled_flag_round_trips() -> None:
    ctx = SpanContext(trace_id=new_trace_id(), span_id=new_span_id(), sampled=False)
    parsed = parse_traceparent(format_traceparent(ctx))
    assert parsed is not None and parsed.sampled is False


# --------------------------------------------------------------------------- #
# Deterministic, parent-based sampling
# --------------------------------------------------------------------------- #
def test_sampling_bounds() -> None:
    tid = new_trace_id()
    assert _decide_sampled(HTTP_REQUEST, None, ratio=1.0, trace_id=tid) is True
    assert _decide_sampled(HTTP_REQUEST, None, ratio=0.0, trace_id=tid) is False


def test_sampling_is_deterministic_per_trace_id() -> None:
    tid = new_trace_id()
    first = _decide_sampled(HTTP_REQUEST, None, ratio=0.5, trace_id=tid)
    second = _decide_sampled(HTTP_REQUEST, None, ratio=0.5, trace_id=tid)
    assert first == second


def test_parent_decision_is_honored() -> None:
    sampled_parent = SpanContext(trace_id="a" * 32, span_id="b" * 16, sampled=True, remote=True)
    unsampled_parent = SpanContext(trace_id="c" * 32, span_id="d" * 16, sampled=False, remote=True)
    # Parent forces the child's decision regardless of ratio.
    assert _decide_sampled(JOB_EXECUTE, sampled_parent, ratio=0.0, trace_id="a" * 32) is True
    assert _decide_sampled(JOB_EXECUTE, unsampled_parent, ratio=1.0, trace_id="c" * 32) is False


def test_low_value_span_is_reduced_rate() -> None:
    # A trace id that samples at 0.05 but not at 0.005 (0.05/10) demonstrates the
    # reduced rate for low-value roots.
    tid_hit = None
    for _ in range(2000):
        tid = new_trace_id()
        if tracing._trace_id_is_sampled(tid, 0.05) and not tracing._trace_id_is_sampled(tid, 0.005):
            tid_hit = tid
            break
    assert tid_hit is not None
    assert _decide_sampled(HTTP_REQUEST, None, ratio=0.05, trace_id=tid_hit) is True
    assert _decide_sampled(READINESS_CHECK, None, ratio=0.05, trace_id=tid_hit) is False


def test_child_records_under_sampled_parent_even_at_zero_ratio(restore_tracer) -> None:
    configure_tracer(InMemoryTracer(sample_ratio=0.0))
    parent = SpanContext(trace_id="a" * 32, span_id="b" * 16, sampled=True, remote=True)
    with start_span(
        JOB_EXECUTE, kind="consumer", parent=parent, attributes={"component": "jobs"}
    ) as s:
        assert s.recording is True
        assert s.trace_id == "a" * 32


# --------------------------------------------------------------------------- #
# Safe exception recording
# --------------------------------------------------------------------------- #
def test_exception_records_class_not_message(memory_tracer) -> None:
    class DownstreamTimeout(Exception):
        pass

    with pytest.raises(DownstreamTimeout):
        with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}):
            raise DownstreamTimeout("password=hunter2 db=postgres://user:pw@host")

    finished = memory_tracer.finished(HTTP_REQUEST)[-1]
    assert finished.status == tracing.STATUS_ERROR
    assert finished.error_class == "DownstreamTimeout"
    # Only the class is recorded; the message (and any secret token in it) never is.
    blob = str(finished.to_export()).lower()
    for token in ("hunter2", "postgres", "password", "pw@host"):
        assert token not in blob


# --------------------------------------------------------------------------- #
# Trace/log correlation
# --------------------------------------------------------------------------- #
def test_recording_span_drives_trace_id_context(memory_tracer) -> None:
    assert trace_id_ctx.get() is None
    with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}) as span:
        assert trace_id_ctx.get() == span.trace_id
    # Reset on exit — never leaks to the next request/job.
    assert trace_id_ctx.get() is None


def test_noop_span_leaves_correlation_untouched(restore_tracer) -> None:
    # The default (disabled) tracer is a no-op: a non-recording span must not change
    # existing log-correlation behavior.
    configure_tracer(NoOpTracer())
    token = trace_id_ctx.set("preexisting-correlation")
    try:
        with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}) as span:
            assert span.recording is False
            assert trace_id_ctx.get() == "preexisting-correlation"
    finally:
        trace_id_ctx.reset(token)


# --------------------------------------------------------------------------- #
# Tracer implementations
# --------------------------------------------------------------------------- #
def test_noop_tracer_records_nothing(restore_tracer) -> None:
    configure_tracer(NoOpTracer())
    with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}) as span:
        assert span.recording is False


def test_in_memory_tracer_collects_and_resets(memory_tracer) -> None:
    with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}):
        pass
    assert len(memory_tracer.finished(HTTP_REQUEST)) == 1
    memory_tracer.reset()
    assert memory_tracer.finished() == []


# --------------------------------------------------------------------------- #
# Runtime export/flush failure isolation
# --------------------------------------------------------------------------- #
class _ExplodingTracer(_SafeTracer):
    """A tracer whose export + flush always raise — to prove failures are isolated."""

    def _export(self, span) -> None:
        raise RuntimeError("collector unreachable")

    def _flush(self, timeout_seconds: float) -> bool:
        raise RuntimeError("flush timed out")


def test_export_failure_is_swallowed_and_counted(restore_tracer) -> None:
    tracer = _ExplodingTracer(sample_ratio=1.0)
    configure_tracer(tracer)
    # The caller's work completes normally despite the export raising.
    with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}) as span:
        span.set_attribute("outcome", "success")
    assert trace_export_failure_count() == 1
    assert last_export_failure_category() == "export"


def test_flush_failure_is_swallowed_and_counted(restore_tracer) -> None:
    tracer = _ExplodingTracer(sample_ratio=1.0)
    configure_tracer(tracer)
    assert tracer.shutdown(0.1) is False
    assert last_export_failure_category() == "flush"


# --------------------------------------------------------------------------- #
# configure_tracing_from_settings
# --------------------------------------------------------------------------- #
def test_disabled_by_default_installs_noop(restore_tracer) -> None:
    tracer = configure_tracing_from_settings(Settings(_env_file=None))
    assert isinstance(tracer, NoOpTracer)
    assert tracing_exporter_status(tracing_enabled=False) == "disabled"


def test_enabled_memory_exporter_installs_in_memory(restore_tracer) -> None:
    settings = Settings(
        _env_file=None,
        tracing_enabled=True,
        tracing_exporter="memory",
        tracing_sample_ratio=0.25,
    )
    tracer = configure_tracing_from_settings(settings)
    assert isinstance(tracer, InMemoryTracer)
    assert tracer.sample_ratio == 0.25
    assert tracing_exporter_status(tracing_enabled=True) == "healthy"


def test_otlp_without_sdk_fails_closed_to_noop(restore_tracer, monkeypatch) -> None:
    # Simulate the OpenTelemetry SDK being absent: the import inside the OTLP builder
    # raises, and configuration degrades to a no-op rather than blocking startup.
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "opentelemetry":
            raise ImportError("no opentelemetry")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    settings = Settings(
        _env_file=None,
        tracing_enabled=True,
        tracing_exporter="otlp",
        otlp_endpoint="http://localhost:4318",
    )
    tracer = configure_tracing_from_settings(settings)
    assert isinstance(tracer, NoOpTracer)


# --------------------------------------------------------------------------- #
# HTTP request spans end-to-end
# --------------------------------------------------------------------------- #
@pytest.fixture()
def http_client(memory_tracer) -> TestClient:
    # A FastAPI app is used (not bare Starlette) because only FastAPI populates
    # ``scope["route"]`` with a path template, which the middleware reads for the
    # low-cardinality ``http.route`` attribute.
    from fastapi import FastAPI

    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/items/{item_id}")
    def _get_item(item_id: str) -> dict:
        return {"ok": True}

    with TestClient(app) as c:
        yield c


def test_http_request_emits_bounded_server_span(http_client, memory_tracer) -> None:
    resp = http_client.get("/items/12345")
    assert resp.status_code == 200
    span = memory_tracer.finished(HTTP_REQUEST)[-1]
    assert span.kind == "server"
    assert span.attributes["http.request.method"] == "GET"
    # Route template, never the raw id-bearing path.
    assert span.attributes["http.route"] == "/items/{item_id}"
    assert "12345" not in str(span.attributes)
    assert span.attributes["http.response.status_code"] == 200


def test_inbound_traceparent_becomes_parent(http_client, memory_tracer) -> None:
    trace_id = "a" * 32
    header = f"00-{trace_id}-{'b' * 16}-01"
    http_client.get("/items/7", headers={"traceparent": header})
    span = memory_tracer.finished(HTTP_REQUEST)[-1]
    assert span.trace_id == trace_id
    assert span.parent is not None and span.parent.remote is True


def test_malformed_traceparent_starts_fresh_root(http_client, memory_tracer) -> None:
    http_client.get("/items/7", headers={"traceparent": "garbage"})
    span = memory_tracer.finished(HTTP_REQUEST)[-1]
    assert span.parent is None


# --------------------------------------------------------------------------- #
# Durable-job trace propagation end-to-end
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'trace.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    yield factory
    engine.dispose()


@pytest.fixture()
def db(session_factory) -> Session:
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


def _context() -> ExecutionContext:
    return ExecutionContext.for_scout_request(organization_id=ORG, workspace_id=WS)


@register_handler("test.trace.noop")
def _noop_handler(ctx: HandlerContext) -> dict:
    return {"ok": True}


def test_enqueue_persists_trace_context_under_recording_span(memory_tracer, db) -> None:
    with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}) as span:
        enqueue_trace = span.trace_id
        job = enqueue_job(db, job_type="test.trace.noop", context=_context(), payload={"n": 1})
        db.commit()
    assert job.trace_context is not None
    parsed = extract_context(job.trace_context)
    assert parsed is not None and parsed.trace_id == enqueue_trace


def test_worker_links_execution_to_enqueue_lineage(memory_tracer, session_factory) -> None:
    setup = session_factory()
    with start_span(HTTP_REQUEST, kind="server", attributes={"component": "api"}) as span:
        enqueue_trace = span.trace_id
        enqueue_job(setup, job_type="test.trace.noop", context=_context(), payload={"n": 2})
        setup.commit()
    setup.close()

    runner = JobRunner(settings=Settings(_env_file=None), session_factory=session_factory)
    assert runner.poll_once(worker_id="w-trace") is True

    execute_spans = memory_tracer.finished(JOB_EXECUTE)
    assert execute_spans, "expected a job.execute span"
    assert execute_spans[-1].trace_id == enqueue_trace
    # The terminal transition is its own child span within the execution.
    assert memory_tracer.finished(JOB_COMPLETE)


def test_claim_emits_controlled_database_transaction_span(memory_tracer, session_factory) -> None:
    # The claim's DB span is asserted directly (not via poll_once) because under a
    # worker poll it is nested inside the reduced-sampled WORKER_POLL span; called
    # directly it is a normally-sampled root, so the assertion is deterministic.
    from app.jobs.store import job_store

    setup = session_factory()
    enqueue_job(setup, job_type="test.trace.noop", context=_context(), payload={"n": 9})
    setup.commit()
    setup.close()

    db = session_factory()
    try:
        claimed = job_store.claim_one(db, worker_id="w-direct", lease_seconds=30)
    finally:
        db.close()
    assert claimed is not None
    span = memory_tracer.finished(DATABASE_TRANSACTION)[-1]
    assert span.attributes["operation"] == "claim"
    assert span.attributes["dependency"] == "database"
    assert span.attributes["outcome"] == "claimed"


def test_job_without_persisted_context_starts_fresh_root(memory_tracer, session_factory) -> None:
    # Enqueue with tracing off so no trace_context is persisted.
    configure_tracer(NoOpTracer())
    setup = session_factory()
    enqueue_job(setup, job_type="test.trace.noop", context=_context(), payload={"n": 3})
    setup.commit()
    setup.close()

    # Now turn tracing on for execution: the job has no parent, so a fresh root span.
    tracer = InMemoryTracer(sample_ratio=1.0)
    configure_tracer(tracer)
    runner = JobRunner(settings=Settings(_env_file=None), session_factory=session_factory)
    assert runner.poll_once(worker_id="w-trace") is True
    execute_spans = tracer.finished(JOB_EXECUTE)
    assert execute_spans and execute_spans[-1].parent is None


# --------------------------------------------------------------------------- #
# Redis + S3 dependency spans
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self):
        self._d = {}

    def get(self, k):
        return self._d.get(k)

    def set(self, k, v, ex=None):
        self._d[k] = v

    def delete(self, k):
        self._d.pop(k, None)


def test_redis_cache_spans_are_bounded(memory_tracer) -> None:
    from app.infra.cache import RedisCache

    cache = RedisCache(_FakeRedis(), key_prefix="p")
    cache.set("k", {"v": 1})
    cache.get("k")
    cache.get("absent")
    cache.delete("k")

    spans = memory_tracer.finished(REDIS_CACHE)
    ops = [s.attributes.get("operation") for s in spans]
    assert ops == ["set", "get", "get", "delete"]
    outcomes = {s.attributes.get("outcome") for s in spans}
    assert outcomes == {"success", "hit", "miss"}
    for s in spans:
        assert s.attributes["dependency"] == "redis"
        # Never the key or value.
        assert "k" not in str(s.attributes)


class _FakeS3:
    def put_object(self, **kwargs):
        return None

    def generate_presigned_url(self, *args, **kwargs):
        return "https://signed.example/object"


def test_s3_storage_spans_are_bounded(memory_tracer) -> None:
    from app.infra.storage import S3Storage

    store = S3Storage(_FakeS3(), bucket="b", max_object_bytes=1000, signed_url_ttl_seconds=60)
    store.put("k/o.txt", b"data")
    store.signed_url("k/o.txt")

    assert memory_tracer.finished(STORAGE_UPLOAD)[-1].attributes["operation"] == "put"
    assert memory_tracer.finished(STORAGE_SIGN_URL)[-1].attributes["operation"] == "sign_url"
    for s in memory_tracer.finished(STORAGE_UPLOAD) + memory_tracer.finished(STORAGE_SIGN_URL):
        assert s.attributes["dependency"] == "s3"
        # Never the object key or the signed URL.
        assert "o.txt" not in str(s.attributes)
        assert "signed.example" not in str(s.attributes)


def test_inject_context_returns_none_without_active_span(restore_tracer) -> None:
    configure_tracer(NoOpTracer())
    assert inject_context() is None
