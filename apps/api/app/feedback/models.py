"""Persistence model for customer opportunity feedback (Phase 3C, 3C-B).

This is the durable, dark-deployed foundation for the human feedback loop. A
:class:`OpportunityFeedback` row is an **append-only, immutable** record of one
customer's binary judgement about a scored opportunity, captured against the exact
immutable intelligence record that judgement was made on:

* **Capture-only** — persisting feedback never influences scoring, ranking, model
  training, prompts, or any cross-workspace / cross-market signal. The whole
  subsystem ships behind ``opportunity_feedback_enabled`` (default off).
* **Immutable / append-only** — feedback is never edited in place or overwritten.
  A change of mind is a *new* row; the history is the audit trail. (The inherited
  ``updated_at`` column is unused for mutation here — nothing ever updates a row.)
* **Version-attributable** — the feedback points at the specific immutable
  :class:`~app.intelligence.records.SignalIntelligenceRecord` via a direct FK, and
  additionally snapshots that record's ``analysis_version`` / ``scoring_version`` /
  ``fingerprint`` as provenance metadata. Those version strings are *copied from the
  target record at capture time* — they are never the primary identity and are never
  caller-supplied, so feedback can always be interpreted against the exact analysis
  it was given on even if newer records are later written.
* **Isolation-preserving** — typed FK scope columns
  (organization/workspace/opportunity/intelligence-record) keep every row bound to
  exactly one tenant scope; retention follows the workspace deletion lifecycle
  (``CASCADE``), with no TTL and no purge worker.

Types are portable across SQLite (local) and PostgreSQL (full): ``String`` UUIDs,
``Boolean``, ``String`` enum codes (never native enum types), and a portable
``CheckConstraint`` for reason polarity.
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import NEGATIVE_FEEDBACK_REASONS, POSITIVE_FEEDBACK_REASONS
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _quote_reasons(reasons: frozenset) -> str:
    """Render a reason set as a deterministic SQL ``IN`` value list."""
    return ", ".join(f"'{code.value}'" for code in sorted(reasons, key=lambda c: c.value))


#: Portable polarity guard: a reason code, when present, must match the feedback's
#: usefulness. Positive codes only with useful feedback, negative codes only with
#: not-useful feedback; a NULL reason is always allowed. Written as a plain boolean
#: expression so it holds identically on SQLite and PostgreSQL (no native enums).
_REASON_POLARITY_SQL = (
    "reason_code IS NULL"
    f" OR (is_useful AND reason_code IN ({_quote_reasons(POSITIVE_FEEDBACK_REASONS)}))"
    f" OR (NOT is_useful AND reason_code IN ({_quote_reasons(NEGATIVE_FEEDBACK_REASONS)}))"
)


class OpportunityFeedback(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Append-only, immutable record of one customer judgement on an opportunity."""

    __tablename__ = "opportunity_feedback"
    __table_args__ = (
        CheckConstraint(
            _REASON_POLARITY_SQL,
            name="ck_opportunity_feedback_reason_polarity",
        ),
    )

    # --- Scope (isolation-preserving typed FKs) ------------------------------
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # The exact immutable intelligence record this judgement was made against.
    intelligence_record_id: Mapped[str] = mapped_column(
        ForeignKey("signal_intelligence_records.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Actor attribution. SET NULL (not CASCADE) so deleting a user never destroys the
    # immutable feedback history — it only forgets who authored it.
    submitted_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # --- Judgement -----------------------------------------------------------
    #: Required binary judgement: True = useful, False = not useful.
    is_useful: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Optional structured reason from the closed ``FeedbackReason`` vocabulary.
    #: There is no free-text alternative by design. Polarity is DB-enforced.
    reason_code: Mapped[str | None] = mapped_column(String(40))

    # --- Provenance snapshot (copied from the target record; never caller-supplied) --
    analysis_version: Mapped[str] = mapped_column(String(20), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(20), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
