"""Single-actor migration entrypoint: ``python -m app.db.migrate``.

Exactly **one** actor applies schema changes per deployment; API and worker
replicas never migrate themselves (they only verify compatibility at startup via
:mod:`app.db.schema`). Running DDL from every replica would race N writers against
one schema and is explicitly disallowed.

Invocation:

* *(no subcommand)* - the default, fail-closed staging path: upgrade to the single
                      code head, then read the database's Alembic revision back and
                      exit ``0`` **only** when it exactly equals that head.
* ``upgrade``          - apply migrations up to ``head`` (or an explicit target)
                        without the post-upgrade read-back (operator/CI use).
* ``check``            - report schema compatibility without mutating; exit code
                        ``0`` when startup-safe, ``1`` otherwise.
* ``downgrade <rev>``  - step the schema down to an explicit revision (operator
                        escape hatch; requires the target revision, never a bare
                        ``head``).

Exit codes for the default upgrade-and-verify path: ``0`` success (db at head);
``3`` code graph has zero or multiple heads; ``4`` Alembic upgrade failed; ``5``
post-upgrade revision read-back failed; ``6`` database has zero or multiple
Alembic revisions; ``7`` database revision does not match the code head. The
explicit ``upgrade``/``downgrade`` subcommands also fail closed to ``4`` (fixed
classification, never a re-raised driver traceback); ``check`` exits ``1`` when
the schema state cannot be read at all.

Structured, secret-free logs describe each step; the database URL, credentials and
raw driver exceptions are never logged.
"""

from __future__ import annotations

import argparse

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger, log_event
from app.core.metrics import MIGRATION_RUNS_TOTAL, get_metrics
from app.db.schema import (
    alembic_config,
    check_schema_compatibility,
    code_head_revision,
)

logger = get_logger("signalnest.db.migrate")

# Fixed exit codes for the default upgrade-and-verify path (documented above).
EXIT_OK = 0
EXIT_CODE_HEADS = 3
EXIT_UPGRADE_FAILED = 4
EXIT_READBACK_FAILED = 5
EXIT_DB_HEADS = 6
EXIT_REVISION_MISMATCH = 7


def _single_code_head() -> str | None:
    """The lone code head, or ``None`` when the graph has zero or multiple heads."""
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    return heads[0] if len(heads) == 1 else None


