"""The process engine never prints bound parameters (D-9e, review r8 C-r8-3).

``source_takedown_cases`` holds an operator's private free text. SQLAlchemy's own
``sqlalchemy.engine`` logger prints the bound parameters of every statement when it is
raised to INFO/DEBUG, and DBAPI error text embeds them. ``build_engine`` creates the
engine with ``hide_parameters=True``, so neither can carry the reason.

Scope: ``hide_parameters`` covers statement parameters and error text. SQLAlchemy's DEBUG
result-row logging (``Row (...)``) is not controlled by any engine option; it needs the
``sqlalchemy.engine`` logger raised to DEBUG, which no supported configuration does
(``configure_logging`` leaves it at WARNING). The test therefore exercises the write path.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.models import SourceTakedown, SourceTakedownCase
from app.core.config import Settings
from app.db.models import Base
from app.db.session import build_engine

MARKER = "PRIVATE-REASON-ZQ77 counsel memo"
TICKET = "PRIVATE-TICKET-ZQ77"


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage() + " " + str(record.__dict__.get("args", "")))


def test_a_raised_sqlalchemy_logger_does_not_print_the_private_reason(tmp_path):
    url = f"sqlite:///{tmp_path}/hp.db"
    engine = build_engine(Settings(_env_file=None, database_url=url))
    Base.metadata.create_all(engine)
    handler = _Collect()
    logger = logging.getLogger("sqlalchemy.engine")
    saved = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with Session(engine) as s:
            row = SourceTakedown(source_key="hide-params-key")
            s.add(row)
            s.flush()
            s.add(
                SourceTakedownCase(
                    takedown_id=row.id, source_key=row.source_key, reason=MARKER,
                    external_reference=TICKET,
                )
            )
            s.commit()
    finally:
        logger.setLevel(saved)
        logger.removeHandler(handler)
        engine.dispose()
    # Positive control: the statements were logged at DEBUG, only their parameters hidden.
    assert any("INSERT INTO source_takedown_cases" in line for line in handler.lines)
    assert any("[SQL parameters hidden due to hide_parameters=True]" in x for x in handler.lines)
    for line in handler.lines:
        assert MARKER not in line and TICKET not in line and "ZQ77" not in line, line
    with Session(engine2 := build_engine(Settings(_env_file=None, database_url=url))) as s:
        assert s.scalars(select(SourceTakedownCase.reason)).one() == MARKER  # it was stored
    engine2.dispose()


def test_a_database_error_does_not_embed_the_private_reason(tmp_path):
    engine = build_engine(Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/he.db"))
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as s, pytest.raises(IntegrityError) as raised:
            # takedown_id NULL violates NOT NULL: the failing statement carries the text.
            s.add(SourceTakedownCase(takedown_id=None, source_key="k", reason=MARKER))
            s.commit()
        assert "ZQ77" not in str(raised.value)
        assert "hidden" in str(raised.value)
    finally:
        engine.dispose()
