"""D-9d: ``source_takedowns`` is a suppression record free of identifying metadata, and
the suppression outlives every piece of metadata around it (P6-PRIV-4).

Two proofs, on a database of this module's own:

* **Nested identifying metadata.** After a first takedown, a repeat, and the deletion of
  the operator's account, every JSON value in the row (``purge_summary``,
  ``repeat_purges``) is scanned recursively: no key other than ``at`` / ``purged`` / a
  count name, no value equal to any user id, e-mail address, the ticket id, the reason or a case id,
  no string longer than an ISO timestamp. Before D-9d a deleted operator's user id
  survived inside ``repeat_purges`` (the FK column was SET NULL; the JSON copy was not).
* **Suppression persistence.** ``active_takedowns()`` still names the key, the live
  gate (``live_targets_for``) still refuses the source and ``without_taken_down`` still
  drops its signals after each of: the actor's user row deleted; the per-workspace
  audit rows anonymised (actor NULL, context cleared); every audit row for the source
  deleted (audit expiry); ``purge_summary`` / ``repeat_purges`` cleared. Anonymising or
  expiring metadata can never re-enable fetching a taken-down source.

The first proof deletes the actor through the ORM with the schema's own ON DELETE rules
(``PRAGMA foreign_keys=ON``). With P6-PRIV-1 merged, further variants use the real
mechanisms: the retention sweep expiring the ``audit_logs`` class, and the account-deletion
route erasing the operator's account together with the organization it is the only
member of (which anonymises and deletes that tenant's audit rows) -- the case records
stay, with the actor link cleared -- and the organization export, which never carries the
private reason (D-9e).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.audit.models import AuditLog
from app.brands.models import Brand
from app.connectors.live import SourceTarget, live_targets_for
from app.connectors.models import SourceTakedown, SourceTakedownCase
from app.connectors.sources import LegalStatus, SourceRecord
from app.connectors.takedown import (
    AUDIT_ACTION,
    active_takedowns,
    takedown_source,
    validate_external_reference,
    validate_reason,
    without_taken_down,
)
from app.core.config import Settings, get_settings
from app.core.enums import ScoutRequestStatus
from app.core.errors import ValidationDomainError
from app.organizations.models import Organization, User
from app.privacy import retention
from app.privacy.inventory import RetentionClass
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal
from app.tests._auth2_support import (
    ALICE,
    BOB,
    EMAIL,
    OLD_PASSWORD,
    ORG_A,
    ORG_B,
    WS_A,
    WS_B,
    Env,
    bearer,
    environment,
)

API = get_settings().api_prefix
MARKET = "Toronto, ON"
KEY = "privacy-proof-business"
OTHER = "privacy-proof-food"
#: The operator's private ticket id / reason: case record only (D-9e), so also forbidden in
#: the suppression row and in every tenant audit row.
REFERENCE = "TKD-PRIV-1"
REPEAT_REFERENCE = "TKD-PRIV-1-repeat"
REASON = "Counsel memo PRIV-1 (confidential)"
REPEAT_REASON = "Counsel memo PRIV-1, second letter (confidential)"
#: The only keys a repeat entry may carry, and the count names a purge may report.
ENTRY_KEYS = {"at", "purged"}
COUNT_NAMES = {
    "raw_signals",
    "normalized_signals",
    "opportunities_deleted",
    "opportunities_redacted",
    "feedback_removed",
    "clusters_deleted",
}
#: ``datetime.isoformat()`` with microseconds and a UTC offset: the longest JSON string
#: the row may legitimately hold (a timestamp); anything longer is not a count or a time.
ISO_TIMESTAMP_MAX = len(datetime(2026, 1, 1, tzinfo=UTC).replace(microsecond=1).isoformat())
#: The persistent suppression record (D-9d): key, time, counts, row bookkeeping.
MINIMAL_COLUMNS = {
    "id", "source_key", "taken_down_at", "purge_summary", "repeat_purges",
    "created_at", "updated_at",
}


def _record(key: str) -> SourceRecord:
    return SourceRecord(
        source_key=key,
        connector="rss_news",
        display_name=key,
        publisher="Proof Publisher",
        feed_url=f"https://{key}.example/feed.xml",
        markets=(MARKET,),
        tos_url=f"https://{key}.example/terms",
        approval_reference="DEC-PROOF-1",
        approved_on=date(2026, 1, 1),
        legal_status=LegalStatus.APPROVED,
        enabled=True,
    )


TARGETS = [
    SourceTarget(source=_record(k), fetch_url=_record(k).feed_url, delivery="live")
    for k in (KEY, OTHER)
]


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        connector_rss_live_enabled=True,
        connector_rss_kill_switch=False,
        connector_rss_replay_origin=None,
        connector_egress_contact_url="https://signalnest.example/bot",
    )


def _seed_tenant(s: Session, org: str, ws: str, slug: str) -> None:
    brand = Brand(organization_id=org, workspace_id=ws, name=slug, industry="coffee")
    s.add(brand)
    s.flush()
    req = ScoutRequest(
        organization_id=org, workspace_id=ws, brand_id=brand.id, name=slug,
        status=ScoutRequestStatus.COMPLETED.value, source_types=[], keywords=["coffee"],
        resolved_market=MARKET,
    )
    s.add(req)
    s.flush()
    for key, n in ((KEY, 2), (OTHER, 1)):
        for i in range(n):
            raw = RawSignal(
                organization_id=org, workspace_id=ws, scout_request_id=req.id,
                source_type="rss_news", content=f"{slug} {key} {i}", is_simulated=False,
                source_key=key, raw_metadata={"provenance": {"source_key": key}},
            )
            s.add(raw)
            s.flush()
            s.add(
                NormalizedSignal(
                    organization_id=org, workspace_id=ws, scout_request_id=req.id,
                    raw_signal_id=raw.id, source_type="rss_news", excerpt=raw.content,
                    content_hash=f"{slug}-{key}-{i}", is_simulated=False, source_key=key,
                    ingest_metadata={"provenance": {"source_key": key}},
                )
            )
    s.flush()


@dataclass
class World:
    env: Env
    factory: sessionmaker

    def session(self) -> Session:
        return self.factory()

    def take_down(self, user_id: str, reference: str) -> dict:
        r = self.env.client.post(
            f"{API}/internal/system/data-sources/{KEY}/takedown",
            headers={"Authorization": f"Bearer {bearer(self.env, user_id)}"},
            json={
                "reason": REPEAT_REASON if reference == REPEAT_REFERENCE else REASON,
                "external_reference": reference,
            },
        )
        assert r.status_code == 200, r.text
        return r.json()

    def row(self) -> dict:
        """The persistent record as the database holds it (raw SQL, every column)."""
        with self.env.request_engine.connect() as conn:
            result = conn.execute(
                text("SELECT * FROM source_takedowns WHERE source_key = :k"), {"k": KEY}
            )
            [values] = result.mappings().all()
        return dict(values)


@pytest.fixture
def world(tmp_path) -> Iterator[World]:
    with environment(tmp_path / "takedown-privacy.db") as env:
        factory = sessionmaker(bind=env.request_engine, expire_on_commit=False, future=True)
        with factory() as s:
            for user_id in (ALICE, BOB):
                s.get(User, user_id).is_operator = True
            _seed_tenant(s, ORG_A, WS_A, "tenant-a")
            _seed_tenant(s, ORG_B, WS_B, "tenant-b")
            s.commit()
        yield World(env=env, factory=factory)


# --------------------------------------------------------------------------- #
# The operator's private ticket id and reason: validated, kept in the case record only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("TKD-1\u009b", id="C1 control (CSI, Cc)"),
        pytest.param("TKD-\u200b1", id="zero width space (Cf)"),
        pytest.param("TKD-1\u202e", id="right-to-left override (Cf)"),
        pytest.param("TKD-1\u2028", id="line separator (Zl)"),
        pytest.param("TKD-1\u2029", id="paragraph separator (Zp)"),
        pytest.param("TKD-1\ue000", id="private use (Co)"),
        pytest.param("TKD-1\udcff", id="surrogate escape (Cs)"),
        pytest.param("TKD-1\x07", id="C0 control"),
        pytest.param("TKD-1\x7f", id="DEL"),
        pytest.param("TKD-1\nmemo", id="newline"),
        pytest.param("TKD-1\tmemo", id="tab"),
        pytest.param("x" * 201, id="too long"),
    ],
)
def test_an_external_reference_never_carries_invisible_or_control_characters(bad: str):
    """The ticket id is read by operators in the case listing: no C0/C1 control, format
    (zero-width, bidi override), private-use, surrogate or line/paragraph separator
    character may hide in it, on top of the one-line, <= 200 rule (r6 C-r6-2, kept)."""
    with pytest.raises(ValidationDomainError):
        validate_external_reference(bad)


@pytest.mark.parametrize(
    "good",
    [
        pytest.param("TKD-2026-0042", id="ticket id"),
        pytest.param("  REQ 7 / Zürich Straße — café  ", id="accented text with spaces"),
        pytest.param("Žádost č. 12 (Ñandú)", id="accented letters and punctuation"),
        pytest.param("counsel@example.com", id="address (private now: never shown to tenants)"),
    ],
)
def test_ordinary_visible_text_is_an_acceptable_external_reference(good: str):
    assert validate_external_reference(good) == good.strip()


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_the_external_reference_is_optional(blank):
    assert validate_external_reference(blank) is None


def test_a_reason_is_free_text_but_bounded_and_clean():
    assert validate_reason("  Line one\nLine two\tcol  ") == "Line one\nLine two\tcol"
    for bad in ("", "   ", "x" * 2001, "bell\x07", "nul\x00", "pua\ue000", "surrogate\udcff"):
        with pytest.raises(ValidationDomainError):
            validate_reason(bad)
    assert validate_reason("x" * 2000)


# --------------------------------------------------------------------------- #
# Nested identifying metadata
# --------------------------------------------------------------------------- #
def _walk(value, path: str = "$") -> Iterator[tuple[str, object]]:
    """Every (path, leaf) and every (path, key) in a JSON value, recursively."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}.<key>", k
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, value


