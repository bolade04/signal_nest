"""Tests for the fail-closed migration entrypoint and migration-mode settings.

Covers the Batch 4F hardening: the bare ``python -m app.db.migrate`` invocation
must upgrade to the single code head and then read the database revision back and
exit 0 only on an exact match; and ``migration_mode`` must let the staging
migration actor initialize with only ``DATABASE_URL`` while ordinary staging
startup keeps requiring every production-grade secret.
"""

from __future__ import annotations

import logging

import pytest

import app.db.migrate as migrate
from app.core.config import Settings

# --------------------------------------------------------------------------
# migration_mode settings isolation
# --------------------------------------------------------------------------
_PG_URL = "postgresql+psycopg://u:p@db.invalid:5432/signalnest?sslmode=require"


def _settings(**env: str) -> Settings:
    return Settings(**env)


def test_migration_mode_staging_needs_only_database_url() -> None:
    s = Settings(environment="staging", migration_mode=True, database_url=_PG_URL)
    assert s.migration_mode is True
    assert s.environment == "staging"


def test_migration_mode_reads_sn_migration_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SN_MIGRATION_MODE", "1")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DATABASE_URL", _PG_URL)
    s = Settings()
    assert s.migration_mode is True


def test_migration_mode_rejects_development() -> None:
    with pytest.raises(ValueError, match="no development fallback"):
        Settings(environment="development", migration_mode=True, database_url=_PG_URL)


def test_migration_mode_rejects_sqlite() -> None:
    with pytest.raises(ValueError, match="no sqlite/local fallback"):
        Settings(
            environment="staging",
            migration_mode=True,
            database_url="sqlite:///./x.db",
        )


def test_normal_staging_still_requires_secret_key() -> None:
    with pytest.raises(ValueError, match="secret_key must be set"):
        Settings(environment="staging", app_mode="full", database_url=_PG_URL)


def test_normal_staging_full_requires_llm_key() -> None:
    with pytest.raises(ValueError, match="requires llm_api_key"):
        Settings(
            environment="staging",
            app_mode="full",
            llm_provider="anthropic",
            secret_key="a-strong-non-default-secret-value",
            database_url=_PG_URL,
        )


def test_normal_staging_full_redis_backend_requires_redis_url() -> None:
    # REDIS_URL is required only where the app contract selects a redis backend;
    # ordinary staging startup (migration_mode off) still enforces it.
    with pytest.raises(ValueError, match="requires redis_url"):
        Settings(
            environment="staging",
            app_mode="full",
            queue_backend="redis",
            secret_key="a-strong-non-default-secret-value",
            llm_provider="anthropic",
            llm_api_key="fabricated-key",
            storage_backend="local",
            database_url=_PG_URL,
        )


def test_migration_mode_does_not_leak_into_default_settings() -> None:
    # A development default construction never enters migration mode.
    s = Settings()
    assert s.migration_mode is False


# --------------------------------------------------------------------------
# upgrade-and-verify dispatch and fail-closed behaviour
# --------------------------------------------------------------------------
class _FakeScalars:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def all(self) -> list[str]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeConn:
    def __init__(self, rows: list[str], raise_on_execute: bool) -> None:
        self._rows = rows
        self._raise = raise_on_execute

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def execute(self, _stmt: object) -> _FakeResult:
        if self._raise:
            raise RuntimeError("connection refused to db.invalid:5432 pw=hunter2")
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, rows: list[str], raise_on_execute: bool = False) -> None:
        self._rows = rows
        self._raise = raise_on_execute
        self.disposed = False

    def connect(self) -> _FakeConn:
        return _FakeConn(self._rows, self._raise)

    def dispose(self) -> None:
        self.disposed = True


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    *,
    code_head: str | None,
    upgrade_raises: bool = False,
    db_rows: list[str] | None = None,
    engine_raises: bool = False,
) -> dict[str, object]:
    state: dict[str, object] = {"upgraded_to": None}

    monkeypatch.setattr(migrate, "_single_code_head", lambda: code_head)

    def fake_upgrade(_cfg: object, target: str) -> None:
        state["upgraded_to"] = target
        if upgrade_raises:
            raise RuntimeError("alembic failed: host=db.invalid user=signalnest_app")

    monkeypatch.setattr(migrate.command, "upgrade", fake_upgrade)
    monkeypatch.setattr(migrate, "alembic_config", lambda: object())

    engine = _FakeEngine(db_rows or [], raise_on_execute=engine_raises)
    monkeypatch.setattr(migrate, "create_engine", lambda *a, **k: engine)

    class _S:
        database_url = _PG_URL

    monkeypatch.setattr(migrate, "get_settings", lambda: _S())
    state["engine"] = engine
    return state


