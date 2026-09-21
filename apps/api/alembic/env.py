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

from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.db.downgrade_guard import (
    CONFIRM_KEY,
    OP_DOWNGRADE,
    OP_SAFE,
    OP_STAMP,
    DowngradeTargetError,
    chain_identity,
    classify_operation,
    confirmation_token,
    live_identity,
    require_confirmation,
    resolve_downgrade,
    target_fingerprint,
)
from app.db.models import Base  # noqa: F401  (imports all models onto Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime database URL from application settings.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata

_IS_SQLITE = settings.database_url.startswith("sqlite")

# Every Alembic entry point loads this module, which makes it the one place that
# sees `alembic downgrade`, `app.db.migrate downgrade`, `npm run migrate:down`,
# `command.downgrade(...)` and CI alike. Guarding the wrapper instead would
# protect the one path with no shipped callers and leave the four that have them.
#
# Direction comes from the EnvironmentContext that Alembic installs before this
# module runs. `_proxy` is private, so classification is an ALLOWLIST: only
# operations known to leave downgrade-authorization state alone run ungated, and
# an unrecognised or undetectable name is refused. An Alembic release that
# renames either the proxy or an inner function therefore fails loudly on the
# upgrade path every CI job exercises, instead of silently disarming the guard
# on the downgrade path nothing routine exercises.


def _operation_name() -> str | None:
    proxy = getattr(context, "_proxy", None)
    opts = getattr(proxy, "context_opts", None)
    fn = opts.get("fn") if isinstance(opts, dict) else None
    return getattr(fn, "__name__", None)


def _requested_target() -> str:
    proxy = getattr(context, "_proxy", None)
    opts = getattr(proxy, "context_opts", None)
    dest = opts.get("destination_rev") if isinstance(opts, dict) else None
    # `stamp` hands this over as a tuple; a single element is the ordinary case.
    # More than one is an ambiguous request, so it is left unresolvable and ends
    # up gated rather than guessed at.
    if isinstance(dest, (tuple, list)):
        dest = dest[0] if len(dest) == 1 else ",".join(str(d) for d in dest)
    return "base" if dest is None else str(dest)


def _supplied_confirmation() -> str | None:
    """`-x confirm=...` for the CLI, `config.attributes` for programmatic callers.

    Deliberately not an environment variable: both of these are per-invocation and
    cannot be left exported for the rest of a shell session. Both channels feed
    the same comparison against the same expected value, so neither accepts what
    the other rejects.
    """
    try:
        supplied = context.get_x_argument(as_dictionary=True).get(CONFIRM_KEY)
    except Exception:  # pragma: no cover - x-args unavailable on some paths
        supplied = None
    if supplied is not None:
        return supplied
    attributes = getattr(config, "attributes", None) or {}
    value = attributes.get(CONFIRM_KEY)
    return value if isinstance(value, str) else None


def _single_source_revision(migration_context) -> str:
    """The one revision the database is at, or a refusal.

    `get_current_heads()` is public and returns () for an absent or empty version
    table and several entries for unmerged branches. Both are ambiguous states, and
    an ambiguous source cannot be bound into a confirmation, so both refuse rather
    than have the guard invent an identity. SignalNest ships a single linear head.
    """
    heads = migration_context.get_current_heads()
    if len(heads) != 1:
        raise DowngradeTargetError(
            "the database's revision state is not a single revision "
            f"({len(heads)} found), so a destructive migration cannot be confirmed."
        )
    return heads[0]


def _guard_destructive(connection) -> None:
    """Refuse a destructive migration that is not confirmed for THIS transition.

    Runs after the connection is open and before any migration executes. Reading
    the live revision is what makes a used confirmation stale, so zero database
    I/O was given up deliberately; what is preserved is that no DDL and no
    destructive mutation happen before this returns.
    """
    operation = classify_operation(_operation_name())
    if operation == OP_SAFE:
        return  # upgrade, check, current, show - untouched

    if operation not in (OP_DOWNGRADE, OP_STAMP):
        # Refuse BEFORE reading the database. Reading first would report a
        # fresh database's empty revision state -- the CI shape -- and send the
        # operator after a database problem when the real cause is an Alembic
        # operation name this guard does not recognise.
        raise DowngradeTargetError(
            "the Alembic operation could not be identified, so a destructive "
            "migration cannot be ruled out. Refusing."
        )

    migration_context = context.get_context()
    source = _single_source_revision(migration_context)
    script = ScriptDirectory.from_config(config)
    requested = _requested_target()

    if operation == OP_STAMP:
        # `stamp` writes no DDL, but it rewrites `alembic_version` -- the very
        # fact every confirmation is bound to. A stamp in EITHER direction is
        # therefore authorization-state mutation and is gated.
        #
        # Forward stamps are not exempt, and the earlier "forward is free" fast
        # path was the re-arm vector: after a downgrade OLD -> X, stamping back
        # to OLD is a forward move, and it restored every input the spent token
        # was computed from, so the exact stale confirmation became valid again.
        # It also leaves `alembic_version` claiming a schema the database does
        # not have, which defeats the startup compatibility check.
        #
        # Gating makes the stamp confirmable, not impossible: a CONFIRMED stamp
        # back to OLD still restores every input, so the spent confirmation is
        # valid again, and `migrate check` reports `compatible` on a schema
        # missing its objects. That is the same transition the operator already
        # reviewed, against a target the refusal names -- see the module's
        # second residual.
        command = f"alembic -x {CONFIRM_KEY}=<token> stamp {requested}"
        requested = f"stamp:{requested}"
        resolved, chain = None, []
    else:  # OP_DOWNGRADE -- the only remaining classification
        command = f"alembic -x {CONFIRM_KEY}=<token> downgrade {requested}"
        resolved, chain = resolve_downgrade(script, source, requested)

    # A URL is a claim about a destination; a tunnel or DNS alias can make that
    # claim false while the fingerprint still matches. Ask the server instead,
    # and bind the answer into the confirmation rather than comparing it to the
    # URL -- a hostname and a server address are different namespaces.
    identity = live_identity(connection, settings.database_url)

    expected = confirmation_token(
        database_url=settings.database_url,
        live_identity=identity,
        source_revision=source,
        requested_target=requested,
        resolved_destination=resolved,
        chain_identity=chain_identity(script, chain),
    )
    require_confirmation(
        supplied=_supplied_confirmation(),
        expected=expected,
        fingerprint=target_fingerprint(settings.database_url),
        live_target=identity,
        source_revision=source,
        requested_target=requested,
        chain=chain,
        command=command.replace("<token>", expected),
    )


def run_migrations_offline() -> None:
    """Run migrations without a live DBAPI connection (emit SQL)."""
    if classify_operation(_operation_name()) != OP_SAFE:
        # Offline mode emits a script that is executed later, elsewhere, against
        # whatever database someone pipes it into. A confirmation minted against a
        # target this process never connected to says nothing about where the SQL
        # lands -- which is the exact lie target confusion exploits. A stamp is
        # refused for the same reason: an `UPDATE alembic_version` script is
        # authorization state someone can apply anywhere.
        #
        # The test is the same allowlist the online path uses. Refusing only a
        # positively-identified downgrade would fall open the moment direction
        # became undetectable, which is precisely when it must not.
        raise DowngradeTargetError(
            "offline (--sql) destructive operations are not supported: the "
            "generated script would be applied to a database this process cannot "
            "identify. Run the operation against the target database directly."
        )
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
        _guard_destructive(connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
