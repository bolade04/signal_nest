"""Single-actor migration entrypoint: ``python -m app.db.migrate``.

Exactly **one** actor applies schema changes per deployment; API and worker
replicas never migrate themselves (they only verify compatibility at startup via
:mod:`app.db.schema`). Running DDL from every replica would race N writers against
one schema and is explicitly disallowed.

Subcommands:

* ``upgrade`` (default) — apply migrations up to ``head`` (or an explicit target).
* ``check``             — report schema compatibility without mutating; exit code
                          ``0`` when startup-safe, ``1`` otherwise.
* ``downgrade <rev>``   — step the schema down to an explicit revision (operator
                          escape hatch; requires the target revision, never a bare
                          ``head``).

Structured, secret-free logs describe each step; the database URL is never logged.
"""

from __future__ import annotations

import argparse

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


def _record(operation: str, outcome: str) -> None:
    # Bounded lifecycle metric (no-op unless a backend is installed). Labels are
    # low-cardinality: the operation kind and a coarse outcome only.
    get_metrics().increment(MIGRATION_RUNS_TOTAL, operation=operation, outcome=outcome)


def upgrade(target: str = "head") -> int:
    """Apply migrations up to ``target``. The single-actor write path."""
    log_event(logger, "migrate.upgrade.start", component="migrate", target=target)
    try:
        command.upgrade(alembic_config(), target)
    except Exception:
        _record("upgrade", "failure")
        log_event(logger, "migrate.upgrade.failed", component="migrate", outcome="failure")
        raise
    _record("upgrade", "success")
    log_event(
        logger,
        "migrate.upgrade.done",
        component="migrate",
        outcome="success",
        head=code_head_revision(),
    )
    return 0


def downgrade(target: str) -> int:
    """Step the schema down to an explicit revision (never a bare ``head``)."""
    log_event(logger, "migrate.downgrade.start", component="migrate", target=target)
    try:
        command.downgrade(alembic_config(), target)
    except Exception:
        _record("downgrade", "failure")
        log_event(logger, "migrate.downgrade.failed", component="migrate", outcome="failure")
        raise
    _record("downgrade", "success")
    log_event(logger, "migrate.downgrade.done", component="migrate", outcome="success")
    return 0


def check() -> int:
    """Report compatibility without mutating. Exit 0 if startup-safe, else 1."""
    from app.db.session import engine

    compat = check_schema_compatibility(engine)
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
    cmd = args.command or "upgrade"
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
