"""add scout execution controls

D-25 execution-control candidate (PROPOSED, contract D-25-EXECUTION-CONTROL-CONTRACT-01 §3/§8;
``app.jobs.admission``):

* ``scout_requests.owner_job_id`` / ``owner_attempt`` / ``owner_lease_token`` - the Scout's
  single current execution owner (O2), set by the committed claim's compare-and-set and
  cleared by the pause, the run's end, its stop or failure, or taken over once the recorded
  owner's authority has lapsed.
* ``scout_admissions`` - one row per external operation (a connector fetch attempt or a model
  attempt), committed before the operation is dispatched. ``resolved_at`` is set only by a
  known recorded end or by reconciliation. The partial unique index
  ``uq_scout_admission_unresolved`` allows at most ONE unresolved admission per Scout (K = 1).
  Counts, keys, states and timestamps only - no content; ``resolution_note`` is a reason code
  plus at most 200 characters (CHECK). Rows go with the tenant (CASCADE); pruning is a
  follow-up.

Additive: three nullable columns and one new table; no backfill (owners start NULL, the ledger
empty). Rollback drops the table, its index and the columns.

Revision ID: c6f1d8e2a4b7
Revises: b5e0c7a3d2f1
Create Date: 2026-10-30 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6f1d8e2a4b7"
down_revision: str | None = "b5e0c7a3d2f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scout_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_job_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("owner_attempt", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("owner_lease_token", sa.String(length=64), nullable=True))
    op.create_table(
        "scout_admissions",
        sa.Column("scout_request_id", sa.String(length=32), nullable=False),
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("attempt_key", sa.String(length=200), nullable=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_kind", sa.String(length=16), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=200), nullable=True),
        sa.Column("resolution_note", sa.String(length=240), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("operation IN ('fetch', 'model')", name="ck_scout_admission_operation"),
        sa.CheckConstraint(
            "end_kind IS NULL OR end_kind IN ('known', 'unknown')", name="ck_scout_admission_end_kind"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scout_request_id"], ["scout_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scout_admissions_job_id", "scout_admissions", ["job_id"])
    op.create_index(
        "uq_scout_admission_unresolved",
        "scout_admissions",
        ["scout_request_id"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
        sqlite_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_scout_admission_unresolved", table_name="scout_admissions")
    op.drop_index("ix_scout_admissions_job_id", table_name="scout_admissions")
    op.drop_table("scout_admissions")
    with op.batch_alter_table("scout_requests", schema=None) as batch_op:
        batch_op.drop_column("owner_lease_token")
        batch_op.drop_column("owner_attempt")
        batch_op.drop_column("owner_job_id")
