"""Project Phase 5 CF-3 (W21): the consolidated 5D + 5E contracts-freeze migration ``c3f8e1a6d2b9``.

Through the real Alembic CLI on a throwaway SQLite database (as CI validates migrations), with
the plumbing of ``test_cf1_contract_migration``:

* this revision follows CF-2's ``a7d2e9f4c1b8`` and is followed by 5D-W1's campaign-location
  data repair ``d5f616c0ec97`` (data only; tested in ``test_campaign_locations_repair``) and
  then by the single head, D-N4 item 37's append-only backstop ``d0a8bce8de6a`` (triggers on
  PostgreSQL only; tested in ``test_append_only_backstop_postgres``), so the cases below
  upgrade to THIS revision explicitly;
  ``upgrade head`` then ``alembic check`` reports no drift (Phase 5 plan §6.2.0);
* **the migrated CHECKs equal their Python authorities, in both directions** — campaign mode
  and status (``CampaignMode`` / ``CampaignStatus``), export format (``ExportFormat``), the
  notification event code on both tables (``NotificationEventCode``) and the capability set
  (``persisted_values()``, now with ``content_export``); every jurisdiction CHECK equals
  ``jurisdiction_code_check_sql``. Discovery: every CHECK on the new tables and every CHECK
  CF-3 adds to an existing table must be one this module knows;
* the CHECKs hold on insert, and the **workspace-consistency keys** of ``campaign_locations``
  refuse a membership whose campaign or location belongs to another workspace (foreign keys
  on, as the application runs SQLite);
* **the O-8 back-fill** (a database at CF-2 with real campaigns, then ``upgrade``): only ids of
  a location of the campaign's own workspace are migrated (an inactive one too); a missing id,
  another workspace's id and a malformed list are dropped **and reported** in the migration
  output; the JSON arrays are untouched; running the back-fill again inserts nothing;
* **the campaign vocabularies** are enumerated and normalised before the CHECKs, and every
  rewrite is reported; an unknown status never becomes ``active``;
* the downgrade **refuses before any DDL** while an override names ``content_export``, while
  ``exports`` holds a row, while a brief / draft / review holds a captured jurisdiction, while
  any row is marked ``deleted_at`` or while a claims-library entry is jurisdiction-restricted;
  otherwise one step down removes exactly this revision (every other index and row kept) and
  ``upgrade`` restores it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.briefs.models import RecommendationBrief
from app.campaign_context.models import Campaign, CampaignLocation
from app.capabilities.registry import persisted_values
from app.core.enums import CampaignMode, CampaignStatus, ExportFormat, NotificationEventCode
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.jurisdiction.codes import jurisdiction_code_check_sql
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.notifications.models import Notification, NotificationPreference
from app.tests.test_cf1_contract_migration import (
    _alembic,
    _checks,
    _in_list,
    _indexes,
    _sql,
    _tables,
    _version,
)

API_DIR = Path(__file__).resolve().parents[2]
#: This revision (W21 CF-3).
CF3 = "c3f8e1a6d2b9"
#: 5D-W1's campaign-location data repair (Amendment D-1), which follows it.
REPAIR = "d5f616c0ec97"
#: The code head (D-N4 item 37's PostgreSQL append-only backstop), which follows the repair.
HEAD = "d0a8bce8de6a"
CF2 = "a7d2e9f4c1b8"
OVERRIDES = "workspace_capability_overrides"
NEW_TABLES = {"campaign_locations", "exports", "notifications", "notification_preferences"}
MIGRATION = next((API_DIR / "alembic" / "versions").glob(f"*-{CF3}_*.py"))

#: The jurisdiction CHECK of every table that carries a jurisdiction column.
JURISDICTION_CHECKS = {
    "business_locations": ("ck_business_location_jurisdiction_code", True),
    "geo_coverage_rules": ("ck_geo_coverage_rule_jurisdiction_code", True),
    "recommendation_briefs": ("ck_recommendation_brief_jurisdiction_code", True),
    "creative_drafts": ("ck_creative_draft_jurisdiction_code", True),
    "content_reviews": ("ck_content_review_jurisdiction_code", True),
    "exports": ("ck_export_jurisdiction_code", False),
}
#: Every CHECK the new tables carry, and every CHECK CF-3 adds to an existing table.
EXPECTED_CHECKS = {
    "campaign_locations": set(),
    "exports": {"ck_export_draft_version", "ck_export_format", "ck_export_jurisdiction_code"},
    "notifications": {"ck_notification_event_code"},
    "notification_preferences": {"ck_notification_preference_event_code"},
}
ADDED_CHECKS = {
    "campaigns": {"ck_campaign_mode", "ck_campaign_status"},
    **{t: {name} for t, (name, _nullable) in JURISDICTION_CHECKS.items() if t != "exports"},
}
_MODELS = {
    "campaign_locations": CampaignLocation,
    "exports": Export,
    "notifications": Notification,
    "notification_preferences": NotificationPreference,
    "campaigns": Campaign,
    "business_locations": BusinessLocation,
    "geo_coverage_rules": GeoCoverageRule,
    "recommendation_briefs": RecommendationBrief,
    "creative_drafts": CreativeDraft,
    "content_reviews": ContentReview,
}


def _norm(sqltext: str) -> str:
    return re.sub(r"\s+", " ", sqltext).strip()


def _columns(db_path: Path, table: str) -> set[str]:
    return {r[1] for r in _sql(db_path, f"PRAGMA table_info({table})")}


def _declared_checks(model) -> set[str]:
    return {
        c.name for c in model.__table__.constraints if c.__class__.__name__ == "CheckConstraint"
    }


def _fk_sql(db_path: Path, statement: str, params: tuple = ()) -> None:
    """One statement with foreign keys enforced, as ``app.db.session`` runs SQLite."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(statement, params)
        con.commit()
    finally:
        con.close()


