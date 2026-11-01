"""Project Phase 5 CF-3 (W21): the 5D + 5E contracts, without the migration.

* **Nothing activates.** ``content_export_enabled`` ships ``False``, is bound to
  ``Capability.CONTENT_EXPORT`` (workspace-enableable, O-3, ``future_activation_phase="5E"``)
  and is reflected in ``FeatureFlagsOut`` (O-4). No route exists for any CF-3 table: the
  contract keeps its operation count, and no new path names a 5D/5E surface.
* **Vocabularies.** ``CampaignStatus``, ``NotificationEventCode`` (each code names its subject
  table, read-only) and ``ExportFormat``; the jurisdiction grammar (``UNRESOLVED`` is never a
  code; the Pydantic type refuses it).
* **Models.** ``exports`` is append-only with FK-less scope (O-6) and a NOT NULL jurisdiction;
  ``notifications`` / ``notification_preferences`` are per-user and cascade; ``campaign_locations``
  carries the two workspace-consistency keys; the 5D/5E additive columns exist.
* **Policies.** The export, export-history and analytics role sets are exact, and the
  dependencies admit exactly them (rank plays no part).
* **The notification emission interface** is a Protocol with the frozen keyword signature.
* **Campaign hardening is live:** an out-of-vocabulary ``mode`` or ``status`` is a 422 at the
  schema layer (create and update), never a database error; valid values still work.
* **The analytics fixture module** is registered by the root ``conftest.py`` and empty.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Iterator
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from app.analytics.policy import ANALYTICS_READER_ROLES, require_analytics_reader
from app.campaign_context.models import Campaign, CampaignLocation
from app.capabilities.registry import Capability, get_policy, persisted_values
from app.core.config import Settings
from app.core.enums import (
    NOTIFICATION_SUBJECT_TABLES,
    CampaignStatus,
    ExportFormat,
    NotificationEventCode,
    Role,
)
from app.core.errors import PermissionDeniedError
from app.db.append_only import is_append_only
from app.db.models import Base
from app.exports.models import Export
from app.exports.policy import (
    EXPORT_HISTORY_READER_ROLES,
    EXPORTER_ROLES,
    require_export_history_reader,
    require_exporter,
)
from app.jurisdiction.codes import (
    UNRESOLVED,
    JurisdictionCode,
    is_jurisdiction_code,
)
from app.main import app
from app.notifications.interface import NotificationEmitter
from app.notifications.models import Notification, NotificationPreference
from app.privacy.schemas import AccountExportOut
from app.tests._privacy_support import ALICE, API, ORG_A, WS_A, PrivacyEnv, privacy_world


# --------------------------------------------------------------------------- #
# Nothing activates
# --------------------------------------------------------------------------- #
def test_the_flag_ships_dark_and_is_reflected() -> None:
    """The CF-3 flag-default mutation-check target."""
    assert Settings(_env_file=None).content_export_enabled is False
    assert Settings.model_fields["content_export_enabled"].default is False
    assert app.openapi()["components"]["schemas"]["FeatureFlagsOut"]["required"].count(
        "content_export_enabled"
    ) == 1


def test_the_capability_is_workspace_enableable_and_bound_to_the_flag() -> None:
    policy = get_policy(Capability.CONTENT_EXPORT)
    assert policy.global_flag_attr == "content_export_enabled"
    assert policy.workspace_enableable and policy.workspace_disableable
    assert policy.subject_to_safety_ceiling and policy.requires_workspace_context
    assert policy.future_activation_phase == "5E"
    assert "content_export" in persisted_values()


def test_no_route_serves_a_cf3_surface() -> None:
    """CF-3 mounts nothing: the contract keeps its 127 operations, and no path names an
    export, notification, analytics, jurisdiction or campaign-membership surface."""
    paths = app.openapi()["paths"]
    operations = sum(
        1 for item in paths.values() for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    )
    assert operations == 127
    privacy_exports = ("/account/export", "/export", "/export/{table}")  # Phase 6 (P6-PRIV-1)
    for path in paths:
        for word in ("export", "notification", "analytics", "jurisdiction", "campaign-location"):
            if word == "export" and path.endswith(privacy_exports):
                continue
            assert word not in path, path


# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #
def test_the_three_core_vocabularies_are_exact() -> None:
    assert [s.value for s in CampaignStatus] == [
        "draft", "active", "paused", "completed", "archived"
    ]
    assert [e.value for e in NotificationEventCode] == [
        "brief.created", "approval.decided", "draft.generated", "review.blocked",
        "export.completed",
    ]
    assert [f.value for f in ExportFormat] == ["text/plain", "text/markdown", "json"]


def test_every_event_code_names_a_real_subject_table_and_the_map_is_read_only() -> None:
    assert isinstance(NOTIFICATION_SUBJECT_TABLES, MappingProxyType)
    assert set(NOTIFICATION_SUBJECT_TABLES) == set(NotificationEventCode)
    assert set(NOTIFICATION_SUBJECT_TABLES.values()) <= set(Base.metadata.tables)
    with pytest.raises(TypeError):
        NOTIFICATION_SUBJECT_TABLES[NotificationEventCode.BRIEF_CREATED] = "x"  # type: ignore[index]


@pytest.mark.parametrize("code", ["GB", "KE", "US-TX", "NG-LA", "GB-ENG", "CA-1"])
def test_a_jurisdiction_code_is_accepted(code: str) -> None:
    assert is_jurisdiction_code(code)
    assert TypeAdapter(JurisdictionCode).validate_python(code) == code


@pytest.mark.parametrize(
    "value", [UNRESOLVED, "us-tx", "Gb", "USA", "U", "US_TX", "US-", "US-ABCD", "", "US TX", None]
)
def test_anything_else_is_not_a_jurisdiction_code(value) -> None:
    assert not is_jurisdiction_code(value)
    with pytest.raises(ValidationError):
        TypeAdapter(JurisdictionCode).validate_python(value)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def _fk_rules(model) -> dict[str, str]:
    return {fk.parent.name: (fk.ondelete or "").upper() for fk in model.__table__.foreign_keys}


def test_exports_are_append_only_compliance_records() -> None:
    assert is_append_only(Export)
    columns = Export.__table__.c
    assert "updated_at" not in columns and "created_at" in columns
    assert columns.jurisdiction_code.nullable is False
    # O-6: FK-less scope; every live link is SET NULL so the record survives what it names.
    assert not columns.organization_id.foreign_keys and not columns.workspace_id.foreign_keys
    assert _fk_rules(Export) == {
        "draft_id": "SET NULL", "approval_id": "SET NULL", "exported_by": "SET NULL"
    }


def test_the_notification_tables_are_per_user_and_cascade() -> None:
    for model in (Notification, NotificationPreference):
        assert not is_append_only(model)  # read_at / enabled / deleted_at change
        assert _fk_rules(model) == {
            "organization_id": "CASCADE", "workspace_id": "CASCADE", "user_id": "CASCADE"
        }
        assert not model.__table__.c.user_id.nullable
    unique = {
        tuple(c.name for c in u.columns)
        for u in NotificationPreference.__table__.constraints
        if u.__class__.__name__ == "UniqueConstraint"
    }
    assert ("workspace_id", "user_id", "event_code") in unique


def test_the_membership_table_carries_the_workspace_consistency_keys() -> None:
    assert not is_append_only(CampaignLocation)
    composite = {
        (tuple(fk.column_keys), fk.referred_table.name, fk.ondelete)
        for fk in CampaignLocation.__table__.foreign_key_constraints
        if len(fk.column_keys) == 2
    }
    assert composite == {
        (("campaign_id", "workspace_id"), "campaigns", "CASCADE"),
        (("location_id", "workspace_id"), "business_locations", "CASCADE"),
    }
    targets = {
        (index.table.name, tuple(c.name for c in index.columns))
        for table in ("campaigns", "business_locations")
        for index in Base.metadata.tables[table].indexes
        if index.unique
    }
    assert {("campaigns", ("id", "workspace_id")),
            ("business_locations", ("id", "workspace_id"))} <= targets


def test_the_additive_columns_exist_and_are_nullable() -> None:
    tables = Base.metadata.tables
    for table in ("business_locations", "geo_coverage_rules", "recommendation_briefs",
                  "creative_drafts", "content_reviews"):
        assert tables[table].c.jurisdiction_code.nullable, table
    for table in ("recommendation_briefs", "creative_drafts", "content_reviews",
                  "notifications"):
        assert tables[table].c.deleted_at.nullable, table
    assert tables["claims_library"].c.jurisdiction_codes.nullable
    assert Campaign.__table__.c.status.default.arg == "active"


# --------------------------------------------------------------------------- #
# Policies: exact role sets on the Phase 6 primitive
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("dependency", "allowed"),
    [
        (require_exporter, {Role.OWNER, Role.ADMIN, Role.MARKETER}),
        (require_export_history_reader, {Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER}),
        (require_analytics_reader, {Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER}),
    ],
)
def test_each_policy_admits_exactly_its_roles(dependency, allowed) -> None:
    for role in Role:
        ctx = SimpleNamespace(role=role)
        if role in allowed:
            assert dependency(ctx=ctx) is ctx
        else:
            with pytest.raises((PermissionDeniedError, HTTPException)):
                dependency(ctx=ctx)


def test_the_role_sets_are_named_and_exact() -> None:
    assert EXPORTER_ROLES == frozenset({Role.OWNER, Role.ADMIN, Role.MARKETER})
    assert EXPORT_HISTORY_READER_ROLES == ANALYTICS_READER_ROLES == frozenset(
        {Role.OWNER, Role.ADMIN, Role.MARKETER, Role.REVIEWER}
    )


def test_the_emission_interface_signature_is_frozen() -> None:
    params = inspect.signature(NotificationEmitter.__call__).parameters
    assert list(params) == [
        "self", "db", "organization_id", "workspace_id", "recipient_user_ids", "event_code",
        "subject_id",
    ]
    assert all(
        params[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in list(params)[2:]
    )


def test_the_account_export_schema_carries_the_cf3_fields() -> None:
    assert {"exports_performed", "notifications_received", "notification_preferences"} <= set(
        AccountExportOut.model_fields
    )


def test_the_analytics_fixture_module_is_registered_and_empty() -> None:
    module = sys.modules.get("app.tests.fixtures.analytics")
    assert module is not None  # imported by the root conftest.py
    assert module.__all__ == []


# --------------------------------------------------------------------------- #
# Campaign hardening (live): the schema layer refuses out-of-vocabulary values
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "cf3-campaigns.db") as e:
        yield e


def test_campaign_mode_and_status_are_validated_at_the_schema_layer(env) -> None:
    url = f"{API}/workspaces/{WS_A}/campaigns"
    for body in ({"name": "x", "mode": "everywhere"}, {"name": "x", "status": "running"},
                 {"name": "x", "mode": "Grouped"}):
        r = env.client.post(url, headers=env.auth(ALICE), json=body)
        assert r.status_code == 422, (body, r.status_code, r.text)
    created = env.client.post(
        url, headers=env.auth(ALICE), json={"name": "Launch", "mode": "grouped", "status": "draft"}
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert (row["mode"], row["status"]) == ("grouped", "draft")
    default = env.client.post(url, headers=env.auth(ALICE), json={"name": "Defaults"})
    assert (default.json()["mode"], default.json()["status"]) == ("per_location", "active")
    patch = f"{url}/{row['id']}"
    bad = env.client.patch(patch, headers=env.auth(ALICE), json={"status": "live"})
    assert bad.status_code == 422, bad.text
    good = env.client.patch(patch, headers=env.auth(ALICE), json={"status": "paused"})
    assert good.status_code == 200 and good.json()["status"] == "paused"
    assert ORG_A == row["organization_id"]  # the response projection is 5D-W1's, unchanged