def upgrade_and_verify() -> int:
    """Default path: upgrade to the single head, then verify the DB reached it.

    Fail-closed: returns ``0`` only after the database's Alembic revision is read
    back and matches the exact single code head. Emits only fixed, secret-free
    classifications; never the URL, credentials, SQL, or a driver traceback.
    """
    code_head = _single_code_head()
    if code_head is None:
        _record("upgrade_verify", "failure")
        log_event(
            logger, "migrate.head.ambiguous", component="migrate", outcome="failure"
        )
        return EXIT_CODE_HEADS

    log_event(logger, "migrate.upgrade.start", component="migrate", target=code_head)
    try:
        command.upgrade(alembic_config(), code_head)
    except Exception as exc:
        _record("upgrade_verify", "failure")
        log_event(
            logger,
            "migrate.upgrade.failed",
            component="migrate",
            outcome="failure",
            error_class=type(exc).__name__,
        )
        return EXIT_UPGRADE_FAILED

    # Read the applied revision back through the application DATABASE_URL over its
    # own short-lived TLS connection (never app.db.session; the URL carries the
    # TLS mode). Any driver error is swallowed to a fixed classification.
    engine = create_engine(get_settings().database_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            db_revisions = (
                conn.execute(text("SELECT version_num FROM alembic_version"))
                .scalars()
                .all()
            )
    except Exception as exc:
        _record("upgrade_verify", "failure")
        log_event(
            logger,
            "migrate.verify.readback_failed",
            component="migrate",
            outcome="failure",
            error_class=type(exc).__name__,
        )
        return EXIT_READBACK_FAILED
    finally:
        engine.dispose()

    if len(db_revisions) != 1:
        _record("upgrade_verify", "failure")
        log_event(
            logger,
            "migrate.verify.db_heads",
            component="migrate",
            outcome="failure",
            db_head_count=len(db_revisions),
            code_head=code_head,
        )
        return EXIT_DB_HEADS
    if db_revisions[0] != code_head:
        _record("upgrade_verify", "failure")
        log_event(
            logger,
            "migrate.verify.mismatch",
            component="migrate",
            outcome="failure",
            db_revision=db_revisions[0],
            code_head=code_head,
        )
        return EXIT_REVISION_MISMATCH

    # Success-event provenance: ``code_head`` comes from the repository script
    # directory (resolved above) and ``db_revision`` from the version-table
    # read-back — two independent sources, both computed into plain locals
    # before logging so a field-computation failure can never convert a
    # successful migration into an unhandled exit.
    db_revision = db_revisions[0]
    _record("upgrade_verify", "success")
    log_event(
        logger,
        "migrate.upgrade_verify.done",
        component="migrate",
        outcome="success",
        head=code_head,
        code_head=code_head,
        db_revision=db_revision,
    )
    return EXIT_OK


def _record(operation: str, outcome: str) -> None:
    # Bounded lifecycle metric (no-op unless a backend is installed). Labels are
    # low-cardinality: the operation kind and a coarse outcome only.
    get_metrics().increment(MIGRATION_RUNS_TOTAL, operation=operation, outcome=outcome)


def upgrade(target: str = "head") -> int:
    """Apply migrations up to ``target``. The single-actor write path.

    Fails closed to :data:`EXIT_UPGRADE_FAILED` with a fixed, secret-free
    classification — never a re-raise: a raw driver exception reaching the
    default excepthook would print an unredacted traceback (DSN and all).
    """
    log_event(logger, "migrate.upgrade.start", component="migrate", target=target)
    try:
        command.upgrade(alembic_config(), target)
    except Exception as exc:
        _record("upgrade", "failure")
        log_event(
            logger,
            "migrate.upgrade.failed",
            component="migrate",
            outcome="failure",
            error_class=type(exc).__name__,
        )
        return EXIT_UPGRADE_FAILED
    # Guarded field computation: a post-success helper failure must never turn
    # a completed upgrade into an unhandled exit (it is a logging field only).
    try:
        head = code_head_revision()
    except Exception:
        head = None
    _record("upgrade", "success")
    log_event(
        logger,
        "migrate.upgrade.done",
        component="migrate",
        outcome="success",
        head=head,
    )
    return EXIT_OK


def downgrade(target: str) -> int:
    """Step the schema down to an explicit revision (never a bare ``head``).

    Fails closed to :data:`EXIT_UPGRADE_FAILED` (the shared "Alembic operation
    failed" band) with a fixed classification instead of re-raising the raw
    driver exception into the default excepthook.
    """
    log_event(logger, "migrate.downgrade.start", component="migrate", target=target)
    try:
        command.downgrade(alembic_config(), target)
    except Exception as exc:
        _record("downgrade", "failure")
        log_event(
            logger,
            "migrate.downgrade.failed",
            component="migrate",
            outcome="failure",
            error_class=type(exc).__name__,
        )
        return EXIT_UPGRADE_FAILED
    _record("downgrade", "success")
    log_event(logger, "migrate.downgrade.done", component="migrate", outcome="success")
    return EXIT_OK


def check() -> int:
    """Report compatibility without mutating. Exit 0 if startup-safe, else 1.

    A connection/driver failure is contained by ``check_schema_compatibility``
    into a fixed :class:`~app.db.schema.SchemaVerificationError`; it is treated
    as not-startup-safe (exit 1) with a fixed event, never a traceback.
    """
    from app.db.schema import SchemaVerificationError
    from app.db.session import engine

    try:
        compat = check_schema_compatibility(engine)
    except SchemaVerificationError:
        _record("check", "unverifiable")
        log_event(
            logger,
            "migrate.check.unverifiable",
            component="migrate",
            outcome="failure",
        )
        return 1
    _record("check", compat.state.value)
    log_event(
        logger,
        "migrate.check",
        component="migrate",
        outcome=compat.state.value,
        db_revision=compat.db_revision,
        code_head=compat.code_head,
    )
    return 0 if compat.is_startup_safe else 1


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging("DEBUG" if settings.debug else "INFO")

    parser = argparse.ArgumentParser(prog="app.db.migrate", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    up = sub.add_parser("upgrade", help="apply migrations up to a target (default head)")
    up.add_argument("target", nargs="?", default="head")

    down = sub.add_parser("downgrade", help="step down to an explicit revision")
    down.add_argument("target")

    sub.add_parser("check", help="report compatibility without mutating")

    args = parser.parse_args(argv)
    # The bare, argv-less invocation is the fail-closed staging path:
    # upgrade to the single head, then verify the database reached it.
    if args.command is None:
        return upgrade_and_verify()
    cmd = args.command
    if cmd == "upgrade":
        return upgrade(args.target)
    if cmd == "downgrade":
        if args.target in (None, "", "head"):
            parser.error("downgrade requires an explicit target revision (never 'head')")
        return downgrade(args.target)
    if cmd == "check":
        return check()
    parser.error(f"unknown command {cmd!r}")  # pragma: no cover - argparse guards
    return 2


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