def _json(value):
    """A JSON column as raw SQL returns it (text on SQLite, decoded on PostgreSQL)."""
    return json.loads(value) if isinstance(value, str) else value


def _assert_nothing_identifying(row: dict, *, forbidden: set[str], repeats: int) -> None:
    assert set(row) == MINIMAL_COLUMNS, sorted(row)
    assert row["source_key"] == KEY
    # Column values: nothing equal to a user id, an address or a reference.
    for column, value in row.items():
        assert value not in forbidden, (column, value)
    summary = _json(row["purge_summary"])
    repeat_purges = _json(row["repeat_purges"])
    assert set(summary) <= COUNT_NAMES and all(isinstance(v, int) for v in summary.values())
    assert len(repeat_purges) == repeats
    for entry in repeat_purges:
        assert set(entry) == ENTRY_KEYS, entry
        assert set(entry["purged"]) <= COUNT_NAMES, entry
        # Every leaf under a count name is an int, as for purge_summary (r6 C-r6-3).
        assert all(
            isinstance(v, int) and not isinstance(v, bool) for v in entry["purged"].values()
        ), entry
        datetime.fromisoformat(entry["at"])  # a timestamp, nothing else
    for path, leaf in _walk({"purge_summary": summary, "repeat_purges": repeat_purges}):
        if path.endswith(".<key>"):
            assert leaf in ENTRY_KEYS | COUNT_NAMES | {"purge_summary", "repeat_purges"}, (
                path, leaf,
            )
            continue
        assert leaf not in forbidden, (path, leaf)
        if isinstance(leaf, str):
            assert len(leaf) <= ISO_TIMESTAMP_MAX, (path, leaf)
        else:
            assert isinstance(leaf, int) and not isinstance(leaf, bool), (path, leaf)


