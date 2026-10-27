"""add llm budget reservation ledger

Adds the two tables behind the per-workspace monthly LLM budget's hard ceiling on
PostgreSQL (D-LLM-1 option B, ``app.llm.budget``):

* ``llm_budget_periods`` - one row per workspace and UTC calendar month (unique
  ``workspace_id, period_start``; surrogate ``id`` like every other table, for the
  privacy export, erasure walk and verification): ``reserved_usd`` (the budget
  consumed: the sum of each call's worst-case bound) and ``settled_usd`` and
  ``bound_violations`` (settled calls whose reported usage exceeded their bound). The budget check is one conditional ``UPDATE`` of this row.
* ``llm_budget_reservations`` - one row per reserved provider call, keyed by attempt
  (``attempt_key`` unique: job id, worker attempt, call ordinal) so settlement is
  idempotent and a reservation whose process died can be recovered; ``state``
  (reserved | settled | released | expired), ``expires_at`` for the sweep,

Purely **additive and safe on a live database**:

* Two brand-new tables and their indexes. No existing table is altered.
* Money as ``NUMERIC(12, 6)`` (exact sums on PostgreSQL), never a float.
* No backfill: spend before this revision stays in ``llm_usage_events``, which the
  budget check still reads (the larger of the two counts).
* Counts, money and keys only - no prompt, scraped content or model text.
  ``CASCADE`` on organization and workspace ties retention to tenant deletion;
  ``SET NULL`` on ``job_id`` keeps the reservation record when a job row is deleted.

Rollback: ``downgrade`` drops the indexes and both tables, and with them the
reserved amounts of calls in flight; the budget then falls back to the usage rows.

Revision ID: 8005724d811a
Revises: 47028898175f
Create Date: 2026-10-27 10:10:11.472389
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8005724d811a'
down_revision: str | None = '47028898175f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('llm_budget_periods',
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('organization_id', sa.String(length=32), nullable=False),
    sa.Column('reserved_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('settled_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('bound_violations', sa.Integer(), server_default='0', nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'period_start', name='uq_llm_budget_periods_workspace_period')
    )
    with op.batch_alter_table('llm_budget_periods', schema=None) as batch_op:
        batch_op.create_index('ix_llm_budget_periods_organization_id', ['organization_id'], unique=False)

    op.create_table('llm_budget_reservations',
    sa.Column('organization_id', sa.String(length=32), nullable=False),
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=True),
    sa.Column('attempt_key', sa.String(length=96), nullable=False),
    sa.Column('reserved_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('settled_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('usage_recorded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('attempt_key')
    )
    with op.batch_alter_table('llm_budget_reservations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_budget_reservations_job_id'), ['job_id'], unique=False)
        batch_op.create_index('ix_llm_budget_reservations_state_expires', ['state', 'expires_at'], unique=False)
        batch_op.create_index('ix_llm_budget_reservations_workspace_period', ['workspace_id', 'period_start'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('llm_budget_reservations', schema=None) as batch_op:
        batch_op.drop_index('ix_llm_budget_reservations_workspace_period')
        batch_op.drop_index('ix_llm_budget_reservations_state_expires')
        batch_op.drop_index(batch_op.f('ix_llm_budget_reservations_job_id'))

    op.drop_table('llm_budget_reservations')

    with op.batch_alter_table('llm_budget_periods', schema=None) as batch_op:
        batch_op.drop_index('ix_llm_budget_periods_organization_id')

    op.drop_table('llm_budget_periods')