def test_multiple_code_heads_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, code_head=None)
    assert migrate.upgrade_and_verify() == migrate.EXIT_CODE_HEADS


def test_upgrade_failure_prevents_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_common(monkeypatch, code_head="98289430a3ec", upgrade_raises=True)
    assert migrate.upgrade_and_verify() == migrate.EXIT_UPGRADE_FAILED
    # verification engine was never consulted (no dispose call)
    assert state["engine"].disposed is False  # type: ignore[attr-defined]


def test_readback_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, code_head="98289430a3ec", engine_raises=True)
    assert migrate.upgrade_and_verify() == migrate.EXIT_READBACK_FAILED


def test_multiple_db_heads_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(
        monkeypatch, code_head="98289430a3ec", db_rows=["98289430a3ec", "b2c3d4e5f6a7"]
    )
    assert migrate.upgrade_and_verify() == migrate.EXIT_DB_HEADS


def test_revision_mismatch_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common(monkeypatch, code_head="98289430a3ec", db_rows=["4945b98229e6"])
    assert migrate.upgrade_and_verify() == migrate.EXIT_REVISION_MISMATCH


def test_exact_match_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _patch_common(monkeypatch, code_head="98289430a3ec", db_rows=["98289430a3ec"])
    assert migrate.upgrade_and_verify() == migrate.EXIT_OK
    assert state["upgraded_to"] == "98289430a3ec"
    assert state["engine"].disposed is True  # type: ignore[attr-defined]


def test_bare_invocation_dispatches_upgrade_and_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}
    monkeypatch.setattr(
        migrate, "upgrade_and_verify", lambda: called.__setitem__("n", called["n"] + 1) or 0
    )
    # main() calls get_settings() for logging; keep it cheap and secret-free.
    monkeypatch.setattr(migrate, "get_settings", lambda: Settings())
    rc = migrate.main([])
    assert rc == 0
    assert called["n"] == 1


def test_explicit_upgrade_subcommand_is_upgrade_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify_called = {"n": 0}
    upgrade_called = {"n": 0}
    monkeypatch.setattr(
        migrate, "upgrade_and_verify", lambda: verify_called.__setitem__("n", 1) or 0
    )
    monkeypatch.setattr(
        migrate, "upgrade", lambda target="head": upgrade_called.__setitem__("n", 1) or 0
    )
    monkeypatch.setattr(migrate, "get_settings", lambda: Settings())
    migrate.main(["upgrade"])
    assert upgrade_called["n"] == 1
    assert verify_called["n"] == 0


def test_no_aws_or_secrets_manager_import_in_migrate() -> None:
    import inspect

    src = inspect.getsource(migrate)
    assert "boto3" not in src
    assert "secretsmanager" not in src
    assert "get_secret_value" not in src


def test_only_database_url_consumed_by_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    # The verify path builds its engine solely from settings.database_url.
    captured: dict[str, object] = {}
    _patch_common(monkeypatch, code_head="98289430a3ec", db_rows=["98289430a3ec"])

    def capture_engine(url: str, **_k: object) -> _FakeEngine:
        captured["url"] = url
        return _FakeEngine(["98289430a3ec"])

    monkeypatch.setattr(migrate, "create_engine", capture_engine)
    assert migrate.upgrade_and_verify() == migrate.EXIT_OK
    assert captured["url"] == _PG_URL