def test_the_takedown_row_never_holds_identifying_metadata_even_nested(world: World):
    forbidden = {ALICE, BOB, *EMAIL.values(), REFERENCE, REPEAT_REFERENCE, REASON, REPEAT_REASON}
    with world.session() as s:
        forbidden |= set(s.scalars(select(User.id))) | set(s.scalars(select(User.email)))

    # (1) first takedown, by ALICE
    first = world.take_down(ALICE, REFERENCE)
    assert first["verified"] and first["purged"]["raw_signals"] == 4
    forbidden.add(first["case_id"])
    _assert_nothing_identifying(world.row(), forbidden=forbidden, repeats=0)
    with world.session() as s:
        entries = s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        # Attribution and the opaque case id are in the tenant audit rows; the reason and
        # ticket id are in the case record, nowhere else.
        assert {(e.organization_id, e.workspace_id) for e in entries} == {
            (ORG_A, WS_A), (ORG_B, WS_B),
        }
        assert all(e.actor_user_id == ALICE and e.context["case_id"] == first["case_id"]
                   for e in entries)

    # (2) a repeat, by ALICE again
    second = world.take_down(ALICE, REPEAT_REFERENCE)
    assert second["already_taken_down"] and second["case_id"] != first["case_id"]
    forbidden.add(second["case_id"])
    _assert_nothing_identifying(world.row(), forbidden=forbidden, repeats=1)
    before = world.row()

    # (3) the operator's account is deleted (no account-deletion route on this branch:
    # the ORM delete with the schema's ON DELETE rules — audit_logs.actor_user_id SET NULL)
    with world.session() as s:
        s.execute(delete(User).where(User.id == ALICE))
        s.commit()
    with world.session() as s:
        assert s.get(User, ALICE) is None
        entries = s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        assert entries and all(e.actor_user_id is None for e in entries)
        assert all(e.context["case_id"] == first["case_id"] for e in entries)  # evidence kept
        # The case records outlive the account; only the actor link is cleared.
        cases = s.scalars(select(SourceTakedownCase).order_by(SourceTakedownCase.created_at)).all()
        assert [c.id for c in cases] == [first["case_id"], second["case_id"]]
        assert all(c.actor_user_id is None for c in cases)
        assert [c.reason for c in cases] == [REASON, REPEAT_REASON]
    after = world.row()
    assert after == before  # nothing in the suppression record referred to the account
    _assert_nothing_identifying(after, forbidden=forbidden, repeats=1)


