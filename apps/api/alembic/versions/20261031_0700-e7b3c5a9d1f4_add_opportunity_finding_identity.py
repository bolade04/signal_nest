"""add opportunity finding identity

W16 customer-workflow repairs (PROPOSED, contract W16-CUSTOMER-REPAIR-CONTRACT-01 §A/§I.1): one
current card per finding across a Scout's repeated runs.

* ``opportunities.finding_key`` - the finding's identity within its Scout: ``f2:`` + SHA-256 over
  the sorted ``(source_type, source identity, content_hash, market)`` of the card's member evidence
  items, computed from their stored rows. NULL on every card that exists before this revision.
* ``uq_opportunities_finding`` - unique ``(organization_id, workspace_id, scout_request_id,
  finding_key)`` where the key is set, on PostgreSQL and SQLite: two keyed cards of one finding
  cannot exist; NULL (a card created before the identity) never conflicts.
* ``opportunity_observations`` - one INSERT-only row per sighting of a finding by a run
  (``first_seen`` or ``seen_again``, the run's job and cluster); ids, a kind and instants only.

**Nothing is back-filled.** Existing cards keep ``finding_key = NULL`` and no sighting is invented;
a later run may adopt the oldest identity-less card whose own stored member rows give its key
(application code, at run time, one card at a time). Rollback drops the table, the index and the
column and nothing else.

Revision ID: e7b3c5a9d1f4
Revises: d4a8f2c6b9e1
Create Date: 2026-10-31 07:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7b3c5a9d1f4"
down_revision: str | None = "d4a8f2c6b9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBS = "opportunity_observations"


def upgrade() -> None:
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("finding_key", sa.String(length=72), nullable=True))
    op.create_index(
        "uq_opportunities_finding",
        "opportunities",
        ["organization_id", "workspace_id", "scout_request_id", "finding_key"],
        unique=True,
        postgresql_where=sa.text("finding_key IS NOT NULL"),
        sqlite_where=sa.text("finding_key IS NOT NULL"),
    )
    op.create_table(
        _OBS,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("scout_request_id", sa.String(length=32), nullable=False),
        sa.Column("opportunity_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("cluster_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('first_seen', 'seen_again')", name="ck_opportunity_observation_kind"
        ),
        sa.ForeignKeyConstraint(["cluster_id"], ["signal_clusters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["scout_request_id"], ["scout_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "organization_id",
        "workspace_id",
        "scout_request_id",
        "opportunity_id",
        "job_id",
        "cluster_id",
    ):
        op.create_index(f"ix_{_OBS}_{column}", _OBS, [column])


def downgrade() -> None:
    for column in (
        "cluster_id",
        "job_id",
        "opportunity_id",
        "scout_request_id",
        "workspace_id",
        "organization_id",
    ):
        op.drop_index(f"ix_{_OBS}_{column}", table_name=_OBS)
    op.drop_table(_OBS)
    # The partial unique index goes before the batch: on SQLite the batch rebuilds the table
    # and must not carry a reflected partial index through it.
    op.drop_index("uq_opportunities_finding", table_name="opportunities")
    with op.batch_alter_table("opportunities", schema=None) as batch_op:
        batch_op.drop_column("finding_key")