def test_no_url_or_credentials_in_output(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture
) -> None:
    # Across success, mismatch, upgrade-fail and read-back-fail, no URL/host/pw/
    # SQL/traceback token ever reaches logs or stdout/stderr. Each scenario
    # FIRST asserts its expected classification event actually emitted (a path
    # that emits nothing must fail), and output is captured exactly once per
    # scenario with both streams read from the same capture. caplog is valid
    # here because these stub-based tests never run configure_logging (which
    # would evict pytest's capture handler); the real-formatter coverage lives
    # in test_migrate_real_alembic_logging.py.
    secrets_tokens = ["db.invalid", "hunter2", "signalnest_app", "version_num", "Traceback"]
    expected_event = {
        "ok": "migrate.upgrade_verify.done",
        "mismatch": "migrate.verify.mismatch",
        "upgrade_fail": "migrate.upgrade.failed",
        "readback_fail": "migrate.verify.readback_failed",
    }

    for scenario in ("ok", "mismatch", "upgrade_fail", "readback_fail"):
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            if scenario == "ok":
                _patch_common(monkeypatch, code_head="98289430a3ec", db_rows=["98289430a3ec"])
            elif scenario == "mismatch":
                _patch_common(monkeypatch, code_head="98289430a3ec", db_rows=["4945b98229e6"])
            elif scenario == "upgrade_fail":
                _patch_common(monkeypatch, code_head="98289430a3ec", upgrade_raises=True)
            else:
                _patch_common(monkeypatch, code_head="98289430a3ec", engine_raises=True)
            migrate.upgrade_and_verify()
        # Positive emission first: the scenario's classification is present.
        assert any(
            r.getMessage() == expected_event[scenario] for r in caplog.records
        ), f"expected event {expected_event[scenario]!r} not emitted in {scenario}"
        cap = capsys.readouterr()  # single capture; both streams from one read
        blob = caplog.text + cap.out + cap.err
        for tok in secrets_tokens:
            assert tok not in blob, f"{tok!r} leaked in scenario {scenario}"


def test_check_and_downgrade_subcommands_preserved() -> None:
    # The public API still exposes the pre-existing subcommands.
    assert callable(migrate.check)
    assert callable(migrate.downgrade)
    assert callable(migrate.upgrade)


def test_revision_mismatch_reports_both_sources(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Field-level provenance where the two values GENUINELY DIFFER: the mismatch
    # event must carry the database's value in db_revision and the repository's
    # in code_head. An implementation deriving both fields from one source
    # cannot produce two different values here.
    _patch_common(monkeypatch, code_head="98289430a3ec", db_rows=["4945b98229e6"])
    with caplog.at_level(logging.INFO):
        assert migrate.upgrade_and_verify() == migrate.EXIT_REVISION_MISMATCH
    rec = [r for r in caplog.records if r.getMessage() == "migrate.verify.mismatch"]
    assert len(rec) == 1
    assert rec[0].extra_fields["db_revision"] == "4945b98229e6"
    assert rec[0].extra_fields["code_head"] == "98289430a3ec"


class _TaggedRevision(str):
    """A str subclass marking values that came from the version-table read-back."""


def test_success_event_db_revision_sourced_from_readback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # On the success path db_revision NECESSARILY equals code_head (EXIT_OK is
    # returned only on equality), so no value comparison can pin the field's
    # source. Identity does: the fake read-back rows carry a tagged str
    # subclass, and the emitted db_revision must BE that tagged object while
    # code_head must not be. A same-source forgery (db_revision=code_head)
    # emits a plain str here and fails.
    row = _TaggedRevision("98289430a3ec")
    _patch_common(monkeypatch, code_head="98289430a3ec", db_rows=[row])
    with caplog.at_level(logging.INFO):
        assert migrate.upgrade_and_verify() == migrate.EXIT_OK
    rec = [
        r for r in caplog.records if r.getMessage() == "migrate.upgrade_verify.done"
    ]
    assert len(rec) == 1
    assert type(rec[0].extra_fields["db_revision"]) is _TaggedRevision
    assert type(rec[0].extra_fields["code_head"]) is not _TaggedRevision


def test_explicit_upgrade_fails_closed_without_reraise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A raw driver exception from the upgrade subcommand must map to the fixed
    # exit band (never re-raise into the default excepthook's traceback).
    _patch_common(monkeypatch, code_head="98289430a3ec", upgrade_raises=True)
    with caplog.at_level(logging.INFO):
        rc = migrate.upgrade("head")
    assert rc == migrate.EXIT_UPGRADE_FAILED
    records = [r for r in caplog.records if r.getMessage() == "migrate.upgrade.failed"]
    assert len(records) == 1
    assert records[0].extra_fields["error_class"] == "RuntimeError"
    assert "db.invalid" not in caplog.text


def test_explicit_downgrade_fails_closed_without_reraise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(_cfg: object, _target: str) -> None:
        raise RuntimeError("alembic failed: host=db.invalid user=signalnest_app")

    monkeypatch.setattr(migrate.command, "downgrade", _boom)
    monkeypatch.setattr(migrate, "alembic_config", lambda: object())
    with caplog.at_level(logging.INFO):
        rc = migrate.downgrade("9a7c614699d8")
    assert rc == migrate.EXIT_UPGRADE_FAILED
    records = [r for r in caplog.records if r.getMessage() == "migrate.downgrade.failed"]
    assert len(records) == 1
    assert records[0].extra_fields["error_class"] == "RuntimeError"
    assert "db.invalid" not in caplog.text
