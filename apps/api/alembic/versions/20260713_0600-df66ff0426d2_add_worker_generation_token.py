"""add worker generation token

Adds a single additive, nullable ``generation_token`` column to
``worker_registrations``. A fresh token is minted whenever a worker process
registers (a restart), and every later heartbeat or status transition presents
the token it captured at registration. A stale process whose token no longer
matches the row is fenced out, so it cannot keep alive or transition the
registration that replaced it.

Purely **additive and safe on a live database**:

* One new nullable column is added; no existing column is altered or dropped, so
  all existing fleet rows and all other data are preserved untouched.
* The column starts NULL for any pre-existing row; such rows are simply
  re-stamped with a fresh token the next time that worker registers.
* The token is an opaque fencing value, never a credential, and is never exposed
  by any operator or customer response.

Rollback: ``downgrade`` drops only the new column. No other schema or data is
touched, and workers continue to operate (unfenced) on a re-downgrade until the
column is re-added.

Revision ID: df66ff0426d2
Revises: d4f6a8c0b2e1
Create Date: 2026-07-13 06:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'df66ff0426d2'
down_revision: str | None = 'd4f6a8c0b2e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'worker_registrations',
        sa.Column('generation_token', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('worker_registrations', 'generation_token')
