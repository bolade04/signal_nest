"""add job correlation id

Adds a single additive, nullable ``correlation_id`` column to ``jobs``. A fresh
opaque id is generated when a durable job is enqueued and is restored into the
worker's logging context while the job executes, so a job can be followed across
enqueue -> claim -> execute -> terminal purely from safe log correlation.

Purely **additive and safe on a live database**:

* One new nullable column is added; no existing column is altered or dropped, so
  all existing jobs and all other data are preserved untouched.
* The column starts NULL for any pre-existing job. Correlation is best-effort and
  only meaningful for jobs created after this migration, so no backfill is needed.
* The id is opaque and internal — distinct from the primary key, the lease token,
  the worker id and every tenant identifier — never a credential, and never
  exposed by any customer API.

Rollback: ``downgrade`` drops only the new column. No other schema or data is
touched, and enqueue continues to work (without persisted correlation) on a
re-downgrade until the column is re-added.

Revision ID: e7c2a9b4f1d3
Revises: df66ff0426d2
Create Date: 2026-07-14 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7c2a9b4f1d3'
down_revision: str | None = 'df66ff0426d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'jobs',
        sa.Column('correlation_id', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('jobs', 'correlation_id')
