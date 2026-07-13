"""add user is_operator

Adds a server-controlled platform-operator flag to users. Additive and
backward compatible: existing rows default to non-operator (server_default
false), so the column is safe to add on a live database with no data repair.

Revision ID: b2d4e6f8a1c3
Revises: 9a7c614699d8
Create Date: 2026-07-13 03:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2d4e6f8a1c3'
down_revision: str | None = '9a7c614699d8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'is_operator',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('is_operator')
