"""Project Phase 5E, I-5E fold of the R-5E r1 reviews: what an export serializes with.

``create_export`` takes four row locks before its checks and holds them to commit
(``app.exports.service._lock_for_export``; "Lock order" in ``app.approvals.service``): the
exporter's ``users`` row and the organization (``KEY SHARE``), the draft (``SHARE``) and the
draft's brief (``KEY SHARE``). Before the fold it took none (reviews R-5E-A r1, A-F1; R-5E-B r1,
F-B-1 and F-B-5): an export in flight committed after a committed revocation; an export racing
an organization deletion kept its exporter's identity in a surviving row; with notifications
on, the two deadlocked.

**PostgreSQL** (``TEST_POSTGRES_URL``; unset FAILS in CI and skips elsewhere), a database
migrated to head, every request through its real route in a thread of its own, every wait
bounded. One transaction is held open at a named point (after its writes are flushed, before
it commits), the other is started, and the test waits until PostgreSQL reports the second in a
lock wait on the expected row and statement. Each pair is run in **both orders**, with
notifications off and on for the workspace:

* a rejection or revocation of the draft's approval and an export: the decision first, the
  export waits on the draft and is then refused 409 ``export_not_approved`` and writes nothing;
  the export first, the decision waits on the draft, both commit, and the export's row precedes
  the decision's;
* a decision withdrawing the approval of the draft's brief and an export: the same, on the brief
  row, with 409 ``export_review_blocked`` ("This draft's brief is no longer approved");
* organization deletion and an export by another member: the deletion first, the export waits on
  the organization and answers the uniform 404; the export first, the deletion waits, then
  anonymises the export row and its audit row. No deadlock in either order;
* deletion of the exporter's own account and an export: the erasure first (holding the user row,
  before it reaches the organization), the export waits on the user row holding nothing and
  answers 401; the export first, the erasure waits and then anonymises;
* no cycle with the paths that hold a row the export wants without waiting for it: an override
  write holding the workspace (the export's notification rows wait, then commit), an erasure
  holding the brief ``NO KEY UPDATE`` (the export's ``KEY SHARE`` is admitted), the retention
  purge holding a deleted draft (the export waits on the draft holding no brief lock, then
  answers 404), and a second export of the same draft (``SHARE`` admits ``SHARE``).

**Both engines:** the statement order of an export (the four locks, in order, before the
decision is read), no lock statement while export is dark, and the lock statements' modes as
compiled for PostgreSQL.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects import postgresql

from app.approvals import service as approvals_service
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.capabilities.registry import Capability
from app.capabilities.service import set_capability_override
from app.core.config import get_settings
from app.creative.models import CreativeDraft
from app.exports import service as export_service
from app.exports.models import Export
from app.notifications.models import Notification
from app.organizations.models import Organization, User
from app.privacy import erasure
from app.privacy.inventory import RetentionClass
from app.privacy.retention import purge_batch
from app.privacy.writer import mark_privacy_writer
from app.tests._creative_support import CreativeWorld, creative_world, postgres_creative_world
from app.tests.test_campaign_context_integrity import PASSWORD, _post
from app.tests.test_exports_refusal_preconditions import decide, draft, set_cap

JOIN_SECONDS = 60.0
POLL_SECONDS = 30.0
#: Long enough for PostgreSQL's ``deadlock_timeout`` (1 s) to have aborted one side of a cycle.
PAST_DEADLOCK_TIMEOUT = 1.5
BODY = {"format": "text/plain"}
DRAFT_NOT_FOUND = "Draft not found in this workspace."
BRIEF_NO_LONGER_APPROVED = (
    "This draft's brief is no longer approved, so the draft can't be exported."
)
NOT_APPROVED = {
    "rejected": "This draft version's latest decision is a rejection, so it can't be exported.",
    "revoked": "This draft version's approval was revoked, so it can't be exported.",
}
NOTIFICATIONS = pytest.mark.parametrize(
    "notifications", [False, True], ids=["notifications-off", "notifications-on"]
)
WITHDRAWALS = pytest.mark.parametrize("decision", ["revoked", "rejected"])


# --------------------------------------------------------------------------- #
# Worlds
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("i5exs") / "p.db", "i5exs") as w:
            yield w
        return
    with postgres_creative_world("i5exs") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


@pytest.fixture(scope="module")
def pg() -> Iterator[CreativeWorld]:
    with postgres_creative_world("i5exl") as w:
        # Bounded waits on every connection opened from here on: a lock that is never released
        # fails its statement instead of hanging the module.
        with w.witness.connect() as conn:
            name = conn.execute(text("SELECT current_database()")).scalar_one()
            conn.execute(text(f'ALTER DATABASE "{name}" SET lock_timeout = \'45s\''))
            conn.commit()
        w.engine.dispose()
        w.witness.dispose()
        yield w


@pytest.fixture(autouse=True)
def flags_off(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "notifications_analytics_enabled", False)
    monkeypatch.setattr(settings, "content_export_enabled", False)


# --------------------------------------------------------------------------- #
# Threads, pauses and what PostgreSQL reports
# --------------------------------------------------------------------------- #
class _Runner(threading.Thread):
    def __init__(self, fn: Callable):
        super().__init__(daemon=True)
        self._fn = fn
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:  # pragma: no cover - exercised through outcome()
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            self.error = exc

    def outcome(self, seconds: float = JOIN_SECONDS):
        self.join(seconds)
        assert not self.is_alive(), "a request did not finish"
        if self.error is not None:
            raise self.error
        return self.result


class _Gate:
    """Holds the first transaction that reaches it, at that point, until released."""

    def __init__(self) -> None:
        self.reached, self.release = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._taken = False

    def _first(self) -> bool:
        with self._lock:
            first, self._taken = not self._taken, True
        return first

    def _hold(self) -> None:
        self.reached.set()
        assert self.release.wait(JOIN_SECONDS), "the held transaction was never released"

    def before(self, real: Callable) -> Callable:
        """``real``, held once just before it runs."""

        def held(*args, **kwargs):
            if self._first():
                self._hold()
            return real(*args, **kwargs)

        return held

    def after(self, real: Callable) -> Callable:
        """``real``, held once right after it returned (its writes flushed, not committed)."""

        def held(*args, **kwargs):
            result = real(*args, **kwargs)
            if self._first():
                self._hold()
            return result

        return held

    def wait(self, what: str) -> None:
        assert self.reached.wait(JOIN_SECONDS), f"{what} never reached its hold"


def _lock_waits(w: CreativeWorld) -> list[str]:
    """The statements of the backends PostgreSQL reports in a heavyweight-lock wait."""
    with w.witness.connect() as conn:
        rows = conn.execute(text(
            "SELECT query FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )).scalars().all()
    return [" ".join(q.split()) for q in rows]


def _wait_for_lock_wait(w: CreativeWorld, waiter: _Runner, what: str, *needles: str) -> str:
    """The one statement in a lock wait, once there is one. It must contain every needle:
    a transaction waiting anywhere else fails the test at once, with the statement, and so
    does a ``waiter`` that finishes without ever having waited."""
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        waits = _lock_waits(w)
        if waits:
            assert len(waits) == 1, waits
            assert all(n in waits[0] for n in needles), f"{what}: waiting elsewhere: {waits[0]}"
            return waits[0]
        if not waiter.is_alive():
            raise AssertionError(
                f"{what}: it finished without waiting ({waiter.error or waiter.result!r})"
            )
        time.sleep(0.05)
    raise AssertionError(f"timed out after {POLL_SECONDS} s waiting for {what}")


def _still_waiting(w: CreativeWorld, statement: str) -> None:
    """Past ``deadlock_timeout`` the same statement still waits: a wait, not a cycle."""
    time.sleep(PAST_DEADLOCK_TIMEOUT)
    assert _lock_waits(w) == [statement]


# --------------------------------------------------------------------------- #
# Seeding and requests
# --------------------------------------------------------------------------- #
def _fresh_organization(base: CreativeWorld, key: str) -> CreativeWorld:
    """A new organization on ``base``'s database with an owner, an admin, a marketer and a
    reviewer and one onboarded workspace, as a world of its own (deleting it, or one of its
    accounts, leaves ``base`` whole). ``w.name`` is the organization's name."""
    tag = uuid.uuid4().hex[:8]
    name = f"I5E organization {key} {tag}"
    owner = _post(base.client, "/auth/register", {
        "email": f"i5e-{key}-{tag}-owner@example.com", "full_name": f"{key} owner",
        "password": PASSWORD, "organization_name": name,
    })
    w = CreativeWorld(base.client, base.engine, base.witness, base.factory)
    w.org = owner["memberships"][0]["organization_id"]
    w.tokens["owner"], w.users["owner"] = owner["access_token"], owner["user"]["id"]
    for role in ("admin", "marketer", "reviewer"):
        invitation = _post(base.client, f"/organizations/{w.org}/invitations",
                           {"email": f"i5e-{key}-{tag}-{role}@example.com", "role": role},
                           w.tokens["owner"])
        joined = _post(base.client, "/auth/invitations/register",
                       {"token": invitation["token"], "full_name": f"{role} {tag}",
                        "password": PASSWORD})
        w.tokens[role], w.users[role] = joined["access_token"], joined["user"]["id"]
    w.ws = w.workspace(f"i5e {key}", enabled=False)
    w.name = name
    return w


