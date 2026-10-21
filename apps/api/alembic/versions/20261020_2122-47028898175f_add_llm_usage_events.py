"""add llm usage events

Adds ``llm_usage_events`` (P6-LLM-4): one row per LLM provider attempt made by a
scout run - task, provider, model, token counts, estimated cost in USD and outcome.
It is the ledger behind per-workspace monthly budgets (month-to-date spend is the
sum of a workspace's rows since the first of the month, UTC).

Purely **additive and safe on a live database**:

* One brand-new table and two indexes (``scout_request_id``; ``workspace_id,
  created_at`` for the month-to-date sum). No existing table is altered.
* No backfill: spend before this revision was never recorded, so every workspace
  starts the month at zero recorded spend.
* No prompt, scraped content or model text is stored - counts and money only.
  ``CASCADE`` on organization and workspace ties retention to tenant deletion;
  ``SET NULL`` on ``scout_request_id`` keeps the spend when a request is deleted.

Rollback: ``downgrade`` drops the indexes and the table, and with them the recorded
spend; budgets then restart from zero.

Revision ID: 47028898175f
Revises: 6ad2ff77f691
Create Date: 2026-10-20 21:22:08.651756
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '47028898175f'
down_revision: str | None = '6ad2ff77f691'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('llm_usage_events',
    sa.Column('organization_id', sa.String(length=32), nullable=False),
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('scout_request_id', sa.String(length=32), nullable=True),
    sa.Column('task', sa.String(length=64), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=False),
    sa.Column('outcome', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['scout_request_id'], ['scout_requests.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('llm_usage_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_usage_events_scout_request_id'), ['scout_request_id'], unique=False)
        batch_op.create_index('ix_llm_usage_events_workspace_created', ['workspace_id', 'created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('llm_usage_events', schema=None) as batch_op:
        batch_op.drop_index('ix_llm_usage_events_workspace_created')
        batch_op.drop_index(batch_op.f('ix_llm_usage_events_scout_request_id'))

    op.drop_table('llm_usage_events')
