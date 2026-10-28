"""P6-PRIV-1: the data inventory is complete, and the retention settings have no default.

* Every table on ``Base.metadata`` has exactly one inventory entry, and the human inventory
  (``docs/operations/data-inventory.md``) names every one of them.
* Every foreign key's delete rule is one the erasure walk follows (CASCADE or SET NULL), every
  SET NULL column is nullable, and the walk refuses any other rule (negative control).
* Every exported table can be scoped to one organization; every excluded column exists; every
  retention root of a workspace class has a ``workspace_id``.
* Retention durations: unset by default everywhere; bounded when set; ``production`` refuses to
  start until all seven are set; the migration actor is exempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Column, ForeignKey, MetaData, String, Table

from app.core.config import Settings
from app.db.models import Base
from app.privacy import erasure
from app.privacy.inventory import (
    PLATFORM_CLASSES,
    TABLES,
    WORKSPACE_CLASSES,
    RetentionClass,
)
from app.privacy.retention import _ROOTS
from app.tests._platform_support import pgvector_build

REPO_ROOT = Path(__file__).resolve().parents[4]
DOC = REPO_ROOT / "docs" / "operations" / "data-inventory.md"


def test_every_table_has_exactly_one_inventory_entry():
    assert set(TABLES) == set(Base.metadata.tables)
    assert len(TABLES) == 40


def test_the_document_names_every_table():
    text = DOC.read_text(encoding="utf-8")
    missing = sorted(t for t in Base.metadata.tables if f"`{t}`" not in text)
    assert missing == []


def test_every_foreign_key_rule_is_followed_by_the_walk():
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            rule = (fk.ondelete or "").upper()
            assert rule in {"CASCADE", "SET NULL"}, (table.name, fk.parent.name, rule)
            if rule == "SET NULL":
                assert fk.parent.nullable, (table.name, fk.parent.name)


def test_the_walk_refuses_an_unsupported_delete_rule(monkeypatch):
    """Negative control: a RESTRICT key is never skipped silently."""
    metadata = MetaData()
    parent = Table("p", metadata, Column("id", String, primary_key=True))
    child = Table(
        "c", metadata, Column("id", String, primary_key=True),
        Column("p_id", ForeignKey("p.id", ondelete="RESTRICT")),
    )
    monkeypatch.setattr(erasure, "_references", lambda: {"p": ((child.c.p_id, "RESTRICT"),)})
    with pytest.raises(erasure.UnsupportedDeleteRule):
        erasure.delete_rows(object(), parent, ["x"])  # raises before any statement


def test_exported_tables_are_organization_scoped_and_exclusions_exist():
    for name, entry in TABLES.items():
        table = Base.metadata.tables[name]
        for column in entry.excluded_columns:
            assert column in table.c, (name, column)
        if entry.exported:
            assert (
                name == "organizations"
                or "organization_id" in table.c
                or "workspace_id" in table.c
            ), name


def test_retention_classes_are_partitioned_and_rooted():
    assert WORKSPACE_CLASSES | PLATFORM_CLASSES == set(RetentionClass)
    assert not WORKSPACE_CLASSES & PLATFORM_CLASSES
    for data_class, roots in _ROOTS.items():
        for model, _predicate in roots:
            assert TABLES[model.__tablename__].retention_class is data_class
            if data_class in WORKSPACE_CLASSES:
                assert "workspace_id" in model.__table__.c, model.__tablename__
    rooted = {e.table for e in TABLES.values() if e.retention_class is not None}
    assert rooted == {m.__tablename__ for roots in _ROOTS.values() for m, _ in roots}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
ALL_SET = {c.setting_name: 30 for c in RetentionClass}


def _build(kwargs: dict) -> Settings:
    # A production stack is only accepted by a pgvector-implementing build (P6-PLAT-1).
    with pgvector_build():
        return Settings(**kwargs)


def _production(**overrides) -> dict:
    kwargs = dict(
        _env_file=None,
        environment="production",
        app_mode="full",
        database_url="postgresql+psycopg://u:p@db.internal:5432/signalnest",
        queue_backend="redis",
        cache_backend="redis",
        redis_url="redis://cache.internal:6379/0",
        storage_backend="s3",
        s3_bucket="example-bucket",
        vector_backend="pgvector",
        rate_limit_backend="redis",
        secret_key="a-real-production-secret-value-0123456789",
        llm_provider="anthropic",
        llm_api_key="inert-test-value",
        mail_backend="ses",
        mail_from_address="no-reply@example.com",
        mail_ses_region="us-east-1",
        public_web_origin="https://app.example.com",
    )
    kwargs.update(ALL_SET)
    kwargs.update(overrides)
    return kwargs


def test_every_retention_setting_exists_and_is_unset_by_default():
    settings = Settings(_env_file=None)
    for data_class in RetentionClass:
        assert getattr(settings, data_class.setting_name) is None


def test_production_with_every_duration_set_is_accepted():
    assert _build(_production()).is_production


@pytest.mark.parametrize("data_class", list(RetentionClass))
def test_production_refuses_to_start_with_any_duration_unset(data_class):
    with pytest.raises(ValueError) as exc:
        _build(_production(**{data_class.setting_name: None}))
    assert f"{data_class.setting_name} must be set explicitly in production" in str(exc.value)


def test_staging_and_development_accept_unset_durations():
    for environment in ("development", "test"):
        Settings(_env_file=None, environment=environment)
    staging = _production(environment="staging", **{c.setting_name: None for c in RetentionClass})
    assert _build(staging).environment == "staging"


def test_the_migration_actor_is_exempt():
    kwargs = _production(migration_mode=True, **{c.setting_name: None for c in RetentionClass})
    assert _build(kwargs).migration_mode


@pytest.mark.parametrize("value", [0, -1, 36501])
def test_out_of_range_durations_are_rejected(value):
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, retention_audit_logs_days=value)
    assert "retention_audit_logs_days must be between 1 and 36500 days" in str(exc.value)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("retention_purge_batch_size", 0, "retention_purge_batch_size must be between"),
        ("retention_purge_batch_size", 10001, "retention_purge_batch_size must be between"),
        ("retention_purge_max_batches", 0, "retention_purge_max_batches must be >= 1"),
    ],
)
def test_purge_bounds(field, value, message):
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, **{field: value})
    assert message in str(exc.value)
