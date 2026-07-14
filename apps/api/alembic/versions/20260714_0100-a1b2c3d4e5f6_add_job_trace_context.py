"""add job trace context

Adds a single additive, nullable ``trace_context`` column to ``jobs`` holding a
safe W3C ``traceparent`` string captured at enqueue. The worker restores it as
the remote parent of the job's execution span, so a durable job's work links back
to the request (or scheduler) that enqueued it across the enqueue -> claim ->
execute boundary.

Purely **additive and safe on a live database**:

* One new nullable column is added; no existing column is altered or dropped, so
  all existing jobs and all other data are preserved untouched.
* The column starts NULL for any pre-existing job. Trace propagation is
  best-effort and only meaningful for jobs created after this migration; a job
  with no persisted context simply starts a fresh root span. No backfill needed.
* The value is a bounded W3C ``traceparent`` (``00-<trace>-<span>-<flags>``,
  55 chars) — trace/span identifiers only, never a credential, tenant id, URL or
  payload, and never exposed by any customer API.

Rollback: ``downgrade`` drops only the new column. No other schema or data is
touched, and enqueue/execution continue to work (without persisted trace context)
on a re-downgrade until the column is re-added.

Revision ID: a1b2c3d4e5f6
Revises: e7c2a9b4f1d3
Create Date: 2026-07-14 01:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = 'e7c2a9b4f1d3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'jobs',
        sa.Column('trace_context', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('jobs', 'trace_context')
