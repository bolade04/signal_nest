"""add signal intelligence records

Adds the additive ``signal_intelligence_records`` table (Phase 3B Batch 4A). One
row is the durable, scoped, immutable, version-aware form of a Batch 3
``OpportunityCandidate``: the deterministic facts/inference/relevance/score that
previously lived only in ``NormalizedSignal.ingest_metadata["intelligence"]``.

Purely **additive and safe on a live database**:

* It creates one brand-new table, its indexes and one unique constraint. No
  existing table is altered or dropped, so all business, signal, opportunity and
  durable-job data are preserved untouched.
* No backfill is fabricated: pre-existing normalized signals simply have no
  intelligence row until they are (re)scored after this migration. Absence of a
  row is a valid state.
* The unique constraint ``uq_signal_intelligence_identity``
  ``(workspace_id, normalized_signal_id, analysis_version, scoring_version,
  fingerprint)`` is the final concurrency guard for the idempotent insert path;
  it never blends rows across workspaces.
* The table stores only the already-sanitized excerpt and derived analysis (never
  credentials, tokens, raw untrusted text beyond the sanitized excerpt, or job
  payloads) and is not exposed by any customer API in this batch.

Rollback: ``downgrade`` drops the table and its indexes/constraint. Because the
Batch 3 advisory annotation still rides ``ingest_metadata``, dropping this table
loses no other data and ingestion continues unchanged.

Revision ID: 0155a5c468e3
Revises: a1b2c3d4e5f6
Create Date: 2026-07-15 12:06:49.120313
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0155a5c468e3'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('signal_intelligence_records',
    sa.Column('organization_id', sa.String(length=32), nullable=False),
    sa.Column('workspace_id', sa.String(length=32), nullable=False),
    sa.Column('scout_request_id', sa.String(length=32), nullable=False),
    sa.Column('normalized_signal_id', sa.String(length=32), nullable=False),
    sa.Column('opportunity_id', sa.String(length=32), nullable=True),
    sa.Column('location_id', sa.String(length=32), nullable=True),
    sa.Column('analysis_version', sa.String(length=20), nullable=False),
    sa.Column('scoring_version', sa.String(length=20), nullable=False),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('enricher', sa.String(length=40), nullable=False),
    sa.Column('accepted', sa.Boolean(), nullable=False),
    sa.Column('classification', sa.String(length=40), nullable=False),
    sa.Column('decision', sa.String(length=40), nullable=True),
    sa.Column('rejection_reason', sa.String(length=40), nullable=True),
    sa.Column('cluster_key', sa.String(length=120), nullable=False),
    sa.Column('score_total', sa.Integer(), nullable=False),
    sa.Column('evidence_count', sa.Integer(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('is_simulated', sa.Boolean(), nullable=False),
    sa.Column('facts', sa.JSON(), nullable=False),
    sa.Column('inference', sa.JSON(), nullable=False),
    sa.Column('relevance', sa.JSON(), nullable=False),
    sa.Column('score_components', sa.JSON(), nullable=False),
    sa.Column('provenance', sa.JSON(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['location_id'], ['business_locations.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['normalized_signal_id'], ['normalized_signals.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['scout_request_id'], ['scout_requests.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'normalized_signal_id', 'analysis_version', 'scoring_version', 'fingerprint', name='uq_signal_intelligence_identity')
    )
    with op.batch_alter_table('signal_intelligence_records', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_signal_intelligence_records_fingerprint'), ['fingerprint'], unique=False)
        batch_op.create_index(batch_op.f('ix_signal_intelligence_records_location_id'), ['location_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_signal_intelligence_records_normalized_signal_id'), ['normalized_signal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_signal_intelligence_records_opportunity_id'), ['opportunity_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_signal_intelligence_records_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_signal_intelligence_records_scout_request_id'), ['scout_request_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_signal_intelligence_records_workspace_id'), ['workspace_id'], unique=False)

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('signal_intelligence_records', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_signal_intelligence_records_workspace_id'))
        batch_op.drop_index(batch_op.f('ix_signal_intelligence_records_scout_request_id'))
        batch_op.drop_index(batch_op.f('ix_signal_intelligence_records_organization_id'))
        batch_op.drop_index(batch_op.f('ix_signal_intelligence_records_opportunity_id'))
        batch_op.drop_index(batch_op.f('ix_signal_intelligence_records_normalized_signal_id'))
        batch_op.drop_index(batch_op.f('ix_signal_intelligence_records_location_id'))
        batch_op.drop_index(batch_op.f('ix_signal_intelligence_records_fingerprint'))

    op.drop_table('signal_intelligence_records')
    # ### end Alembic commands ###
