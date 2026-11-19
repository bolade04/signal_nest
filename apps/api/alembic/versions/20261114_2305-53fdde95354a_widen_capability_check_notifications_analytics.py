"""widen the capability-override CHECK for notifications_analytics (Project Phase 5E, O-12(B))

Operator ruling O-12(B) of 2026-11-12: "Choose O-12(B): one additional flag for the notifications
and analytics surfaces, off by default. Explicitly amend O-4’s three-flag limit. Gate the backend
routes, customer UI and applicable notification emission consistently." 5E-CF adds
``Capability.NOTIFICATIONS_ANALYTICS`` (``notifications_analytics``), bound to
``notifications_analytics_enabled`` (default ``False``). The capability is enableable and
disableable per workspace, so the override plane must be able to store a row naming it.

**Covered by the operator's narrow CF-3 migration-freeze exception of 2026-11-15 (question Q-1).**
The CF-3 migration freeze admits item-specific exceptions only; this one covers this one CHECK
widening and grants no other schema change, no staging or production migration and no activation.
The option the operator chose, verbatim (one line, so it is byte-equal to the record):
"A migration-freeze exception for this one CHECK widening (53fdde95354a), inside the 5E PR. It grants no other schema change, no staging/production migration and no activation. The flag works like the other Phase 5 flags: off by default, per-workspace override, safety ceiling, no raw reads."

* ``workspace_capability_overrides`` - ``ck_workspace_capability_override_capability`` admits
  ``notifications_analytics``. Nothing else changes: no table, column, index, row or other
  constraint. The model's ``CheckConstraint`` derives from
  :func:`app.capabilities.registry.persisted_values`; this revision brings the migrated
  database's CHECK to the same set, which ``test_cf1_contract_migration``,
  ``test_cf2_contract_migration`` and ``test_cf3_contract_migration`` compare in both
  directions. CHECK literals are frozen with this revision.

Without it, an ``alembic upgrade head`` database refuses any override naming the capability
(the CHECK fails), while ``alembic check`` reports no drift (autogenerate does not compare CHECK
constraints).

Rollback: the downgrade **refuses before any DDL** while an override names
``notifications_analytics`` (clearing it is an operator decision, not a downgrade's), then
narrows the CHECK back to the CF-3 set. The count is a Core expression.

Revision ID: 53fdde95354a
Revises: 1574637e56d3
Create Date: 2026-11-14 23:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "53fdde95354a"
down_revision: str | None = "1574637e56d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OVERRIDES = "workspace_capability_overrides"
_CAPABILITY_CHECK = "ck_workspace_capability_override_capability"
_CAPABILITY = "notifications_analytics"
_CAPABILITY_IN_WIDE = (
    "capability IN ('connector_rss', 'content_export', 'creative_generation',"
    " 'notifications_analytics', 'opportunity_feedback', 'recommendation_briefs',"
    " 'scout_scheduling')"
)
#: The CF-3 set (``c3f8e1a6d2b9``), restored by the downgrade.
_CAPABILITY_IN_NARROW = (
    "capability IN ('connector_rss', 'content_export', 'creative_generation',"
    " 'opportunity_feedback', 'recommendation_briefs', 'scout_scheduling')"
)


def upgrade() -> None:
    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_WIDE))


def downgrade() -> None:
    # Refuse before any DDL (a Core count; no SQL text is assembled from strings).
    overrides = sa.table(_OVERRIDES, sa.column("capability"))
    named = (
        op.get_bind()
        .execute(
            sa.select(sa.func.count())
            .select_from(overrides)
            .where(overrides.c.capability == _CAPABILITY)
        )
        .scalar_one()
    )
    if named:
        raise RuntimeError(
            f"{named} workspace capability override(s) name '{_CAPABILITY}'; clear them before "
            "downgrading below 53fdde95354a. Nothing was changed."
        )
    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_NARROW))
