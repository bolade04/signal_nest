"""add worker registrations

Adds the additive ``worker_registrations`` table for the worker-fleet registry.
One row per worker process records its identity, lifecycle status, capacity and
last heartbeat so operators can see which workers are alive, busy, draining or
stale.

Purely **additive and safe on a live database**:

* It creates one brand-new table and its indexes. No existing table is altered,
  so all business data and all durable-job data are preserved untouched.
* No production worker row is seeded or auto-created; the table starts empty and
  is populated only when a worker process registers itself at startup.
* The table holds only operational fleet state (never credentials, environment
  variables, IP addresses, lease tokens or job payloads), so it is safe to drop.

Rollback: ``downgrade`` drops the table and its indexes. Because it carries only
transient fleet state, dropping it loses no business or job data; workers simply
re-register on the next startup after a re-upgrade.

Revision ID: d4f6a8c0b2e1
Revises: c3e5a7b9d1f2
Create Date: 2026-07-13 05:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4f6a8c0b2e1'
down_revision: str | None = 'c3e5a7b9d1f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'worker_registrations',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('worker_id', sa.String(length=128), nullable=False),
        sa.Column('worker_type', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('stopped_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('concurrency', sa.Integer(), nullable=False),
        sa.Column('supported_job_types', sa.JSON(), nullable=False),
        sa.Column('queue_backend', sa.String(length=16), nullable=False),
        sa.Column('application_version', sa.String(length=32), nullable=False),
        sa.Column('build_revision', sa.String(length=64), nullable=True),
        sa.Column('host_fingerprint', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('worker_id', name='uq_worker_registrations_worker_id'),
    )
    op.create_index(
        'ix_worker_registrations_status', 'worker_registrations', ['status'], unique=False
    )
    op.create_index(
        'ix_worker_registrations_heartbeat',
        'worker_registrations',
        ['last_heartbeat_at'],
        unique=False,
    )
    op.create_index(
        'ix_worker_registrations_type', 'worker_registrations', ['worker_type'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_worker_registrations_type', table_name='worker_registrations')
    op.drop_index('ix_worker_registrations_heartbeat', table_name='worker_registrations')
    op.drop_index('ix_worker_registrations_status', table_name='worker_registrations')
    op.drop_table('worker_registrations')