def test_the_service_records_a_repeat_as_time_and_counts_only(world: World):
    """Direct service calls (the migration and PostgreSQL proofs use them) write the same
    minimal entry as the route: no actor, no reference, whatever the caller passes."""
    with world.session() as s:
        takedown_source(
            s, source_key=KEY, reason=REASON, external_reference=REFERENCE, actor_user_id=ALICE
        )
        s.commit()
    with world.session() as s:
        result = takedown_source(
            s, source_key=KEY, reason=REPEAT_REASON, external_reference=REPEAT_REFERENCE,
            actor_user_id=BOB,
        )
        s.commit()
    assert result.already_taken_down
    [entry] = result.takedown.repeat_purges
    assert set(entry) == ENTRY_KEYS and entry["purged"] == result.purged
    _assert_nothing_identifying(
        world.row(),
        forbidden={ALICE, BOB, REFERENCE, REPEAT_REFERENCE, REASON, REPEAT_REASON},
        repeats=1,
    )


# --------------------------------------------------------------------------- #
# Suppression persistence
# --------------------------------------------------------------------------- #
def _signal(key: str) -> SimpleNamespace:
    return SimpleNamespace(attribution={"source_key": key}, content=key)


def _assert_still_suppressed(s: Session, *, step: str) -> None:
    takedowns = active_takedowns(s)
    assert KEY in takedowns and OTHER not in takedowns, step
    # The live gate (app.connectors.live) refuses the source and nothing else.
    allowed = live_targets_for(
        market=MARKET, source_types=(), takedowns=takedowns, settings=_live_settings(),
        targets=TARGETS,
    )
    assert [t.source.source_key for t in allowed] == [OTHER], step
    # A run in flight re-checks under the lock and drops the source's signals.
    kept = without_taken_down(s, [_signal(KEY), _signal(OTHER), _signal(KEY)])
    assert [sig.content for sig in kept] == [OTHER], step