def _exportable(w: CreativeWorld, ws: str, *, notifications: bool) -> tuple[str, str]:
    """``(draft id, approval id)``: an approved draft of an approved brief with a passed
    review, by the marketer, approved by the reviewer; export on for the workspace."""
    set_cap(w, ws, "content_export", True)
    if notifications:
        set_cap(w, ws, "notifications_analytics", True)
    made = draft(w, ws)
    decide(w, made, "approved")
    [approval] = w.rows(Approval, subject_id=made)
    return made, approval["id"]


def _export(w: CreativeWorld, ws: str, draft_id: str, role: str = "admin"):
    return w.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/exports", role, BODY)


def _decide_draft(w: CreativeWorld, ws: str, draft_id: str, decision: str):
    return w.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/approvals", "reviewer",
                  {"decision": decision, "subject_version": 1})


def _decide_brief(w: CreativeWorld, ws: str, draft_id: str, decision: str):
    made = w.row(CreativeDraft, draft_id)
    return w.send(
        "POST",
        f"/workspaces/{ws}/opportunities/{made['opportunity_id']}/briefs/{made['brief_id']}"
        "/approvals",
        "reviewer", {"decision": decision, "subject_version": 1},
    )


def _error(response) -> tuple[int, str, str]:
    body = response.json()["error"]
    return response.status_code, body["code"], body["message"]


