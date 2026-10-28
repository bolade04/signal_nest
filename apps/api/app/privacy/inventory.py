"""The machine-checked data inventory (``P6-PRIV-1``).

``docs/operations/data-inventory.md`` is the human-readable inventory; this module is its
executable counterpart. Every table registered on ``Base.metadata`` has exactly one
:class:`TableEntry` here, and ``test_privacy_inventory.py`` fails when a table is added
without one. The entries decide three things and nothing else:

* the **category** of data the table holds, which the documentation explains;
* whether, and how, the table appears in an **organization export** (which rows belong
  to the organization, and which columns are never exported);
* which **retention class** purges it (``None``: it lives as long as its parent).

Deletion does not read this module for *what* to delete: it follows the declared foreign
keys (:mod:`app.privacy.erasure`). Verification reads neither: it reflects the live
schema (:mod:`app.privacy.verification`). Three independent views of the same tables, so
one of them being wrong cannot silently agree with itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RetentionClass(StrEnum):
    """The data classes with a time-based purge. Each has one duration setting.

    The setting is ``retention_<value>_days`` on :class:`~app.core.config.Settings`.
    """

    AUTH_SESSIONS = "auth_sessions"
    ACCOUNT_TOKENS = "account_tokens"
    INVITATIONS = "invitations"
    SIGNAL_EVIDENCE = "signal_evidence"
    OPPORTUNITIES = "opportunities"
    JOB_HISTORY = "job_history"
    AUDIT_LOGS = "audit_logs"

    @property
    def setting_name(self) -> str:
        return f"retention_{self.value}_days"


#: Classes whose rows belong to a workspace: purged by the per-workspace
#: ``retention.purge`` durable job, inside that job's tenant scope.
WORKSPACE_CLASSES: frozenset[RetentionClass] = frozenset(
    {RetentionClass.SIGNAL_EVIDENCE, RetentionClass.OPPORTUNITIES, RetentionClass.JOB_HISTORY}
)
#: Classes with no workspace (account credentials, organization invitations, audit
#: records): purged by the sweep itself in bounded, separately committed batches,
#: because a durable job row requires an organization and a workspace.
PLATFORM_CLASSES: frozenset[RetentionClass] = frozenset(RetentionClass) - WORKSPACE_CLASSES


class Category(StrEnum):
    ACCOUNT = "account"  # an account holder's identity or membership
    CREDENTIAL = "credential"  # sessions and one-time tokens of an account
    COMPLIANCE = "compliance"  # audit records
    TENANT_CONTENT = "tenant_content"  # customer-provided business configuration
    COLLECTED = "collected"  # collected third-party content and what is derived from it
    OPERATIONAL = "operational"  # job bookkeeping
    SYSTEM = "system"  # no personal or tenant data


@dataclass(frozen=True)
class TableEntry:
    table: str
    category: Category
    #: Whether the organization export includes the table.
    exported: bool = True
    #: Columns never exported: credentials, token digests and derived vectors.
    excluded_columns: tuple[str, ...] = ()
    #: The retention class whose purge removes the table's rows as a root (dependents
    #: of a purged root are removed with it whatever their own class).
    retention_class: RetentionClass | None = None


_TENANT = Category.TENANT_CONTENT
_COLLECTED = Category.COLLECTED

TABLES: dict[str, TableEntry] = {
    e.table: e
    for e in (
        TableEntry("users", Category.ACCOUNT, exported=False),
        TableEntry("organizations", Category.ACCOUNT),
        TableEntry("organization_members", Category.ACCOUNT),
        TableEntry(
            "organization_invitations",
            Category.ACCOUNT,
            excluded_columns=("token_hash",),
            retention_class=RetentionClass.INVITATIONS,
        ),
        TableEntry(
            "auth_sessions",
            Category.CREDENTIAL,
            exported=False,
            retention_class=RetentionClass.AUTH_SESSIONS,
        ),
        TableEntry(
            "password_reset_tokens",
            Category.CREDENTIAL,
            exported=False,
            retention_class=RetentionClass.ACCOUNT_TOKENS,
        ),
        TableEntry(
            "email_verification_tokens",
            Category.CREDENTIAL,
            exported=False,
            retention_class=RetentionClass.ACCOUNT_TOKENS,
        ),
        TableEntry("audit_logs", Category.COMPLIANCE, retention_class=RetentionClass.AUDIT_LOGS),
        TableEntry("workspaces", _TENANT),
        TableEntry("workspace_capability_overrides", _TENANT),
        TableEntry("brands", _TENANT),
        TableEntry("business_profiles", _TENANT),
        TableEntry("business_locations", _TENANT),
        TableEntry("geo_coverage_rules", _TENANT),
        TableEntry("product_profiles", _TENANT),
        TableEntry("audience_profiles", _TENANT),
        TableEntry("competitor_profiles", _TENANT),
        TableEntry("brand_voice_profiles", _TENANT),
        TableEntry("offer_calendar", _TENANT),
        TableEntry("claims_library", _TENANT),
        TableEntry("source_preferences", _TENANT),
        TableEntry("channel_preferences", _TENANT),
        TableEntry("campaigns", _TENANT),
        TableEntry("scout_requests", _TENANT),
        TableEntry("scout_schedules", _TENANT),
        TableEntry("raw_signals", _COLLECTED, retention_class=RetentionClass.SIGNAL_EVIDENCE),
        TableEntry("normalized_signals", _COLLECTED, excluded_columns=("embedding",)),
        TableEntry("signal_location_evidence", _COLLECTED),
        TableEntry(
            "signal_clusters",
            _COLLECTED,
            excluded_columns=("centroid",),
            retention_class=RetentionClass.SIGNAL_EVIDENCE,
        ),
        TableEntry("signal_intelligence_records", _COLLECTED),
        TableEntry("opportunities", _COLLECTED, retention_class=RetentionClass.OPPORTUNITIES),
        TableEntry("opportunity_scores", _COLLECTED),
        TableEntry("validation_evidence", _COLLECTED),
        TableEntry("opportunity_feedback", _COLLECTED),
        TableEntry(
            "jobs",
            Category.OPERATIONAL,
            exported=False,
            retention_class=RetentionClass.JOB_HISTORY,
        ),
        TableEntry("job_events", Category.OPERATIONAL, exported=False),
        # The LLM usage ledger (P6-LLM-4): tenant-linked usage metadata, one row per
        # provider attempt. Operator decisions of 2026-10-27 (D-9a/D-9b/D-9c, packet
        # D9-DECISIONS-01): OPERATIONAL; included in the organization export with every
        # recorded usage/accounting column, scoped by ``organization_id`` (it has no user
        # key, so it has no supported relationship to an account and never appears in the
        # account export); no time-based retention class: kept for the life of its
        # workspace for usage accounting and reconciliation during the unpaid pilot, deleted
        # with its workspace/organization, to be revisited before paid launch.
        TableEntry("llm_usage_events", Category.OPERATIONAL, exported=True),
        TableEntry("worker_registrations", Category.SYSTEM, exported=False),
    )
}


__all__ = [
    "PLATFORM_CLASSES",
    "TABLES",
    "WORKSPACE_CLASSES",
    "Category",
    "RetentionClass",
    "TableEntry",
]