@pytest.fixture()
def migrated(tmp_path) -> Path:
    db_path = tmp_path / "cf3.db"
    result = _alembic(db_path, "upgrade", CF3)
    assert result.returncode == 0, result.stderr
    assert _version(db_path) == [CF3]
    return db_path


# --------------------------------------------------------------------------- #
# Head, drift, and the CHECKs against their Python authorities
# --------------------------------------------------------------------------- #
def test_the_head_is_single_and_follows_cf2() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert [r.revision for r in script.get_revisions("heads")] == [HEAD]
    assert script.get_revision(HEAD).down_revision == REPAIR
    assert script.get_revision(REPAIR).down_revision == CF3
    assert script.get_revision(CF3).down_revision == CF2


def test_upgrade_leaves_no_model_drift(migrated: Path) -> None:
    assert _alembic(migrated, "upgrade", "head").returncode == 0  # check needs the head
    result = _alembic(migrated, "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert NEW_TABLES <= _tables(migrated)
    for table in ("business_locations", "geo_coverage_rules"):
        assert "jurisdiction_code" in _columns(migrated, table)
    for table in ("recommendation_briefs", "creative_drafts", "content_reviews"):
        assert {"jurisdiction_code", "deleted_at"} <= _columns(migrated, table), table
    assert "deleted_at" in _columns(migrated, "notifications")
    assert "jurisdiction_codes" in _columns(migrated, "claims_library")
    assert "uq_campaigns_id_workspace" in _indexes(migrated, "campaigns")
    assert "uq_business_locations_id_workspace" in _indexes(migrated, "business_locations")


def test_every_check_is_known(migrated: Path) -> None:
    """Discovery: a CHECK the database carries but this module does not know fails here."""
    for table, names in EXPECTED_CHECKS.items():
        assert set(_checks(migrated, table)) == names, table
        assert _declared_checks(_MODELS[table]) == names, table
    for table, names in ADDED_CHECKS.items():
        assert names <= set(_checks(migrated, table)), table
        assert names <= _declared_checks(_MODELS[table]), table


def test_vocabulary_checks_equal_their_authorities(migrated: Path) -> None:
    for table, name, column, authority in (
        ("campaigns", "ck_campaign_mode", "mode", {m.value for m in CampaignMode}),
        ("campaigns", "ck_campaign_status", "status", {s.value for s in CampaignStatus}),
        ("exports", "ck_export_format", "format", {f.value for f in ExportFormat}),
        ("notifications", "ck_notification_event_code", "event_code",
         {e.value for e in NotificationEventCode}),
        ("notification_preferences", "ck_notification_preference_event_code", "event_code",
         {e.value for e in NotificationEventCode}),
        (OVERRIDES, "ck_workspace_capability_override_capability", "capability",
         set(persisted_values())),
    ):
        in_db = _in_list(_checks(migrated, table)[name], column)
        assert (in_db - authority, authority - in_db) == (set(), set()), name
    assert "content_export" in _in_list(
        _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"],
        "capability",
    )


def _model_check(model, name: str) -> str:
    (constraint,) = [c for c in model.__table__.constraints if c.name == name]
    return str(constraint.sqltext)


def test_every_jurisdiction_check_equals_its_authority(migrated: Path) -> None:
    for table, (name, nullable) in JURISDICTION_CHECKS.items():
        in_db = _checks(migrated, table)[name]
        assert _norm(in_db) == _norm(jurisdiction_code_check_sql(nullable=nullable)), table
        assert ("IS NULL" in in_db) is nullable, table  # the export CHECK has no NULL limb
        # And the model declares exactly what the migration installed (alembic check never
        # compares CHECKs, so this is the model/migration congruence for them).
        assert _norm(_model_check(_MODELS[table], name)) == _norm(in_db), table


def test_the_vocabulary_checks_of_the_models_equal_the_migrated_ones(migrated: Path) -> None:
    for table, name in (
        ("campaigns", "ck_campaign_mode"),
        ("campaigns", "ck_campaign_status"),
        ("exports", "ck_export_format"),
        ("exports", "ck_export_draft_version"),
        ("notifications", "ck_notification_event_code"),
        ("notification_preferences", "ck_notification_preference_event_code"),
    ):
        assert _norm(_model_check(_MODELS[table], name)) == _norm(
            _checks(migrated, table)[name]
        ), name


# --------------------------------------------------------------------------- #
# The CHECKs hold on insert
# --------------------------------------------------------------------------- #
_EXPORT_SQL = (
    "INSERT INTO exports (id, organization_id, workspace_id, draft_id, draft_version,"
    " approval_id, format, jurisdiction_code, exported_by_snapshot, is_simulated)"
    " VALUES (?, 'o', 'w', NULL, ?, NULL, ?, ?, 'snap', 0)"
)
_GEO_SQL = (
    "INSERT INTO geo_coverage_rules (id, organization_id, workspace_id, coverage_type,"
    " included_markets, excluded_markets, online_global, jurisdiction_code, created_at,"
    " updated_at) VALUES (?, 'o', 'w', 'radius', '[]', '[]', 0, ?, '2026-11-01', '2026-11-01')"
)
_NOTIFICATION_SQL = (
    "INSERT INTO notifications (id, organization_id, workspace_id, user_id, event_code,"
    " subject_id) VALUES (?, 'o', 'w', 'u', ?, 's')"
)
_VALID_CODES = ("GB", "KE", "US-TX", "NG-LA", "GB-ENG", "CA-1")
_INVALID_CODES = ("UNRESOLVED", "us-tx", "Gb", "USA", "U", "US_TX", "US-", "US-ABCD", "")


def test_the_jurisdiction_checks_hold_on_insert(migrated: Path) -> None:
    for n, code in enumerate(_VALID_CODES):
        _sql(migrated, _GEO_SQL, (f"ok-{n}", code))
        _sql(migrated, _EXPORT_SQL, (f"ok-{n}", 1, "text/plain", code))
    _sql(migrated, _GEO_SQL, ("ok-null", None))  # NULL: nothing captured (nullable columns)
    for n, code in enumerate(_INVALID_CODES):
        with pytest.raises(sqlite3.IntegrityError):
            _sql(migrated, _GEO_SQL, (f"bad-{n}", code))
        with pytest.raises(sqlite3.IntegrityError):
            _sql(migrated, _EXPORT_SQL, (f"bad-{n}", 1, "text/plain", code))
    with pytest.raises(sqlite3.IntegrityError):  # born jurisdiction-aware: never NULL
        _sql(migrated, _EXPORT_SQL, ("null-code", 1, "text/plain", None))


def test_the_export_and_notification_checks_hold_on_insert(migrated: Path) -> None:
    for n, fmt in enumerate(f.value for f in ExportFormat):
        _sql(migrated, _EXPORT_SQL, (f"fmt-{n}", 1, fmt, "GB"))
    for row_id, version, fmt in (("bad-format", 1, "application/pdf"), ("bad-version", 0, "json")):
        with pytest.raises(sqlite3.IntegrityError):
            _sql(migrated, _EXPORT_SQL, (row_id, version, fmt, "GB"))
    for n, code in enumerate(e.value for e in NotificationEventCode):
        _sql(migrated, _NOTIFICATION_SQL, (f"n-{n}", code))
    with pytest.raises(sqlite3.IntegrityError):
        _sql(migrated, _NOTIFICATION_SQL, ("n-bad", "brief.deleted"))
    pref = (
        "INSERT INTO notification_preferences (id, organization_id, workspace_id, user_id,"
        " event_code, enabled) VALUES (?, 'o', 'w', 'u', ?, 0)"
    )
    _sql(migrated, pref, ("p-1", "brief.created"))
    with pytest.raises(sqlite3.IntegrityError):  # one row per (workspace, user, event)
        _sql(migrated, pref, ("p-2", "brief.created"))
    with pytest.raises(sqlite3.IntegrityError):
        _sql(migrated, pref, ("p-3", "sms.sent"))


def _tenant(db_path: Path) -> None:
    """Two organizations; A with workspaces A1 and A2, B with B1; one brand per workspace."""
    for org in ("org-a", "org-b"):
        _sql(db_path, "INSERT INTO organizations (id, name, slug) VALUES (?, ?, ?)",
             (org, org, org))
    for ws, org in (("ws-a1", "org-a"), ("ws-a2", "org-a"), ("ws-b1", "org-b")):
        _sql(
            db_path,
            "INSERT INTO workspaces (id, organization_id, name, slug, onboarding_completed)"
            " VALUES (?, ?, ?, ?, 1)",
            (ws, org, ws, ws),
        )
        _sql(
            db_path,
            "INSERT INTO brands (id, organization_id, workspace_id, name) VALUES (?, ?, ?, ?)",
            (f"brand-{ws}", org, ws, "Brand"),
        )


def _location(db_path: Path, loc: str, ws: str, org: str, *, active: bool = True) -> None:
    _sql(
        db_path,
        "INSERT INTO business_locations (id, organization_id, workspace_id, brand_id, name,"
        " languages, local_social_links, local_competitors, overrides, is_active)"
        " VALUES (?, ?, ?, ?, 'Shop', '[]', '{}', '[]', '{}', ?)",
        (loc, org, ws, f"brand-{ws}", 1 if active else 0),
    )


def _campaign(
    db_path: Path, cid: str, ws: str, org: str, location_ids, *, mode="per_location",
    status="active",
) -> None:
    _sql(
        db_path,
        "INSERT INTO campaigns (id, organization_id, workspace_id, brand_id, name, mode,"
        " location_ids, status) VALUES (?, ?, ?, ?, 'Campaign', ?, ?, ?)",
        (cid, org, ws, f"brand-{ws}", mode, json.dumps(location_ids), status),
    )


_MEMBERSHIP_SQL = (
    "INSERT INTO campaign_locations (id, organization_id, workspace_id, campaign_id,"
    " location_id) VALUES (?, ?, ?, ?, ?)"
)


def test_the_workspace_consistency_keys_refuse_a_cross_workspace_membership(
    migrated: Path,
) -> None:
    _tenant(migrated)
    _location(migrated, "loc-a1", "ws-a1", "org-a")
    _location(migrated, "loc-a2", "ws-a2", "org-a")
    _location(migrated, "loc-b1", "ws-b1", "org-b")
    _campaign(migrated, "camp-a1", "ws-a1", "org-a", [])
    _fk_sql(migrated, _MEMBERSHIP_SQL, ("m-ok", "org-a", "ws-a1", "camp-a1", "loc-a1"))
    for row_id, ws, location in (
        ("m-same-org-other-ws", "ws-a1", "loc-a2"),  # another workspace of the same org
        ("m-other-org", "ws-a1", "loc-b1"),  # another organization
        ("m-row-in-location-ws", "ws-a2", "loc-a2"),  # row scoped to the location's workspace
        ("m-missing-location", "ws-a1", "loc-none"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _fk_sql(migrated, _MEMBERSHIP_SQL, (row_id, "org-a", ws, "camp-a1", location))
    with pytest.raises(sqlite3.IntegrityError):  # one row per (campaign, location)
        _fk_sql(migrated, _MEMBERSHIP_SQL, ("m-dup", "org-a", "ws-a1", "camp-a1", "loc-a1"))
    assert _sql(migrated, "SELECT id FROM campaign_locations") == [("m-ok",)]
    # Every key cascades: the membership goes with its campaign.
    _fk_sql(migrated, "DELETE FROM campaigns WHERE id = 'camp-a1'")
    assert _sql(migrated, "SELECT id FROM campaign_locations") == []


def test_the_campaign_vocabulary_checks_hold_on_insert(migrated: Path) -> None:
    _tenant(migrated)
    _campaign(migrated, "c-ok", "ws-a1", "org-a", [], mode="grouped", status="draft")
    for cid, mode, status in (("c-mode", "Grouped", "active"), ("c-status", "grouped", "live")):
        with pytest.raises(sqlite3.IntegrityError):
            _campaign(migrated, cid, "ws-a1", "org-a", [], mode=mode, status=status)


# --------------------------------------------------------------------------- #
# The O-8 back-fill and the vocabulary normalisation (from a real CF-2 database)
# --------------------------------------------------------------------------- #
#: A 40-character status (it fits ``VARCHAR(40)`` on PostgreSQL) whose repr is 62 characters.
LONG_STATUS = "\n" * 20 + "x" * 20
#: Its full rewrite report line (the repr has no real newline, so the report is one line).
LONG_STATUS_LINE = (
    f"campaign status: campaign=camp-long stored={LONG_STATUS!r} rewritten_to=paused"
)
assert len(repr(LONG_STATUS)) > 60


@pytest.fixture()
def at_cf2_with_campaigns(tmp_path) -> Path:
    db_path = tmp_path / "cf2-campaigns.db"
    result = _alembic(db_path, "upgrade", CF2)
    assert result.returncode == 0, result.stderr
    _tenant(db_path)
    _location(db_path, "loc-a1", "ws-a1", "org-a")
    _location(db_path, "loc-a1-old", "ws-a1", "org-a", active=False)
    _location(db_path, "loc-a2", "ws-a2", "org-a")
    _location(db_path, "loc-b1", "ws-b1", "org-b")
    # Valid, deactivated-but-own, duplicated, same-org-other-workspace, other-org, missing.
    _campaign(
        db_path, "camp-mixed", "ws-a1", "org-a",
        ["loc-a1", "loc-a1-old", "loc-a1", "loc-a2", "loc-b1", "loc-gone"],
        mode="Per-Location", status=" Active ",
    )
    _campaign(db_path, "camp-malformed", "ws-a1", "org-a", {"x": 1}, mode="SAME FOR ALL",
              status="running")
    _campaign(db_path, "camp-empty", "ws-b1", "org-b", [], mode="bogus", status="archived")
    _campaign(db_path, "camp-b", "ws-b1", "org-b", ["loc-b1"])
    # Unknown statuses that CONTAIN a member's text: never read as that member (review DATA C3).
    _campaign(db_path, "camp-inactive", "ws-b1", "org-b", [], mode="grouped", status="inactive")
    _campaign(db_path, "camp-not-active", "ws-b1", "org-b", [], mode="grouped",
              status="not active")
    # A value whose repr exceeds 60 characters: its rewrite report must carry all of it, the
    # only record of the replaced value (review DATA C1).
    _campaign(db_path, "camp-long", "ws-b1", "org-b", [], mode="grouped", status=LONG_STATUS)
    return db_path


def test_the_backfill_migrates_only_own_workspace_ids_and_reports_the_rest(
    at_cf2_with_campaigns: Path,
) -> None:
    db_path = at_cf2_with_campaigns
    before = dict(_sql(db_path, "SELECT id, location_ids FROM campaigns"))

    up = _alembic(db_path, "upgrade", CF3)

    assert up.returncode == 0, up.stderr
    pairs = set(_sql(
        db_path,
        "SELECT campaign_id, location_id, workspace_id, organization_id FROM campaign_locations",
    ))
    assert pairs == {
        ("camp-mixed", "loc-a1", "ws-a1", "org-a"),
        ("camp-mixed", "loc-a1-old", "ws-a1", "org-a"),  # deactivated: history, kept
        ("camp-b", "loc-b1", "ws-b1", "org-b"),
    }
    out = up.stdout + up.stderr
    for location, reason in (
        ("loc-a2", "foreign_workspace"),
        ("loc-b1", "foreign_workspace"),
        ("loc-gone", "missing"),
    ):
        assert (
            f"dropped campaign=camp-mixed workspace=ws-a1 location={location} reason={reason}"
            in out
        ), out
    assert "dropped campaign=camp-malformed workspace=ws-a1 location=None reason=malformed" in out
    assert "3 membership(s) inserted, 4 reference(s) dropped" in out
    # The JSON arrays are retained untouched (read for one more release, O-8).
    assert dict(_sql(db_path, "SELECT id, location_ids FROM campaigns")) == before
    assert _alembic(db_path, "upgrade", "head").returncode == 0  # check needs the head
    assert _alembic(db_path, "check").returncode == 0


def test_the_campaign_vocabularies_are_normalised_and_every_rewrite_reported(
    at_cf2_with_campaigns: Path,
) -> None:
    db_path = at_cf2_with_campaigns

    up = _alembic(db_path, "upgrade", CF3)

    assert up.returncode == 0, up.stderr
    stored = {r[0]: (r[1], r[2]) for r in _sql(db_path, "SELECT id, mode, status FROM campaigns")}
    assert stored == {
        "camp-mixed": ("per_location", "active"),  # spelling normalised
        "camp-malformed": ("same_for_all", "paused"),  # unknown status: never 'active'
        "camp-empty": ("per_location", "archived"),  # unknown mode: the column default
        "camp-b": ("per_location", "active"),  # already valid: untouched
        "camp-inactive": ("grouped", "paused"),  # contains "active": still never active
        "camp-not-active": ("grouped", "paused"),
        "camp-long": ("grouped", "paused"),
    }
    out = up.stdout + up.stderr
    for line in (
        "campaign mode: campaign=camp-mixed stored='Per-Location' rewritten_to=per_location",
        "campaign status: campaign=camp-mixed stored=' Active ' rewritten_to=active",
        "campaign mode: campaign=camp-malformed stored='SAME FOR ALL' rewritten_to=same_for_all",
        "campaign status: campaign=camp-malformed stored='running' rewritten_to=paused",
        "campaign mode: campaign=camp-empty stored='bogus' rewritten_to=per_location",
        "campaign status: campaign=camp-inactive stored='inactive' rewritten_to=paused",
        "campaign status: campaign=camp-not-active stored='not active' rewritten_to=paused",
        LONG_STATUS_LINE,
        "CF-3 campaign vocabularies: 8 value(s) rewritten",
    ):
        assert line in out, out
    assert "campaign=camp-b" not in out.split("back-fill")[0]


def test_the_migrate_entrypoint_reports_the_full_replaced_value(
    at_cf2_with_campaigns: Path,
) -> None:
    """The deployed path (``python -m app.db.migrate upgrade``, JSON logs) carries the same
    untruncated report as the alembic CLI (review DATA C1)."""
    env = dict(os.environ)
    env.pop("TEST_POSTGRES_URL", None)
    env.update(DATABASE_URL=f"sqlite:///{at_cf2_with_campaigns}", LOG_FORMAT="json")
    up = subprocess.run(
        [sys.executable, "-m", "app.db.migrate", "upgrade"],
        cwd=API_DIR, env=env, capture_output=True, text=True,
    )
    assert up.returncode == 0, up.stdout + up.stderr
    messages = []
    for line in up.stdout.splitlines():
        if line.startswith("{"):
            messages.append(json.loads(line).get("message", ""))
    assert any(m.endswith(LONG_STATUS_LINE) for m in messages), messages
    assert _version(at_cf2_with_campaigns) == [HEAD]


def _migration_module():
    spec = importlib.util.spec_from_file_location("cf3_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_backfill_is_idempotent(at_cf2_with_campaigns: Path) -> None:
    db_path = at_cf2_with_campaigns
    assert _alembic(db_path, "upgrade", CF3).returncode == 0
    rows_before = sorted(_sql(db_path, "SELECT * FROM campaign_locations"))
    migration = _migration_module()
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            inserted, dropped = migration.backfill_campaign_locations(conn)
            rewrites = migration.normalise_campaign_vocabularies(conn)
    finally:
        engine.dispose()
    assert inserted == 0 and rewrites == []
    assert len(dropped) == 4  # still reported, still not migrated
    assert sorted(_sql(db_path, "SELECT * FROM campaign_locations")) == rows_before


# --------------------------------------------------------------------------- #
# Downgrade and re-upgrade
# --------------------------------------------------------------------------- #
def _refused(db_path: Path, message: str) -> None:
    tables_before = _tables(db_path)
    refused = _alembic(db_path, "downgrade", "-1")
    assert refused.returncode != 0
    assert message in refused.stdout + refused.stderr, refused.stdout + refused.stderr
    assert "Nothing was changed." in refused.stdout + refused.stderr
    # Nothing changed: same revision, same tables, the new columns and CHECKs kept.
    assert _version(db_path) == [CF3]
    assert _tables(db_path) == tables_before
    assert "jurisdiction_code" in _columns(db_path, "business_locations")
    assert {"ck_campaign_mode", "ck_campaign_status"} <= set(_checks(db_path, "campaigns"))
    assert "content_export" in _in_list(
        _checks(db_path, OVERRIDES)["ck_workspace_capability_override_capability"],
        "capability",
    )


def test_downgrade_refuses_while_an_override_names_the_capability(migrated: Path) -> None:
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-export', 'o', 'w', 'content_export', 1)",
    )
    _refused(migrated, "name 'content_export'; clear them before downgrading")


def test_downgrade_refuses_while_exports_hold_rows(migrated: Path) -> None:
    _sql(migrated, _EXPORT_SQL, ("kept-export", 1, "text/plain", "GB"))
    _refused(migrated, "export record(s) exist; exports are compliance records (O-6)")
    assert _sql(migrated, "SELECT id FROM exports") == [("kept-export",)]


_DRAFT_SQL = (
    "INSERT INTO creative_drafts (id, organization_id, workspace_id, brief_id, opportunity_id,"
    " draft_version, channel, body, prompt_name, prompt_version, model_identifier, input_hash,"
    " is_simulated, input_tokens, output_tokens, estimated_cost_usd, created_by_snapshot,"
    " jurisdiction_code, deleted_at) VALUES ('d1', 'o', 'w', 'b1', 'opp', 1, 'email', 'text',"
    " 'generate_creative', '1.0.0', 'mock', 'h', 1, 0, 0, 0, 'snap', ?, ?)"
)
_BRIEF_SQL = (
    "INSERT INTO recommendation_briefs (id, organization_id, workspace_id, opportunity_id,"
    " intelligence_record_id, brief_version, analysis_version, scoring_version, prompt_name,"
    " prompt_version, model_identifier, input_hash, is_simulated, created_by_snapshot, summary,"
    " rationale, recommended_actions, suggested_angles, risk_notes, evidence_refs,"
    " jurisdiction_code, deleted_at) VALUES ('b1', 'o', 'w', 'opp', 'intel', 1, '1', '1',"
    " 'recommendation_brief', '1.0.0', 'mock', 'h', 1, 'snap', 's', 'r', '[]', '[]', '[]',"
    " '[]', ?, ?)"
)
_REVIEW_SQL = (
    "INSERT INTO content_reviews (id, organization_id, workspace_id, draft_id, draft_version,"
    " risk_level, is_blocked, findings, warnings, blocked_claim_ids, required_disclaimers,"
    " disclaimers_resolved, passed, jurisdiction_code, deleted_at) VALUES ('r1', 'o', 'w', 'd1',"
    " 1, 'low', 0, '[]', '[]', '[]', '[]', 1, 1, ?, ?)"
)
_SNAPSHOT_ROWS = {
    "recommendation_briefs": _BRIEF_SQL,
    "creative_drafts": _DRAFT_SQL,
    "content_reviews": _REVIEW_SQL,
}


@pytest.mark.parametrize("table", list(_SNAPSHOT_ROWS))
def test_downgrade_refuses_while_a_jurisdiction_snapshot_is_captured(
    migrated: Path, table: str
) -> None:
    _sql(migrated, _SNAPSHOT_ROWS[table], ("US-TX", None))
    _refused(migrated, f"{table} row(s) hold a captured jurisdiction snapshot")


@pytest.mark.parametrize("table", [*_SNAPSHOT_ROWS, "notifications"])
def test_downgrade_refuses_while_a_row_is_marked_deleted(migrated: Path, table: str) -> None:
    if table == "notifications":
        _sql(
            migrated,
            "INSERT INTO notifications (id, organization_id, workspace_id, user_id, event_code,"
            " subject_id, deleted_at) VALUES ('n1', 'o', 'w', 'u', 'brief.created', 's',"
            " '2026-11-01 00:00:00')",
        )
    else:
        _sql(migrated, _SNAPSHOT_ROWS[table], (None, "2026-11-01 00:00:00"))
    _refused(migrated, f"{table} row(s) are marked deleted_at")


_CLAIM_SQL = (
    "INSERT INTO claims_library (id, organization_id, workspace_id, brand_id, text, kind,"
    " risk_level, jurisdiction_codes, created_at, updated_at) VALUES (?, 'o', 'w', 'b', 'claim',"
    " 'approved', 'low', ?, '2026-11-01 00:00:00', '2026-11-01 00:00:00')"
)


def test_downgrade_refuses_while_a_claim_is_jurisdiction_restricted(migrated: Path) -> None:
    _sql(migrated, _CLAIM_SQL, ("all-null", None))
    _sql(migrated, _CLAIM_SQL, ("all-empty", "[]"))
    _sql(migrated, _CLAIM_SQL, ("texas-only", '["US-TX"]'))
    _refused(migrated, "claims-library entr(y/ies) are jurisdiction-restricted")


def test_one_step_down_removes_exactly_this_revision_and_up_restores_it(
    migrated: Path,
) -> None:
    indexes = {t: _indexes(migrated, t) for t in (
        "campaigns", "business_locations", "geo_coverage_rules", "recommendation_briefs",
        "creative_drafts", "content_reviews", "claims_library", OVERRIDES,
    )}
    # Rows the downgrade must keep, and the rows it may drop (derivable, uncaptured, dark).
    _tenant(migrated)
    _location(migrated, "loc-a1", "ws-a1", "org-a")
    _campaign(migrated, "camp-a1", "ws-a1", "org-a", ["loc-a1"], mode="grouped",
              status="draft")
    _sql(migrated, _MEMBERSHIP_SQL, ("m1", "org-a", "ws-a1", "camp-a1", "loc-a1"))
    _sql(migrated, _CLAIM_SQL, ("all-empty", "[]"))
    _sql(migrated, _SNAPSHOT_ROWS["creative_drafts"], (None, None))
    _sql(migrated, _NOTIFICATION_SQL, ("n1", "brief.created"))
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-draft', 'o', 'w', 'creative_generation', 1)",
    )

    down = _alembic(migrated, "downgrade", "-1")
    assert down.returncode == 0, down.stdout + down.stderr
    assert _version(migrated) == [CF2]
    assert not NEW_TABLES & _tables(migrated)
    for table in ("business_locations", "geo_coverage_rules", "recommendation_briefs",
                  "creative_drafts", "content_reviews"):
        assert not {"jurisdiction_code", "deleted_at"} & _columns(migrated, table), table
    assert "jurisdiction_codes" not in _columns(migrated, "claims_library")
    assert not {"ck_campaign_mode", "ck_campaign_status"} & set(_checks(migrated, "campaigns"))
    narrowed = _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"]
    assert "content_export" not in _in_list(narrowed, "capability")
    assert indexes["campaigns"] - _indexes(migrated, "campaigns") == {"uq_campaigns_id_workspace"}
    assert indexes["business_locations"] - _indexes(migrated, "business_locations") == {
        "uq_business_locations_id_workspace"
    }
    for table in ("geo_coverage_rules", "recommendation_briefs", "creative_drafts",
                  "content_reviews", "claims_library", OVERRIDES):
        assert _indexes(migrated, table) == indexes[table], table
    # Every row of the kept tables survives the batch rebuilds.
    assert _sql(migrated, "SELECT id, mode, status, location_ids FROM campaigns") == [
        ("camp-a1", "grouped", "draft", '["loc-a1"]')
    ]
    assert _sql(migrated, "SELECT id FROM claims_library") == [("all-empty",)]
    assert _sql(migrated, "SELECT id FROM creative_drafts") == [("d1",)]
    assert _sql(migrated, f"SELECT id FROM {OVERRIDES}") == [("ov-draft",)]

    up = _alembic(migrated, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _version(migrated) == [HEAD]
    assert NEW_TABLES <= _tables(migrated)
    # The re-upgrade back-fills the membership again from the kept array.
    assert _sql(migrated, "SELECT campaign_id, location_id FROM campaign_locations") == [
        ("camp-a1", "loc-a1")
    ]
    assert _alembic(migrated, "check").returncode == 0
