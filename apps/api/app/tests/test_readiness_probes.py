"""Bounded readiness-probe unit tests (Phase 3A.2, follow-up #3).

These exercise the probe framework and its safe/operator disclosure split
without any external service: the default local stack is actively probed, a
misconfigured backend is flagged (never falsely healthy), and public output
carries no infrastructure topology.
"""

from __future__ import annotations

import time

from app.core.config import Settings
from app.system.probes import (
    ProbeResult,
    ProbeStatus,
    ReadinessProbe,
    run_readiness_probes,
)


def _local_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_local_stack_probes_are_ready() -> None:
    report = run_readiness_probes(_local_settings())
    assert report.ready is True
    names = {r.name for r in report.results}
    assert names == {
        "database",
        "queue",
        "durable_queue",
        "cache",
        "vector",
        "storage",
        "llm",
    }
    # The default local backends actively verify as healthy.
    by_name = {r.name: r for r in report.results}
    for name in ("database", "queue", "durable_queue", "cache", "vector"):
        assert by_name[name].status is ProbeStatus.HEALTHY


def test_public_probe_output_excludes_infra_detail() -> None:
    report = run_readiness_probes(_local_settings())
    for result in report.results:
        public = result.to_public_dict()
        assert set(public.keys()) == {"name", "status", "required"}
        # The operator diagnostic may carry more, but never secrets.
        blob = repr(result.to_operator_dict()).lower()
        for forbidden in ("password", "api_key", "secret", "redis://", "postgresql://"):
            assert forbidden not in blob


def test_s3_without_bucket_is_not_configured_not_healthy() -> None:
    # A placeholder external backend must not self-report healthy. (s3 without a
    # bucket cannot pass Settings validation, so probe the check directly.)
    from app.system.probes import _check_storage

    settings = _local_settings(storage_backend="s3", s3_bucket="placeholder")
    status, summary, _detail, _retry = _check_storage(settings)
    assert status is ProbeStatus.DEGRADED  # configured but not actively verified
    assert "healthy" not in summary.lower()


def test_storage_probe_rejects_path_traversal() -> None:
    from app.system.probes import _check_storage

    settings = _local_settings(local_storage_dir="../../etc")
    status, _summary, detail, _retry = _check_storage(settings)
    assert status is ProbeStatus.UNAVAILABLE
    assert "traversal" in (detail or "")


def test_real_llm_provider_is_degraded_not_healthy_without_live_call() -> None:
    from app.system.probes import _check_llm

    settings = _local_settings(llm_provider="openai", llm_api_key="sk-test")
    status, summary, _detail, _retry = _check_llm(settings)
    assert status is ProbeStatus.DEGRADED
    assert "not actively verified" in summary


def test_probe_timeout_is_bounded_and_nonblocking() -> None:
    def _hang(_settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
        time.sleep(5)
        return (ProbeStatus.HEALTHY, "never", None, False)

    probe = ReadinessProbe("slow", required=True, check=_hang)
    started = time.perf_counter()
    result = probe.run(_local_settings(), timeout=0.2)
    elapsed = time.perf_counter() - started
    assert result.status is ProbeStatus.TIMEOUT
    assert result.retryable is True
    assert elapsed < 2.0  # bounded well under the hung 5s check


def test_probe_error_detail_excludes_raw_exception_message() -> None:
    # A failing probe must not leak the raw exception text (which can embed a
    # host, port or connection URL) into the operator diagnostic detail.
    def _boom(_settings: Settings) -> tuple[ProbeStatus, str, str | None, bool]:
        raise RuntimeError("could not connect to db-host:5432 as user secret")

    probe = ReadinessProbe("database", required=True, check=_boom)
    result = probe.run(_local_settings(), timeout=1.0)
    assert result.status is ProbeStatus.UNAVAILABLE
    assert result.detail == "RuntimeError"
    blob = repr(result.to_operator_dict())
    for forbidden in ("db-host", "5432", "secret"):
        assert forbidden not in blob


def test_failed_required_probe_blocks_readiness() -> None:
    result = ProbeResult(
        name="database",
        status=ProbeStatus.UNAVAILABLE,
        required=True,
        summary="check failed",
    )
    assert result.is_blocking is True
    ok = ProbeResult(name="llm", status=ProbeStatus.DEGRADED, required=False, summary="")
    assert ok.is_blocking is False