def _export_records(w: CreativeWorld, **where) -> tuple[list[dict], list[dict]]:
    """The export rows and the export audit rows matching ``where`` (scope columns)."""
    return (
        w.rows(Export, **where),
        w.rows(AuditLog, action="creative_draft.exported", **where),
    )


def _names_nobody(user_id: str, *records: dict) -> None:
    """No column of the surviving records carries the user's id (the snapshot leads with it)."""
    for record in records:
        for column, value in record.items():
            assert user_id not in str(value), (column, value)


def _export_notifications(w: CreativeWorld, ws: str) -> int:
    return w.count(Notification, workspace_id=ws, event_code="export.completed")


# --------------------------------------------------------------------------- #
# A decision on the draft and an export
# --------------------------------------------------------------------------- #
@NOTIFICATIONS
@WITHDRAWALS
def test_a_draft_decision_first_the_export_waits_and_is_refused(
    pg: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = pg.workspace("i5e draft first")  # creative generation on: the draft decision route
    made, _approval = _exportable(pg, ws, notifications=notifications)
    gate = _Gate()
    monkeypatch.setattr(approvals_service, "notify", gate.before(approvals_service.notify))
    deciding = _Runner(lambda: _decide_draft(pg, ws, made, decision))
    deciding.start()
    try:
        gate.wait("the decision")  # its row and its audit row are flushed, not committed
        exporting = _Runner(lambda: _export(pg, ws, made))
        exporting.start()
        # The export does not read the uncommitted state and go on: it waits on the draft row.
        waiting = _wait_for_lock_wait(
            pg, exporting, "the export to wait on the draft the decision holds",
            "FROM creative_drafts", "FOR SHARE",
        )
        _still_waiting(pg, waiting)
        assert _export_records(pg, workspace_id=ws) == ([], [])
    finally:
        gate.release.set()
    assert deciding.outcome().status_code == 201
    refused = exporting.outcome()
    assert _error(refused) == (409, "export_not_approved", NOT_APPROVED[decision])
    assert _export_records(pg, workspace_id=ws) == ([], [])
    assert _export_notifications(pg, ws) == 0


@NOTIFICATIONS
@WITHDRAWALS
def test_an_export_first_the_draft_decision_waits_until_it_has_committed(
    pg: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = pg.workspace("i5e export first")  # creative generation on: the draft decision route
    made, approval_id = _exportable(pg, ws, notifications=notifications)
    gate = _Gate()
    monkeypatch.setattr(export_service, "notify", gate.before(export_service.notify))
    exporting = _Runner(lambda: _export(pg, ws, made))
    exporting.start()
    try:
        gate.wait("the export")  # its row and its audit row are flushed, not committed
        deciding = _Runner(lambda: _decide_draft(pg, ws, made, decision))
        deciding.start()
        waiting = _wait_for_lock_wait(
            pg, deciding, "the decision to wait on the draft the export holds",
            "FROM creative_drafts", "FOR NO KEY UPDATE",
        )
        _still_waiting(pg, waiting)
        # Nothing of the decision exists while the export is uncommitted.
        assert [a["decision"] for a in pg.rows(Approval, subject_id=made)] == ["approved"]
    finally:
        gate.release.set()
    assert exporting.outcome().status_code == 201
    assert deciding.outcome().status_code == 201
    [exported], [audited] = _export_records(pg, workspace_id=ws)
    later = next(a for a in pg.rows(Approval, subject_id=made) if a["decision"] == decision)
    assert exported["approval_id"] == approval_id
    assert audited["new_state"]["approval_id"] == approval_id
    # The order the records state is the order that happened: export, then decision.
    assert exported["created_at"] <= later["created_at"]
    assert _export_notifications(pg, ws) == (2 if notifications else 0)
    # And the next export is refused.
    assert _error(_export(pg, ws, made)) == (409, "export_not_approved", NOT_APPROVED[decision])
    assert len(pg.rows(Export, workspace_id=ws)) == 1


# --------------------------------------------------------------------------- #
# A decision on the draft's brief and an export
# --------------------------------------------------------------------------- #
@NOTIFICATIONS
@WITHDRAWALS
def test_a_brief_decision_first_the_export_waits_and_is_refused(
    pg: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = pg.workspace("i5e brief first", enabled=False)
    set_cap(pg, ws, "recommendation_briefs", True)
    made, _approval = _exportable(pg, ws, notifications=notifications)
    gate = _Gate()
    monkeypatch.setattr(approvals_service, "notify", gate.before(approvals_service.notify))
    deciding = _Runner(lambda: _decide_brief(pg, ws, made, decision))
    deciding.start()
    try:
        gate.wait("the brief decision")
        exporting = _Runner(lambda: _export(pg, ws, made))
        exporting.start()
        waiting = _wait_for_lock_wait(
            pg, exporting, "the export to wait on the brief the decision holds",
            "FROM recommendation_briefs", "FOR KEY SHARE",
        )
        _still_waiting(pg, waiting)
        assert _export_records(pg, workspace_id=ws) == ([], [])
    finally:
        gate.release.set()
    assert deciding.outcome().status_code == 201
    refused = exporting.outcome()
    assert _error(refused) == (409, "export_review_blocked", BRIEF_NO_LONGER_APPROVED)
    assert _export_records(pg, workspace_id=ws) == ([], [])
    assert _export_notifications(pg, ws) == 0
    # The draft's own approval stands (item 38): only the export is refused.
    assert [a["decision"] for a in pg.rows(Approval, subject_id=made)] == ["approved"]


@NOTIFICATIONS
@WITHDRAWALS
def test_an_export_first_the_brief_decision_waits_until_it_has_committed(
    pg: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = pg.workspace("i5e export first b", enabled=False)
    set_cap(pg, ws, "recommendation_briefs", True)
    made, approval_id = _exportable(pg, ws, notifications=notifications)
    brief_id = pg.row(CreativeDraft, made)["brief_id"]
    gate = _Gate()
    monkeypatch.setattr(export_service, "notify", gate.before(export_service.notify))
    exporting = _Runner(lambda: _export(pg, ws, made))
    exporting.start()
    try:
        gate.wait("the export")
        deciding = _Runner(lambda: _decide_brief(pg, ws, made, decision))
        deciding.start()
        waiting = _wait_for_lock_wait(
            pg, deciding, "the decision to wait on the brief the export holds",
            "FROM recommendation_briefs", "FOR UPDATE",
        )
        _still_waiting(pg, waiting)
        assert [a["decision"] for a in pg.rows(Approval, subject_id=brief_id)] == ["approved"]
    finally:
        gate.release.set()
    assert exporting.outcome().status_code == 201
    assert deciding.outcome().status_code == 201
    [exported], [_audited] = _export_records(pg, workspace_id=ws)
    later = next(a for a in pg.rows(Approval, subject_id=brief_id) if a["decision"] == decision)
    assert exported["approval_id"] == approval_id
    assert exported["created_at"] <= later["created_at"]
    assert _error(_export(pg, ws, made)) == (
        409, "export_review_blocked", BRIEF_NO_LONGER_APPROVED
    )
    assert len(pg.rows(Export, workspace_id=ws)) == 1


# --------------------------------------------------------------------------- #
# Organization deletion and an export by another member
# --------------------------------------------------------------------------- #
def _delete_organization(w: CreativeWorld):
    return w.send("POST", f"/organizations/{w.org}/delete", "owner",
                  {"current_password": PASSWORD, "confirm_organization_name": w.name})


def _exists(w: CreativeWorld, model, row_id: str) -> bool:
    return w.row(model, row_id) is not None


@NOTIFICATIONS
def test_an_organization_deletion_first_the_export_waits_and_answers_404(
    pg: CreativeWorld, monkeypatch, notifications: bool
):
    org = _fresh_organization(pg, "delfirst")
    made, _approval = _exportable(org, org.ws, notifications=notifications)
    gate = _Gate()
    monkeypatch.setattr(erasure, "erase_organization", gate.after(erasure.erase_organization))
    deleting = _Runner(lambda: _delete_organization(org))
    deleting.start()
    try:
        gate.wait("the deletion")  # everything of the organization is deleted, not committed
        exporting = _Runner(lambda: _export(org, org.ws, made))
        exporting.start()
        waiting = _wait_for_lock_wait(
            pg, exporting, "the export to wait on the organization the deletion holds",
            "FROM organizations", "FOR KEY SHARE",
        )
        _still_waiting(pg, waiting)
    finally:
        gate.release.set()
    deleted = deleting.outcome()
    assert deleted.status_code == 200, deleted.text
    assert _error(exporting.outcome()) == (404, "not_found", DRAFT_NOT_FOUND)
    assert not _exists(pg, Organization, org.org)
    assert _export_records(pg, organization_id=org.org) == ([], [])
    assert pg.count(Notification, organization_id=org.org) == 0


@NOTIFICATIONS
def test_an_export_first_the_organization_deletion_waits_and_anonymises_it(
    pg: CreativeWorld, monkeypatch, notifications: bool
):
    """F-B-1 (i) and (ii): the export's row and audit row do not keep the exporter's identity
    after the organization is deleted, and with a notification recipient who is not the
    deleting owner the two transactions do not deadlock."""
    org = _fresh_organization(pg, "expfirst")
    made, approval_id = _exportable(org, org.ws, notifications=notifications)
    exporter = org.users["admin"]
    gate = _Gate()
    monkeypatch.setattr(export_service, "notify", gate.before(export_service.notify))
    exporting = _Runner(lambda: _export(org, org.ws, made))
    exporting.start()
    try:
        gate.wait("the export")  # its row and audit row are flushed, not committed
        deleting = _Runner(lambda: _delete_organization(org))
        deleting.start()
        # The deletion stops at its first lock on the organization, before it anonymises
        # anything: it cannot miss the export's rows.
        waiting = _wait_for_lock_wait(
            pg, deleting, "the deletion to wait on the organization the export holds",
            "FROM organizations", "FOR UPDATE",
        )
        _still_waiting(pg, waiting)
    finally:
        gate.release.set()
    exported = exporting.outcome()
    assert exported.status_code == 201, exported.text
    deleted = deleting.outcome()  # re-raises a deadlock (40P01) or a lock timeout
    assert deleted.status_code == 200, deleted.text
    assert not _exists(pg, Organization, org.org)
    assert _exists(pg, User, exporter)  # members' accounts are not deleted
    [row], [audited] = _export_records(pg, organization_id=org.org)
    assert (row["id"], row["exported_by"], row["exported_by_snapshot"], row["draft_id"]) == (
        exported.json()["id"], None, "[erased]", None
    )
    assert (row["approval_id"], row["draft_version"]) == (approval_id, 1)  # the record stays
    assert (
        audited["actor_user_id"], audited["entity_id"], audited["new_state"], audited["context"]
    ) == (None, None, None, {})
    _names_nobody(exporter, row, audited)
    assert pg.count(Notification, organization_id=org.org) == 0  # gone with their workspace


# --------------------------------------------------------------------------- #
# Deletion of the exporter's own account and an export
# --------------------------------------------------------------------------- #
def _delete_account(w: CreativeWorld, role: str):
    return w.send("POST", "/account/delete", role,
                  {"current_password": PASSWORD, "confirmation": "DELETE"})


@NOTIFICATIONS
def test_the_exporters_account_erasure_first_the_export_waits_holding_nothing(
    pg: CreativeWorld, monkeypatch, notifications: bool
):
    """The erasure holds the user row and has not reached the organization yet. The export
    must meet it at the user row, its first lock, holding nothing the erasure needs: taken
    only at the insert's foreign key, after the organization, the two would form a cycle."""
    org = _fresh_organization(pg, "erafirst")
    made, _approval = _exportable(org, org.ws, notifications=notifications)
    exporter = org.users["admin"]
    gate = _Gate()
    monkeypatch.setattr(erasure, "lock_organization", gate.before(erasure.lock_organization))
    erasing = _Runner(lambda: _delete_account(org, "admin"))
    erasing.start()
    try:
        gate.wait("the erasure")  # user row FOR UPDATE; no organization locked yet
        exporting = _Runner(lambda: _export(org, org.ws, made))
        exporting.start()
        waiting = _wait_for_lock_wait(
            pg, exporting, "the export to wait on the exporter's user row", "FROM users",
            "FOR KEY SHARE",
        )
        _still_waiting(pg, waiting)
    finally:
        gate.release.set()
    erased = erasing.outcome()  # re-raises a deadlock (40P01) or a lock timeout
    assert erased.status_code == 200, erased.text
    assert not _exists(pg, User, exporter)
    refused = exporting.outcome()
    assert _error(refused) == (401, "unauthorized", "Invalid or expired token.")
    assert _export_records(pg, organization_id=org.org) == ([], [])
    assert _exists(pg, Organization, org.org)  # other members remain


@NOTIFICATIONS
def test_an_export_first_the_exporters_account_erasure_waits_and_anonymises_it(
    pg: CreativeWorld, monkeypatch, notifications: bool
):
    org = _fresh_organization(pg, "expfirsta")
    made, approval_id = _exportable(org, org.ws, notifications=notifications)
    exporter = org.users["admin"]
    gate = _Gate()
    monkeypatch.setattr(export_service, "notify", gate.before(export_service.notify))
    exporting = _Runner(lambda: _export(org, org.ws, made))
    exporting.start()
    try:
        gate.wait("the export")
        erasing = _Runner(lambda: _delete_account(org, "admin"))
        erasing.start()
        waiting = _wait_for_lock_wait(
            pg, erasing, "the erasure to wait on the user row the export holds", "FROM users",
            "FOR UPDATE",
        )
        _still_waiting(pg, waiting)
    finally:
        gate.release.set()
    exported = exporting.outcome()
    assert exported.status_code == 201, exported.text
    erased = erasing.outcome()
    assert erased.status_code == 200, erased.text
    assert not _exists(pg, User, exporter)
    [row], [audited] = _export_records(pg, organization_id=org.org)
    assert (row["exported_by"], row["exported_by_snapshot"]) == (None, "[erased]")
    assert (row["draft_id"], row["approval_id"]) == (made, approval_id)  # the record stays
    assert audited["actor_user_id"] is None
    _names_nobody(exporter, row, audited)


# --------------------------------------------------------------------------- #
# Paths that hold a row the export wants and do not wait for the export
# --------------------------------------------------------------------------- #
def test_an_override_write_holding_the_workspace_delays_the_export_and_no_cycle_forms(
    pg: CreativeWorld,
):
    """With notifications on, the export's notification rows take the workspace ``KEY SHARE``.
    A capability-override write holds it ``FOR UPDATE``: the export waits for it, holding its
    four locks, and commits with its notifications once the write has."""
    ws = pg.workspace("i5e override", enabled=False)
    made, _approval = _exportable(pg, ws, notifications=True)
    writer = pg.factory()
    try:
        set_capability_override(
            writer, organization_id=pg.org, workspace_id=ws,
            capability=Capability.OPPORTUNITY_FEEDBACK, enabled=True,
            actor_user_id=pg.users["owner"], reason="I-5E export serialization",
        )
        writer.flush()
        exporting = _Runner(lambda: _export(pg, ws, made))
        exporting.start()
        waiting = _wait_for_lock_wait(
            pg, exporting, "the export's notification rows to wait on the workspace",
            "INSERT INTO notifications",
        )
        _still_waiting(pg, waiting)
        writer.commit()
    finally:
        writer.rollback()
        writer.close()
    assert exporting.outcome().status_code == 201
    assert len(pg.rows(Export, workspace_id=ws)) == 1
    assert _export_notifications(pg, ws) == 2  # the draft's author and its approver


def test_an_erasure_holding_the_brief_never_makes_the_export_wait(pg: CreativeWorld):
    """Account erasure updates the author's briefs, then their drafts (``NO KEY UPDATE``
    each). Held between the two, it holds the brief: the export's ``KEY SHARE`` is admitted,
    so the export commits without waiting, and the erasure then updates the draft. (``SHARE``
    on the brief would wait, and the erasure would then wait on the export's draft: 40P01.)"""
    org = _fresh_organization(pg, "brief")
    made, _approval = _exportable(org, org.ws, notifications=False)
    briefed, go = threading.Event(), threading.Event()

    def erase() -> dict:
        with pg.factory() as s:
            mark_privacy_writer(s)
            conn = s.connection()

            def hold(conn, cursor, statement, parameters, context, executemany):
                if statement.lstrip().upper().startswith("UPDATE CREATIVE_DRAFTS"):
                    if not briefed.is_set():
                        briefed.set()
                        assert go.wait(JOIN_SECONDS)

            event.listen(conn, "before_cursor_execute", hold)
            counts = erasure.anonymise_phase5_records_for_subject(
                s, user_id=org.users["marketer"], email=None
            )
            s.commit()
            return dict(counts)

    erasing = _Runner(erase)
    erasing.start()
    try:
        assert briefed.wait(JOIN_SECONDS), "the erasure never reached the drafts"
        exporting = _Runner(lambda: _export(org, org.ws, made))
        exporting.start()
        exporting.join(15.0)
        finished, waits = not exporting.is_alive(), _lock_waits(pg)
    finally:
        go.set()
    assert finished, f"the export waited on the brief the erasure holds: {waits}"
    assert exporting.outcome().status_code == 201
    assert erasing.outcome() == {"recommendation_briefs": 1, "creative_drafts": 1}
    assert len(pg.rows(Export, organization_id=org.org)) == 1


def test_the_opportunity_purge_first_the_export_waits_on_the_draft_and_answers_404(
    pg: CreativeWorld, monkeypatch
):
    """The retention purge's foreign-key walk deletes a brief's drafts before the brief. Held
    right after the drafts are deleted, it holds the draft: the export waits on the draft row
    (its third lock) holding no brief lock, so the purge deletes the brief and commits, and the
    export finds no draft: the uniform 404, nothing written. (A brief lock taken before the
    draft would make a cycle.)"""
    ws = pg.workspace("i5e purge", enabled=False)
    made, _approval = _exportable(pg, ws, notifications=False)
    deleted, resume = threading.Event(), threading.Event()
    real = erasure.delete_rows

    def delete_rows(db, table, ids):
        counts = real(db, table, ids)
        if table.name == "creative_drafts" and not deleted.is_set():
            deleted.set()
            assert resume.wait(JOIN_SECONDS)
        return counts

    monkeypatch.setattr(erasure, "delete_rows", delete_rows)

    def purge() -> dict:
        with pg.factory() as s:
            batch = purge_batch(s, RetentionClass.OPPORTUNITIES,
                                datetime.now(UTC) + timedelta(days=1), batch_size=10,
                                workspace_id=ws)
            s.commit()
            return dict(batch.counts.deleted)

    purging = _Runner(purge)
    purging.start()
    try:
        assert deleted.wait(JOIN_SECONDS), "the purge never deleted the drafts"
        exporting = _Runner(lambda: _export(pg, ws, made))
        exporting.start()
        waiting = _wait_for_lock_wait(
            pg, exporting, "the export to wait on the draft the purge deleted",
            "FROM creative_drafts", "FOR SHARE",
        )
        _still_waiting(pg, waiting)
    finally:
        resume.set()
    counts = purging.outcome()  # re-raises a deadlock (40P01) or a lock timeout
    assert (counts["recommendation_briefs"], counts["creative_drafts"]) == (1, 1)
    assert _error(exporting.outcome()) == (404, "not_found", DRAFT_NOT_FOUND)
    assert _export_records(pg, workspace_id=ws) == ([], [])


def test_two_exports_of_one_draft_do_not_wait_for_each_other(pg: CreativeWorld, monkeypatch):
    ws = pg.workspace("i5e two exports", enabled=False)
    made, _approval = _exportable(pg, ws, notifications=False)
    gate = _Gate()
    monkeypatch.setattr(export_service, "notify", gate.before(export_service.notify))
    first = _Runner(lambda: _export(pg, ws, made))
    first.start()
    try:
        gate.wait("the first export")  # holds its four locks, uncommitted
        second = _Runner(lambda: _export(pg, ws, made, "owner"))
        second.start()
        second.join(15.0)
        finished, waits = not second.is_alive(), _lock_waits(pg)
    finally:
        gate.release.set()
    assert finished, f"the second export waited for the first: {waits}"
    assert second.outcome().status_code == 201
    assert first.outcome().status_code == 201
    assert len(pg.rows(Export, workspace_id=ws)) == 2


# --------------------------------------------------------------------------- #
# Both engines: statement order, the dark state, the compiled modes
# --------------------------------------------------------------------------- #
LOCKS = (
    ("select users.id from users where users.id =", "for key share"),
    ("select organizations.id from organizations where organizations.id =", "for key share"),
    ("select creative_drafts.id from creative_drafts where creative_drafts.id =", "for share"),
    ("select recommendation_briefs.id from recommendation_briefs where", "for key share"),
)


def _statements(world: CreativeWorld, request: Callable):
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).lower())

    event.listen(world.engine, "before_cursor_execute", record)
    try:
        response = request()
    finally:
        event.remove(world.engine, "before_cursor_execute", record)
    return response, seen


def _positions(statements: list[str]) -> list[int]:
    found = []
    for prefix, _mode in LOCKS:
        hits = [i for i, q in enumerate(statements) if q.startswith(prefix)]
        assert len(hits) == 1, (prefix, statements)
        found.append(hits[0])
    return found


@pytest.mark.parametrize("approved", [True, False], ids=["exported", "refused"])
def test_the_four_locks_come_in_order_before_the_decision_is_read(
    world: CreativeWorld, approved: bool
):
    """User, organization, draft, brief; then the controlling decision. A refused export has
    taken the same locks and written nothing."""
    ws = world.workspace("i5e order", enabled=False)
    set_cap(world, ws, "content_export", True)
    made = draft(world, ws)
    if approved:
        decide(world, made, "approved")
    response, seen = _statements(world, lambda: _export(world, ws, made))
    assert response.status_code == (201 if approved else 409), response.text
    user, organization, draft_lock, brief_lock = _positions(seen)
    decision_read = next(
        i for i, q in enumerate(seen)
        if q.startswith("select approvals.") and "order by approvals.decision_seq desc" in q
    )
    assert user < organization < draft_lock < brief_lock < decision_read, seen
    writes = [q for q in seen if q.startswith(("insert", "update", "delete"))]
    if approved:
        assert all(i > brief_lock for i, q in enumerate(seen) if q.startswith("insert"))
        assert any(q.startswith("insert into exports") for q in writes)
    else:
        assert writes == []
    if world.engine.dialect.name == "postgresql":
        for position, (_prefix, mode) in zip(
            (user, organization, draft_lock, brief_lock), LOCKS, strict=True
        ):
            assert seen[position].endswith(mode), seen[position]
        assert not seen[draft_lock].endswith("for key share")


def test_a_dark_export_takes_no_lock(world: CreativeWorld):
    """The capability is decided before any lock: a dark workspace's export request issues none
    of the four lock statements (and so cannot delay a decision or a deletion)."""
    ws = world.workspace("i5e dark", enabled=False)
    made = draft(world, ws)
    decide(world, made, "approved")
    response, seen = _statements(world, lambda: _export(world, ws, made))
    assert response.status_code == 503, response.text
    for prefix, _mode in LOCKS:
        assert not [q for q in seen if q.startswith(prefix)], prefix
    assert not [q for q in seen if " for share" in q or " for key share" in q or " for update" in q]


def test_the_export_lock_statements_compile_to_their_modes():
    dialect = postgresql.dialect()
    draft_lock = str(export_service.export_draft_lock_select("d").compile(dialect=dialect))
    assert draft_lock.rstrip().endswith("FOR SHARE") and "FROM creative_drafts" in draft_lock
    # The other three are a decision's own statements (steps 1, 2 and 5 of "Lock order").
    for select_, table in (
        (approvals_service.approver_user_lock_select("u"), "users"),
        (approvals_service.approver_organization_lock_select("o"), "organizations"),
        (approvals_service.draft_brief_lock_select("b"), "recommendation_briefs"),
    ):
        compiled = str(select_.compile(dialect=dialect))
        assert compiled.rstrip().endswith("FOR KEY SHARE") and f"FROM {table}" in compiled
