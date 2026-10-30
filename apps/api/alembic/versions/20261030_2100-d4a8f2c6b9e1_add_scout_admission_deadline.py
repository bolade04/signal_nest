"""add scout admission deadline

D-25 blocked-Scout recovery candidate (PROPOSED, contract D-25-RECOVERY-CONTRACT-01 §3/§9.2;
``app.jobs.admission``): each admission's deadline is persisted with the admission.

* ``scout_admissions.deadline_at`` - the instant, in database time, after which an admission
  with no recorded end becomes ELIGIBLE for reconciliation (a procedural gate, not evidence
  that the external party stopped). Written once, by the admitting INSERT, from the same
  ``clock_timestamp()`` reading as ``admitted_at``.
* ``deadline_rule`` / ``deadline_client_seconds`` / ``deadline_lease_seconds`` - the basis the
  deadline was computed from (the admitting process's client deadline for the operation and
  its worker lease, combined as ``client_plus_lease``), so a deadline can be explained without
  reading today's settings.

**Existing rows are not back-filled.** They keep ``deadline_at = NULL`` and are marked
``deadline_rule = 'unrecorded'`` (the column default). A back-fill from present settings would
invent a historical configuration and could make an unresolved operation eligible earlier than
it was. An unresolved legacy row with no recorded end is never made eligible by time: the
reconcile command requires ``--acknowledge-unrecorded-deadline`` for it (and still refuses while
its attempt holds valid authority); one with a recorded unknown end is eligible as before.

PostgreSQL also gets two CHECKs: the rule is one of the two values, and a recorded deadline is
exactly ``admitted_at`` + its basis while an unrecorded one has neither. SQLite (development and
tests, where the ledger is never written) gets the columns only: adding a CHECK there would
rebuild the table. Rollback drops the CHECKs and the four columns and nothing else.

Revision ID: d4a8f2c6b9e1
Revises: c6f1d8e2a4b7
Create Date: 2026-10-30 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4a8f2c6b9e1"
down_revision: str | None = "c6f1d8e2a4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RULE_CHECK = "deadline_rule IN ('client_plus_lease', 'unrecorded')"
_BASIS_CHECK = (
    "(deadline_rule = 'unrecorded' AND deadline_at IS NULL"
    " AND deadline_client_seconds IS NULL AND deadline_lease_seconds IS NULL)"
    " OR (deadline_rule = 'client_plus_lease' AND deadline_at IS NOT NULL"
    " AND deadline_client_seconds > 0 AND deadline_lease_seconds > 0"
    " AND deadline_at = admitted_at"
    " + make_interval(secs => deadline_client_seconds + deadline_lease_seconds))"
)


def _postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    with op.batch_alter_table("scout_admissions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "deadline_rule",
                sa.String(length=24),
                server_default="unrecorded",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("deadline_client_seconds", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("deadline_lease_seconds", sa.Float(), nullable=True))
    if _postgresql():
        op.create_check_constraint(
            "ck_scout_admission_deadline_rule", "scout_admissions", _RULE_CHECK
        )
        op.create_check_constraint(
            "ck_scout_admission_deadline_basis", "scout_admissions", _BASIS_CHECK
        )


def downgrade() -> None:
    if _postgresql():
        op.drop_constraint("ck_scout_admission_deadline_basis", "scout_admissions", type_="check")
        op.drop_constraint("ck_scout_admission_deadline_rule", "scout_admissions", type_="check")
    with op.batch_alter_table("scout_admissions", schema=None) as batch_op:
        batch_op.drop_column("deadline_lease_seconds")
        batch_op.drop_column("deadline_client_seconds")
        batch_op.drop_column("deadline_rule")
        batch_op.drop_column("deadline_at")
