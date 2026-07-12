"""Alembic migration environment.

The database URL and target metadata both come from the application itself, so a single
migration set drives SQLite (local mode) and PostgreSQL (full mode):

* ``get_settings().database_url`` provides the connection string.
* Importing ``app.db.models`` populates ``Base.metadata`` with every ORM table.

SQLite cannot ``ALTER`` most columns, so ``render_as_batch=True`` is enabled to make
downgrades/edits portable across both engines.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.db.models import Base  # noqa: F401  (imports all models onto Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime database URL from application settings.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata

_IS_SQLITE = settings.database_url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations without a live DBAPI connection (emit SQL)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=_IS_SQLITE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=_IS_SQLITE,
            # Enforce FK integrity during SQLite batch migrations.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