def test_suppression_survives_erasure_anonymisation_and_expiry_of_every_metadata(
    world: World,
):
    world.take_down(ALICE, REFERENCE)
    world.take_down(ALICE, REPEAT_REFERENCE)
    with world.session() as s:
        _assert_still_suppressed(s, step="after the takedown and a repeat")

    # 1. The actor's user row is deleted.
    with world.session() as s:
        s.execute(delete(User).where(User.id == ALICE))
        s.commit()
    with world.session() as s:
        _assert_still_suppressed(s, step="actor deleted")

    # 2. The per-workspace audit rows are anonymised (actor NULL, context cleared).
    with world.session() as s:
        s.execute(
            update(AuditLog)
            .where(AuditLog.action == AUDIT_ACTION)
            .values(actor_user_id=None, context={}, new_state={}, reason=None)
        )
        s.commit()
    with world.session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        _assert_still_suppressed(s, step="audit rows anonymised")

    # 3. Every audit row for the source is deleted (audit expiry).
    with world.session() as s:
        s.execute(delete(AuditLog).where(AuditLog.action == AUDIT_ACTION))
        s.execute(delete(AuditLog).where(AuditLog.entity_type == "source_takedown"))
        s.commit()
    with world.session() as s:
        assert s.scalars(select(AuditLog)).all() == []
        _assert_still_suppressed(s, step="audit rows expired")

    # 4. The counts on the record itself are cleared.
    with world.session() as s:
        s.execute(
            update(SourceTakedown)
            .where(SourceTakedown.source_key == KEY)
            .values(purge_summary={}, repeat_purges=[])
        )
        s.commit()
    with world.session() as s:
        row = s.scalar(select(SourceTakedown).where(SourceTakedown.source_key == KEY))
        assert row.purge_summary == {} and row.repeat_purges == []
        _assert_still_suppressed(s, step="counts cleared")

    # 5. The operator case records are deleted (a future retention sweep, or a manual
    # erasure): the suppression does not depend on them either.
    with world.session() as s:
        s.execute(delete(SourceTakedownCase))
        s.commit()
    with world.session() as s:
        _assert_still_suppressed(s, step="case records deleted")

    # The registry still shows the takedown (to a remaining operator), with nothing
    # identifying in it, and the key cannot be taken down "for the first time" again.
    listing = world.env.client.get(
        f"{API}/internal/system/data-sources",
        headers={"Authorization": f"Bearer {bearer(world.env, BOB)}"},
    )
    assert listing.status_code == 200, listing.text
    entry = {e["source_key"]: e for e in listing.json()["sources"]}[KEY]
    assert entry["takedown"] == {
        "taken_down_at": entry["takedown"]["taken_down_at"],
        "purge_summary": {},
        "repeat_purges": [],
        "cases_count": 0,
        "latest_case_id": None,
    }
    assert entry["activatable"] is False and entry["stored"]["raw_signals"] == 0
    again = world.take_down(BOB, "TKD-PRIV-2")
    assert again["already_taken_down"] is True


def test_suppression_survives_the_retention_sweep_expiring_the_audit_rows(world: World):
    """The audit_logs retention class purges the per-workspace takedown entries (the only
    rows that carry attribution and the case id); the suppression and the operator case
    records are untouched (no retention class covers the case record: pending D-9e)."""
    world.take_down(ALICE, REFERENCE)
    world.take_down(ALICE, REPEAT_REFERENCE)
    with world.session() as s:
        entries = s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        assert len(entries) == 2
        # Older than any cut-off the sweep can compute from the smallest duration.
        s.execute(
            update(AuditLog)
            .where(AuditLog.action == AUDIT_ACTION)
            .values(created_at=datetime.now(UTC) - timedelta(days=400))
        )
        s.commit()
    outcomes = {
        o.data_class: o
        for o in retention.sweep(
            world.factory,
            classes=(RetentionClass.AUDIT_LOGS,),
            settings=Settings(_env_file=None, retention_audit_logs_days=1),
        )
    }
    outcome = outcomes[RetentionClass.AUDIT_LOGS.value]
    assert outcome.status == "purged" and outcome.complete
    assert outcome.deleted.get("audit_logs") == 2
    with world.session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all() == []
        row = s.scalar(select(SourceTakedown).where(SourceTakedown.source_key == KEY))
        assert row is not None and len(row.repeat_purges) == 1
        assert s.scalar(select(func.count()).select_from(SourceTakedownCase)) == 2
        _assert_still_suppressed(s, step="audit rows expired by the retention sweep")


def test_suppression_survives_the_account_deletion_route(world: World):
    """The operator deletes their own account through POST /account/delete. BOB is the
    only member of ORG_B, so the walk erases that organization too (tenant content,
    its audit rows anonymised); the platform-wide suppression record is not reachable
    from either and stays exactly as it was. The case records are kept, actor cleared."""
    first = world.take_down(BOB, REFERENCE)
    second = world.take_down(BOB, REPEAT_REFERENCE)
    before = world.row()
    r = world.env.client.post(
        f"{API}/account/delete",
        headers={"Authorization": f"Bearer {bearer(world.env, BOB)}"},
        json={"current_password": OLD_PASSWORD, "confirmation": "DELETE"},
    )
    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["user_id"] == BOB and receipt["verification"]["references_cleared"] is True
    assert [o["organization_id"] for o in receipt["deleted_organizations"]] == [ORG_B]
    with world.session() as s:
        assert s.get(User, BOB) is None and s.get(Organization, ORG_B) is None
        remaining = s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)).all()
        # Both entries are retained with the actor anonymised; ORG_B's, whose organization
        # was erased with the account, is fully anonymised (context cleared), while ORG_A's
        # still carries the opaque case id.
        assert {e.organization_id for e in remaining} == {ORG_A, ORG_B}
        assert all(e.actor_user_id is None for e in remaining)
        assert all(e.context == {} for e in remaining if e.organization_id == ORG_B)
        assert all(
            e.context["case_id"] == first["case_id"]
            for e in remaining
            if e.organization_id == ORG_A
        )
        cases = s.scalars(select(SourceTakedownCase).order_by(SourceTakedownCase.created_at)).all()
        assert [c.id for c in cases] == [first["case_id"], second["case_id"]]
        assert all(c.actor_user_id is None for c in cases)  # SET NULL; the case is kept
        assert [c.reason for c in cases] == [REASON, REPEAT_REASON]
        assert world.row() == before
        _assert_still_suppressed(s, step="actor erased through the account-deletion route")
    _assert_nothing_identifying(
        world.row(),
        forbidden={BOB, EMAIL[BOB], REFERENCE, REPEAT_REFERENCE, REASON, REPEAT_REASON},
        repeats=1,
    )


def test_the_private_takedown_text_never_reaches_the_organization_or_account_export(
    world: World,
):
    """D-9e: the operator's reason and ticket id are in the case record only. The
    organization export (every page of every exported table) and the account export of the
    operator carry the tenant audit row -- generic sentence and case id -- and nothing the
    operator typed; neither takedown table is exported."""
    taken = world.take_down(ALICE, REFERENCE)
    headers = {"Authorization": f"Bearer {bearer(world.env, ALICE)}"}
    index = world.env.client.get(f"{API}/organizations/{ORG_A}/export", headers=headers)
    assert index.status_code == 200, index.text
    body = index.json()
    assert "source_takedowns" not in body["tables"]
    assert "source_takedown_cases" not in body["tables"]
    assert {"source_takedowns", "source_takedown_cases"} <= set(body["not_exported"])
    pages = [body]
    for table, first in body["tables"].items():
        cursor = first.get("next_cursor")
        while cursor:
            page = world.env.client.get(
                f"{API}/organizations/{ORG_A}/export/{table}",
                headers=headers,
                params={"cursor": cursor, "limit": 5000},
            )
            assert page.status_code == 200, page.text
            pages.append(page.json())
            cursor = page.json().get("next_cursor")
    rendered = json.dumps(pages, default=str)
    # Positive control: the export did carry the tenant audit row of the takedown.
    assert AUDIT_ACTION in rendered and taken["case_id"] in rendered
    for private in (REASON, REFERENCE, "Counsel memo"):
        assert private not in rendered, private
    account = world.env.client.get(f"{API}/account/export", headers=headers)
    assert account.status_code == 200, account.text
    for private in (REASON, REFERENCE, "Counsel memo"):
        assert private not in account.text, private
