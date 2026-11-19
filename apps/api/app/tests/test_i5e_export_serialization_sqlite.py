"""Project Phase 5E, the SQLite fold: what an export serializes with on SQLite.

The operator ruled (2026-11-19) that the export's serialization and erasure requirements hold
wherever the capability can be enabled, SQLite included. On SQLite every row-lock statement of
``app.exports.service._lock_for_export`` compiles to a plain read, and a read holds nothing, so
before this fold nothing was held between an export's checks and its first insert: an export
could commit after a committed revocation of its draft's approval or withdrawal of its brief's
approval, and an export racing an organization deletion or the erasure of its exporter's account
answered a server error instead of the 404 or 401 the absence has.
``app.exports.service.create_export`` takes SQLite's writer lock
(``app.db.sqlite_writer.hold_sqlite_writer_lock``, ``BEGIN IMMEDIATE``) after the role and the
capability and before the first of those reads, and the request's transaction holds it to its
end.

**The engine is the application's own.** Each world is a SQLite *file* opened by
``app.db.session.build_engine`` from a real ``Settings`` (``test_the_world_is_the_applications
_own_sqlite_engine`` pins what that means: rollback-journal mode, ``busy_timeout=5000``, the
driver's default transaction control, foreign keys on). Every request goes through its real
route with a session and connection of its own; the server's own answer is read (a server error
is the application's 500, never an exception raised into the test).

**Forced interleavings, both orders, for each of four competitors** (a rejection or revocation
of the draft's approval; a decision withdrawing the brief's approval; organization deletion;
erasure of the exporter's account), with notifications off and on for the workspace:

* *the competitor first*: it is held after its writes are flushed and before it commits; the
  export is started and observed waiting in a statement; the competitor is released. The export
  must answer what the committed competitor means (409, 409, 404, 401) and write nothing;
* *the export first*: it is held after its last check and before its first insert (the point at
  which the SQLite probes of review R-5E-A, P1 and P2, showed the defect); the competitor is
  started and must be observed waiting in a write, with nothing of it committed; the export is
  released. Both commit, the export first; a deletion or erasure then finds the export's rows
  and anonymises them. (One difference from PostgreSQL is pinned as it is:
  :data:`STAMPED_BEFORE_IT_WAITED`.)

Each test asserts the **final committed state** as read by a separate connection (the rows, their
order, and that no column of a surviving ``exports``, ``audit_logs`` or ``notifications`` row
names the erased exporter), in one comparison, so a failure states everything that was observed.

**The erasure path takes the same lock.** Account erasure and the operator's invitee-email
erasure read which rows name their subject and then update each. For an account whose erasure
writes nothing before that read (no accepted invitation, no audit row naming it), the read ran
while an export was uncommitted and missed its row, and ``exported_by_snapshot`` kept the erased
account's id, name and address. Both now take the writer lock before those reads
(``app.privacy.erasure``); both orders are forced here, and a deletion with a wrong password
takes nothing.

**The mechanism's own facts:** a dark request and a request without the exporter role issue no
``BEGIN IMMEDIATE`` and do not wait for a writer; the statement order of an export; a caller whose
transaction has already written is not refused; PostgreSQL never sees the statement. **Its
consequences as measured:** when SQLite's busy timeout expires, the waiting side answers the
application's 500 and writes nothing, and the other side is unaffected.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

from app.approvals import service as approvals_service
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.dependencies import TenantContext
from app.core.config import Settings, get_settings
from app.core.enums import ExportFormat, Role
from app.creative.models import CreativeDraft
from app.db.models import Base
from app.db.session import build_engine
from app.exports import service as export_service
from app.exports.models import Export
from app.main import app
from app.notifications.models import Notification
from app.notifications.service import emit_notifications
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.privacy import erasure
from app.tests._creative_support import (
    CreativeWorld,
    _creative_world,
    postgres_creative_world,
)
from app.tests.test_campaign_context_integrity import PASSWORD
from app.tests.test_exports_refusal_preconditions import decide, draft, set_cap
from app.tests.test_i5e_export_serialization import (
    BRIEF_NO_LONGER_APPROVED,
    DRAFT_NOT_FOUND,
    LOCKS,
    NOT_APPROVED,
    _decide_brief,
    _decide_draft,
    _delete_account,
    _delete_organization,
    _exists,
    _export,
    _export_notifications,
    _export_records,
    _exportable,
    _fresh_organization,
    _Gate,
    _Runner,
)

#: A statement that has not returned for this long is waiting for SQLite's writer lock (the
#: other transaction is held, so nothing else can keep it). Well inside the busy timeout.
BLOCKED_SECONDS = 0.75
POLL_SECONDS = 30.0
#: The application's busy timeout (``app.db.session.build_engine``), in seconds.
BUSY_TIMEOUT_SECONDS = 5.0
SERIALIZE = "begin immediate"
INTERNAL_ERROR = (500, "internal_error", "An unexpected error occurred")
ACCOUNT_GONE = (401, "unauthorized", "Invalid or expired token.")
NOTIFICATIONS = pytest.mark.parametrize(
    "notifications", [False, True], ids=["notifications-off", "notifications-on"]
)
WITHDRAWALS = pytest.mark.parametrize("decision", ["revoked", "rejected"])
#: A difference from PostgreSQL, stated and pinned. A decision's row gets its ``created_at`` when
#: it is flushed, which on SQLite is before its insert waits for the writer lock (on PostgreSQL
#: the decision waits earlier, at its row lock, before the row exists). So when a decision waited
#: for an export, the commit order is export, then decision, and the two ``created_at`` values
#: can state the reverse, by no more than the time the decision waited. In these tests the
#: export is held before its insert, so they always do.
STAMPED_BEFORE_IT_WAITED = "the decision's created_at (set before it waited) <= the export's"


# --------------------------------------------------------------------------- #
# The world: the application's own SQLite engine on a file
# --------------------------------------------------------------------------- #
@contextmanager
def application_sqlite_world(db_file: Path, key: str) -> Iterator[CreativeWorld]:
    """The 5C world on a SQLite file opened by the application's own engine factory.

    ``creative_world`` builds its engine by hand (no busy timeout); this one calls
    ``app.db.session.build_engine`` with a validated ``Settings`` whose only difference from the
    process settings is the database file. The witness is a second engine of the same kind."""
    settings = Settings(database_url=f"sqlite:///{db_file}")
    request_engine, witness = build_engine(settings), build_engine(settings)
    Base.metadata.create_all(request_engine)
    try:
        with _creative_world(request_engine, witness, key) as w:
            # The server's own answer for an unhandled error (the generic 500), not the
            # exception re-raised into the test.
            w.client = TestClient(app, raise_server_exceptions=False)
            yield w
    finally:
        request_engine.dispose()
        witness.dispose()


@pytest.fixture(scope="module")
def lite(tmp_path_factory) -> Iterator[CreativeWorld]:
    with application_sqlite_world(tmp_path_factory.mktemp("i5esq") / "app.db", "i5esq") as w:
        yield w


@pytest.fixture(autouse=True)
def flags_off(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "notifications_analytics_enabled", False)
    monkeypatch.setattr(settings, "content_export_enabled", False)


def test_the_world_is_the_applications_own_sqlite_engine(lite: CreativeWorld):
    """What "the application's real SQLite settings" are, read from the engine the tests use."""
    for engine in (lite.engine, lite.witness):
        assert engine.dialect.name == "sqlite" and engine.dialect.driver == "pysqlite"
        assert isinstance(engine.pool, QueuePool)
        assert engine.url.database.endswith("app.db")  # a file, not memory
        with engine.connect() as conn:
            raw = conn.connection.dbapi_connection
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "delete"
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            # The driver's default: it opens a transaction only before a write statement.
            assert raw.isolation_level == ""
            assert raw.in_transaction is False
    a, b = lite.factory(), lite.factory()
    try:  # separate sessions are separate connections
        assert (
            a.connection().connection.dbapi_connection
            is not b.connection().connection.dbapi_connection
        )
    finally:
        a.close()
        b.close()


# --------------------------------------------------------------------------- #
# What the application engine is sending, and what is waiting
# --------------------------------------------------------------------------- #
class _Trace:
    """Every statement sent on the application engine while it is open, and the ones that have
    been sent and have not returned (with the time they were sent)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._lock = threading.Lock()
        self.sent: list[str] = []
        self._pending: dict[int, tuple[str, float, int]] = {}

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        text_ = " ".join(statement.split()).lower()
        with self._lock:
            self._pending[id(cursor)] = (text_, time.monotonic(), len(self.sent))
            self.sent.append(text_)

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        with self._lock:
            self._pending.pop(id(cursor), None)

    def _error(self, context) -> None:
        with self._lock:
            self._pending.pop(id(context.cursor), None)

    def __enter__(self) -> _Trace:
        event.listen(self._engine, "before_cursor_execute", self._before)
        event.listen(self._engine, "after_cursor_execute", self._after)
        event.listen(self._engine, "handle_error", self._error)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._before)
        event.remove(self._engine, "after_cursor_execute", self._after)
        event.remove(self._engine, "handle_error", self._error)

    def waiting(self, runner: _Runner) -> tuple[str | None, list[str]]:
        """``(statement, sent before it)`` once a statement has been pending for
        :data:`BLOCKED_SECONDS`; ``(None, sent)`` if ``runner`` finished without that."""
        deadline = time.monotonic() + POLL_SECONDS
        while time.monotonic() < deadline:
            now = time.monotonic()
            with self._lock:
                stuck = [
                    (statement, position)
                    for statement, since, position in self._pending.values()
                    if now - since >= BLOCKED_SECONDS
                ]
                sent = list(self.sent)
            if stuck:
                assert len(stuck) == 1, stuck
                return stuck[0][0], sent[: stuck[0][1]]
            if not runner.is_alive():
                return None, sent
            time.sleep(0.02)
        raise AssertionError("neither a waiting statement nor an answer")


def _is_write(statement: str | None) -> bool:
    return statement is not None and statement.startswith(("insert", "update", "delete"))


def _reads_of_the_export(sent: list[str]) -> list[str]:
    """The export's lock statements and its decision read among ``sent``."""
    return [
        q for q in sent
        if any(q.startswith(prefix) for prefix, _mode in LOCKS)
        or (q.startswith("select approvals.") and "order by approvals.decision_seq desc" in q)
    ]


def _answer(response) -> tuple:
    """``(status,)`` for a success, ``(status, code, message)`` for an error envelope."""
    if response.status_code < 400:
        return (response.status_code,)
    body = response.json()["error"]
    return response.status_code, body["code"], body["message"]


def _decisions(w: CreativeWorld, subject_id: str) -> list[str]:
    rows = sorted(w.rows(Approval, subject_id=subject_id), key=lambda a: a["decision_seq"])
    return [a["decision"] for a in rows]


def _identity(w: CreativeWorld, user_id: str) -> tuple[str, str, str]:
    """The account's id, address and name, read while it exists."""
    row = w.row(User, user_id)
    return row["id"], row["email"], row["full_name"]


def _naming(w: CreativeWorld, identity: tuple[str, ...], **where) -> list[tuple[str, str, str]]:
    """``(table, column, value)`` for every column (JSON included) of every committed
    ``exports``, ``audit_logs`` and ``notifications`` row matching ``where`` (every row of the
    three tables when it is empty) that contains one of the identity's values."""
    found = []
    for model in (Export, AuditLog, Notification):
        for row in w.rows(model, **where):
            found += _named_in(model.__tablename__, row, identity)
    return found


def _named_in(table: str, row: dict, identity: tuple[str, ...]) -> list[tuple[str, str, str]]:
    return [
        (table, column, str(value))
        for column, value in row.items()
        if value is not None and any(part in str(value) for part in identity)
    ]


def _naming_in_the_exports_records(
    w: CreativeWorld, identity: tuple[str, ...], organization_id: str
) -> list[tuple[str, str, str]]:
    """The same, over what an export writes in the organization: its ``exports`` rows, its
    ``creative_draft.exported`` audit rows and its ``export.completed`` notifications."""
    rows, audits = _export_records(w, organization_id=organization_id)
    notified = w.rows(Notification, organization_id=organization_id, event_code="export.completed")
    return [
        hit
        for table, records in (("exports", rows), ("audit_logs", audits),
                               ("notifications", notified))
        for record in records
        for hit in _named_in(table, record, identity)
    ]


def _report(name: str, observed: dict, expected: dict) -> None:
    print(f"\nOBSERVED {name}: {observed}\nEXPECTED {name}: {expected}")
    assert observed == expected


# --------------------------------------------------------------------------- #
# A decision on the draft and an export
# --------------------------------------------------------------------------- #
@NOTIFICATIONS
@WITHDRAWALS
def test_sqlite_a_draft_decision_first_the_export_waits_and_is_refused(
    lite: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = lite.workspace("i5esq draft first")  # creative generation on: the decision route
    made, _approval = _exportable(lite, ws, notifications=notifications)
    gate = _Gate()
    monkeypatch.setattr(approvals_service, "notify", gate.before(approvals_service.notify))
    deciding = _Runner(lambda: _decide_draft(lite, ws, made, decision))
    deciding.start()
    try:
        gate.wait("the decision")  # its row and its audit row are flushed, not committed
        with _Trace(lite.engine) as trace:
            exporting = _Runner(lambda: _export(lite, ws, made))
            exporting.start()
            waiting, before = trace.waiting(exporting)
    finally:
        gate.release.set()
    observed = {
        "decision": _answer(deciding.outcome()),
        "export": _answer(exporting.outcome()),
        "decisions": _decisions(lite, made),
        "export rows, audit rows": tuple(len(r) for r in _export_records(lite, workspace_id=ws)),
        "export notifications": _export_notifications(lite, ws),
    }
    _report("draft decision first", observed, {
        "decision": (201,),
        "export": (409, "export_not_approved", NOT_APPROVED[decision]),
        "decisions": ["approved", decision],
        "export rows, audit rows": (0, 0),
        "export notifications": 0,
    })
    # It waited for the writer lock before any of its reads, holding nothing.
    assert waiting == SERIALIZE and _reads_of_the_export(before) == []


@NOTIFICATIONS
@WITHDRAWALS
def test_sqlite_an_export_first_the_draft_decision_waits_until_it_has_committed(
    lite: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = lite.workspace("i5esq export first")
    made, approval_id = _exportable(lite, ws, notifications=notifications)
    gate = _Gate()
    # After the export's last check and before its first insert (R-5E-A's P1).
    monkeypatch.setattr(export_service, "render_export", gate.before(export_service.render_export))
    exporting = _Runner(lambda: _export(lite, ws, made))
    exporting.start()
    try:
        gate.wait("the export")
        with _Trace(lite.engine) as trace:
            deciding = _Runner(lambda: _decide_draft(lite, ws, made, decision))
            deciding.start()
            waiting, _before = trace.waiting(deciding)
        committed_while_the_export_was_held = _decisions(lite, made)
    finally:
        gate.release.set()
    exported, decided = exporting.outcome(), deciding.outcome()
    rows, audits = _export_records(lite, workspace_id=ws)
    later = [a for a in lite.rows(Approval, subject_id=made) if a["decision"] == decision]
    observed = {
        "decisions committed while the export was held": committed_while_the_export_was_held,
        "the decision waited in a write": _is_write(waiting),
        "export": _answer(exported),
        "decision": _answer(decided),
        "decisions": _decisions(lite, made),
        "export rows, audit rows": (len(rows), len(audits)),
        "the export's approval": [r["approval_id"] for r in rows],
        STAMPED_BEFORE_IT_WAITED: bool(
            rows and later and later[0]["created_at"] <= rows[0]["created_at"]
        ),
        "export notifications": _export_notifications(lite, ws),
        "the next export": _answer(_export(lite, ws, made)),
    }
    _report("export first, draft decision", observed, {
        "decisions committed while the export was held": ["approved"],
        "the decision waited in a write": True,
        "export": (201,),
        "decision": (201,),
        "decisions": ["approved", decision],
        "export rows, audit rows": (1, 1),
        "the export's approval": [approval_id],
        STAMPED_BEFORE_IT_WAITED: True,
        "export notifications": 2 if notifications else 0,
        "the next export": (409, "export_not_approved", NOT_APPROVED[decision]),
    })


# --------------------------------------------------------------------------- #
# A decision on the draft's brief and an export
# --------------------------------------------------------------------------- #
def _brief_workspace(w: CreativeWorld, name: str) -> str:
    ws = w.workspace(name, enabled=False)
    set_cap(w, ws, "recommendation_briefs", True)  # the brief decision route
    return ws


@NOTIFICATIONS
@WITHDRAWALS
def test_sqlite_a_brief_decision_first_the_export_waits_and_is_refused(
    lite: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = _brief_workspace(lite, "i5esq brief first")
    made, _approval = _exportable(lite, ws, notifications=notifications)
    brief_id = lite.row(CreativeDraft, made)["brief_id"]
    gate = _Gate()
    monkeypatch.setattr(approvals_service, "notify", gate.before(approvals_service.notify))
    deciding = _Runner(lambda: _decide_brief(lite, ws, made, decision))
    deciding.start()
    try:
        gate.wait("the brief decision")
        with _Trace(lite.engine) as trace:
            exporting = _Runner(lambda: _export(lite, ws, made))
            exporting.start()
            waiting, before = trace.waiting(exporting)
    finally:
        gate.release.set()
    observed = {
        "decision": _answer(deciding.outcome()),
        "export": _answer(exporting.outcome()),
        "brief decisions": _decisions(lite, brief_id),
        "draft decisions": _decisions(lite, made),
        "export rows, audit rows": tuple(len(r) for r in _export_records(lite, workspace_id=ws)),
        "export notifications": _export_notifications(lite, ws),
    }
    _report("brief decision first", observed, {
        "decision": (201,),
        "export": (409, "export_review_blocked", BRIEF_NO_LONGER_APPROVED),
        "brief decisions": ["approved", decision],
        "draft decisions": ["approved"],  # the draft's own approval stands (item 38)
        "export rows, audit rows": (0, 0),
        "export notifications": 0,
    })
    assert waiting == SERIALIZE and _reads_of_the_export(before) == []


@NOTIFICATIONS
@WITHDRAWALS
def test_sqlite_an_export_first_the_brief_decision_waits_until_it_has_committed(
    lite: CreativeWorld, monkeypatch, decision: str, notifications: bool
):
    ws = _brief_workspace(lite, "i5esq export first b")
    made, approval_id = _exportable(lite, ws, notifications=notifications)
    brief_id = lite.row(CreativeDraft, made)["brief_id"]
    gate = _Gate()
    monkeypatch.setattr(export_service, "render_export", gate.before(export_service.render_export))
    exporting = _Runner(lambda: _export(lite, ws, made))
    exporting.start()
    try:
        gate.wait("the export")
        with _Trace(lite.engine) as trace:
            deciding = _Runner(lambda: _decide_brief(lite, ws, made, decision))
            deciding.start()
            waiting, _before = trace.waiting(deciding)
        committed_while_the_export_was_held = _decisions(lite, brief_id)
    finally:
        gate.release.set()
    exported, decided = exporting.outcome(), deciding.outcome()
    rows, audits = _export_records(lite, workspace_id=ws)
    later = [a for a in lite.rows(Approval, subject_id=brief_id) if a["decision"] == decision]
    observed = {
        "brief decisions committed while the export was held": committed_while_the_export_was_held,
        "the decision waited in a write": _is_write(waiting),
        "export": _answer(exported),
        "decision": _answer(decided),
        "brief decisions": _decisions(lite, brief_id),
        "export rows, audit rows": (len(rows), len(audits)),
        "the export's approval": [r["approval_id"] for r in rows],
        STAMPED_BEFORE_IT_WAITED: bool(
            rows and later and later[0]["created_at"] <= rows[0]["created_at"]
        ),
        "the next export": _answer(_export(lite, ws, made)),
    }
    _report("export first, brief decision", observed, {
        "brief decisions committed while the export was held": ["approved"],
        "the decision waited in a write": True,
        "export": (201,),
        "decision": (201,),
        "brief decisions": ["approved", decision],
        "export rows, audit rows": (1, 1),
        "the export's approval": [approval_id],
        STAMPED_BEFORE_IT_WAITED: True,
        "the next export": (409, "export_review_blocked", BRIEF_NO_LONGER_APPROVED),
    })


# --------------------------------------------------------------------------- #
# Organization deletion and an export by another member
# --------------------------------------------------------------------------- #
@NOTIFICATIONS
def test_sqlite_an_organization_deletion_first_the_export_waits_and_answers_404(
    lite: CreativeWorld, monkeypatch, notifications: bool
):
    org = _fresh_organization(lite, "sqdelfirst")
    made, _approval = _exportable(org, org.ws, notifications=notifications)
    exporter = _identity(lite, org.users["admin"])
    gate = _Gate()
    monkeypatch.setattr(erasure, "erase_organization", gate.after(erasure.erase_organization))
    deleting = _Runner(lambda: _delete_organization(org))
    deleting.start()
    try:
        gate.wait("the deletion")  # everything of the organization is deleted, not committed
        with _Trace(lite.engine) as trace:
            exporting = _Runner(lambda: _export(org, org.ws, made))
            exporting.start()
            waiting, before = trace.waiting(exporting)
    finally:
        gate.release.set()
    observed = {
        "deletion": _answer(deleting.outcome()),
        "export": _answer(exporting.outcome()),
        "the organization exists": _exists(lite, Organization, org.org),
        "export rows, audit rows": tuple(
            len(r) for r in _export_records(lite, organization_id=org.org)
        ),
        "notifications": lite.count(Notification, organization_id=org.org),
        "columns naming the exporter": _naming(lite, exporter, organization_id=org.org),
    }
    _report("organization deletion first", observed, {
        "deletion": (200,),
        "export": (404, "not_found", DRAFT_NOT_FOUND),
        "the organization exists": False,
        "export rows, audit rows": (0, 0),
        "notifications": 0,
        "columns naming the exporter": [],
    })
    assert waiting == SERIALIZE and _reads_of_the_export(before) == []


@NOTIFICATIONS
def test_sqlite_an_export_first_the_organization_deletion_waits_and_anonymises_it(
    lite: CreativeWorld, monkeypatch, notifications: bool
):
    org = _fresh_organization(lite, "sqexpfirst")
    made, approval_id = _exportable(org, org.ws, notifications=notifications)
    exporter = _identity(lite, org.users["admin"])
    gate = _Gate()
    monkeypatch.setattr(export_service, "render_export", gate.before(export_service.render_export))
    exporting = _Runner(lambda: _export(org, org.ws, made))
    exporting.start()
    try:
        gate.wait("the export")
        with _Trace(lite.engine) as trace:
            deleting = _Runner(lambda: _delete_organization(org))
            deleting.start()
            waiting, _before = trace.waiting(deleting)
        existed_while_the_export_was_held = _exists(lite, Organization, org.org)
    finally:
        gate.release.set()
    exported, deleted = exporting.outcome(), deleting.outcome()
    rows, audits = _export_records(lite, organization_id=org.org)
    observed = {
        "the organization existed while the export was held": existed_while_the_export_was_held,
        "the deletion waited in a write": _is_write(waiting),
        "export": _answer(exported),
        "deletion": _answer(deleted),
        "the organization exists": _exists(lite, Organization, org.org),
        "the exporter's account exists": _exists(lite, User, exporter[0]),
        "export rows (exported_by, snapshot, draft, approval, version)": [
            (r["exported_by"], r["exported_by_snapshot"], r["draft_id"], r["approval_id"],
             r["draft_version"])
            for r in rows
        ],
        "audit rows (actor, entity, new_state, context)": [
            (a["actor_user_id"], a["entity_id"], a["new_state"], a["context"]) for a in audits
        ],
        "notifications": lite.count(Notification, organization_id=org.org),
        "columns naming the exporter": _naming(lite, exporter, organization_id=org.org),
    }
    _report("export first, organization deletion", observed, {
        "the organization existed while the export was held": True,
        "the deletion waited in a write": True,
        "export": (201,),
        "deletion": (200,),
        "the organization exists": False,
        "the exporter's account exists": True,  # members' accounts are not deleted
        "export rows (exported_by, snapshot, draft, approval, version)": [
            (None, "[erased]", None, approval_id, 1)  # the record stays, the identity does not
        ],
        "audit rows (actor, entity, new_state, context)": [(None, None, None, {})],
        "notifications": 0,  # gone with their workspace
        "columns naming the exporter": [],
    })


# --------------------------------------------------------------------------- #
# Erasure of the exporter's own account and an export
# --------------------------------------------------------------------------- #
def _hold_after_the_user_row_is_deleted(monkeypatch, gate: _Gate) -> None:
    """Hold the erasure when its last write (the user row) is flushed, before it commits."""
    real = erasure.delete_rows

    def delete_rows(db, table, ids):
        counts = real(db, table, ids)
        if table.name == "users" and gate._first():
            gate._hold()
        return counts

    monkeypatch.setattr(erasure, "delete_rows", delete_rows)


@NOTIFICATIONS
def test_sqlite_the_exporters_account_erasure_first_the_export_waits_and_answers_401(
    lite: CreativeWorld, monkeypatch, notifications: bool
):
    org = _fresh_organization(lite, "sqerafirst")
    made, _approval = _exportable(org, org.ws, notifications=notifications)
    exporter = _identity(lite, org.users["admin"])
    gate = _Gate()
    _hold_after_the_user_row_is_deleted(monkeypatch, gate)
    erasing = _Runner(lambda: _delete_account(org, "admin"))
    erasing.start()
    try:
        gate.wait("the erasure")  # the account and what names it are erased, not committed
        with _Trace(lite.engine) as trace:
            exporting = _Runner(lambda: _export(org, org.ws, made))
            exporting.start()
            waiting, before = trace.waiting(exporting)
    finally:
        gate.release.set()
    observed = {
        "erasure": _answer(erasing.outcome()),
        "export": _answer(exporting.outcome()),
        "the exporter's account exists": _exists(lite, User, exporter[0]),
        "the organization exists": _exists(lite, Organization, org.org),
        "export rows, audit rows": tuple(
            len(r) for r in _export_records(lite, organization_id=org.org)
        ),
        "export notifications": _export_notifications(org, org.ws),
        "columns naming the exporter's id, anywhere": _naming(lite, exporter[:1]),
        "columns of the export's records naming the exporter": _naming_in_the_exports_records(
            lite, exporter, org.org
        ),
    }
    _report("account erasure first", observed, {
        "erasure": (200,),
        "export": ACCOUNT_GONE,
        "the exporter's account exists": False,
        "the organization exists": True,  # other members remain
        "export rows, audit rows": (0, 0),
        "export notifications": 0,
        "columns naming the exporter's id, anywhere": [],
        "columns of the export's records naming the exporter": [],
    })
    assert waiting == SERIALIZE and _reads_of_the_export(before) == []


@NOTIFICATIONS
def test_sqlite_an_export_first_the_exporters_account_erasure_waits_and_anonymises_it(
    lite: CreativeWorld, monkeypatch, notifications: bool
):
    org = _fresh_organization(lite, "sqexpfirsta")
    made, approval_id = _exportable(org, org.ws, notifications=notifications)
    exporter = _identity(lite, org.users["admin"])
    gate = _Gate()
    monkeypatch.setattr(export_service, "render_export", gate.before(export_service.render_export))
    exporting = _Runner(lambda: _export(org, org.ws, made))
    exporting.start()
    try:
        gate.wait("the export")
        with _Trace(lite.engine) as trace:
            erasing = _Runner(lambda: _delete_account(org, "admin"))
            erasing.start()
            waiting, _before = trace.waiting(erasing)
        existed_while_the_export_was_held = _exists(lite, User, exporter[0])
    finally:
        gate.release.set()
    exported, erased = exporting.outcome(), erasing.outcome()
    rows, audits = _export_records(lite, organization_id=org.org)
    observed = {
        "the account existed while the export was held": existed_while_the_export_was_held,
        "the erasure waited in a write": _is_write(waiting),
        "export": _answer(exported),
        "erasure": _answer(erased),
        "the exporter's account exists": _exists(lite, User, exporter[0]),
        "export rows (exported_by, snapshot, draft, approval)": [
            (r["exported_by"], r["exported_by_snapshot"], r["draft_id"], r["approval_id"])
            for r in rows
        ],
        "audit rows (actor)": [a["actor_user_id"] for a in audits],
        "export notifications": _export_notifications(org, org.ws),
        "columns naming the exporter's id, anywhere": _naming(lite, exporter[:1]),
        "columns of the export's records naming the exporter": _naming_in_the_exports_records(
            lite, exporter, org.org
        ),
    }
    _report("export first, account erasure", observed, {
        "the account existed while the export was held": True,
        "the erasure waited in a write": True,
        "export": (201,),
        "erasure": (200,),
        "the exporter's account exists": False,
        "export rows (exported_by, snapshot, draft, approval)": [
            (None, "[erased]", made, approval_id)  # the record stays, the identity does not
        ],
        "audit rows (actor)": [None],
        # The draft's author and its approver, who are not the exporter, keep theirs.
        "export notifications": 2 if notifications else 0,
        "columns naming the exporter's id, anywhere": [],
        "columns of the export's records naming the exporter": [],
    })


# --------------------------------------------------------------------------- #
# An erasure that writes nothing before it reads which rows name its subject
# --------------------------------------------------------------------------- #
def _account_nothing_names(w: CreativeWorld, org: CreativeWorld) -> tuple[str, str, str]:
    """Make the organization's owner an account whose erasure has nothing to write before it
    reads which exports name it, and return its identity.

    Account erasure reads the rows naming the account and then updates each
    (``anonymise_phase5_records_for_subject``). Deleting an accepted invitation or clearing an
    audit row that names the account comes earlier and is a write, which on SQLite happens to
    take the writer lock before that read. The owner registered (no invitation) and authored
    nothing; here the admin is made a second owner (so the owner may leave), and the audit rows
    naming the owner are removed, as the audit retention purge removes them by age."""
    owner = _identity(w, org.users["owner"])
    with w.session() as s:
        s.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == org.org,
                OrganizationMember.user_id == org.users["admin"],
            )
            .values(role=Role.OWNER.value)
        )
    naming = [
        row["id"] for row in w.rows(AuditLog) if _named_in("audit_logs", row, owner[:2])
    ]
    with w.session() as s:
        s.execute(delete(AuditLog.__table__).where(AuditLog.__table__.c.id.in_(naming)))
    assert _naming(w, owner[:2]) == []
    return owner


def _whole_row(row: dict) -> dict:
    """Every column of an ``exports`` row except its id and timestamp."""
    return {k: v for k, v in row.items() if k not in ("id", "created_at")}


def _expected_export_row(org: CreativeWorld, made: str, approval_id: str, **changed) -> dict:
    return {
        "organization_id": org.org, "workspace_id": org.ws, "draft_id": made,
        "draft_version": 1, "approval_id": approval_id, "format": "text/plain",
        "jurisdiction_code": "US-TX", "is_simulated": True, **changed,
    }  # fmt: skip


@NOTIFICATIONS
def test_sqlite_an_export_first_an_erasure_with_no_earlier_write_waits_and_anonymises_it(
    lite: CreativeWorld, monkeypatch, notifications: bool
):
    """The erasure takes the writer lock before it reads which rows name the account, so it
    waits for the export there and then finds the export's row. Without that lock its read
    misses the uncommitted row, and the row keeps the erased account's id, name and address in
    ``exported_by_snapshot``."""
    org = _fresh_organization(lite, "sqbarex")
    made, approval_id = _exportable(org, org.ws, notifications=notifications)
    exporter = _account_nothing_names(lite, org)
    gate = _Gate()
    monkeypatch.setattr(export_service, "render_export", gate.before(export_service.render_export))
    exporting = _Runner(lambda: _export(org, org.ws, made, "owner"))
    exporting.start()
    try:
        gate.wait("the export")
        with _Trace(lite.engine) as trace:
            erasing = _Runner(lambda: _delete_account(org, "owner"))
            erasing.start()
            waiting, before = trace.waiting(erasing)
    finally:
        gate.release.set()
    exported, erased = exporting.outcome(), erasing.outcome()
    rows, audits = _export_records(lite, organization_id=org.org)
    observed = {
        "the erasure waited in": waiting,
        "its reads of exports before it waited": [q for q in before if " from exports" in q],
        "export": _answer(exported),
        "erasure": _answer(erased),
        "the account exists": _exists(lite, User, exporter[0]),
        "the organization exists": _exists(lite, Organization, org.org),
        "export rows, every column": [_whole_row(r) for r in rows],
        "audit rows (actor)": [a["actor_user_id"] for a in audits],
        "columns naming the account's id or address, anywhere": _naming(lite, exporter[:2]),
        "columns of the export's records naming the account": _naming_in_the_exports_records(
            lite, exporter, org.org
        ),
    }
    _report("export first, an erasure with no earlier write", observed, {
        "the erasure waited in": SERIALIZE,
        "its reads of exports before it waited": [],
        "export": (201,),
        "erasure": (200,),
        "the account exists": False,
        "the organization exists": True,  # the second owner remains
        "export rows, every column": [
            _expected_export_row(
                org, made, approval_id, exported_by=None, exported_by_snapshot="[erased]"
            )
        ],
        "audit rows (actor)": [None],
        "columns naming the account's id or address, anywhere": [],
        "columns of the export's records naming the account": [],
    })


@NOTIFICATIONS
def test_sqlite_an_erasure_first_held_before_its_first_write_the_export_waits_and_answers_401(
    lite: CreativeWorld, monkeypatch, notifications: bool
):
    """The other order: the erasure has read which rows name the account and has written
    nothing yet. It already holds the writer lock, so the export waits and, once the erasure
    has committed, answers 401. Without that lock the export commits in the gap, after the
    erasure's read, and its row keeps the erased identity."""
    org = _fresh_organization(lite, "sqbarer")
    made, _approval = _exportable(org, org.ws, notifications=notifications)
    exporter = _account_nothing_names(lite, org)
    gate = _Gate()
    real = erasure.delete_rows

    def delete_rows(db, table, ids):  # the user row's walk: the erasure's first write
        if table.name == "users" and gate._first():
            gate._hold()
        return real(db, table, ids)

    monkeypatch.setattr(erasure, "delete_rows", delete_rows)
    erasing = _Runner(lambda: _delete_account(org, "owner"))
    erasing.start()
    try:
        gate.wait("the erasure")
        with _Trace(lite.engine) as trace:
            exporting = _Runner(lambda: _export(org, org.ws, made, "owner"))
            exporting.start()
            waiting, before = trace.waiting(exporting)
        exports_committed_while_the_erasure_was_held = len(
            lite.rows(Export, organization_id=org.org)
        )
    finally:
        gate.release.set()
    erased, exported = erasing.outcome(), exporting.outcome()
    rows, audits = _export_records(lite, organization_id=org.org)
    observed = {
        "the export waited in": waiting,
        "exports committed while the erasure was held": (
            exports_committed_while_the_erasure_was_held
        ),
        "erasure": _answer(erased),
        "export": _answer(exported),
        "the account exists": _exists(lite, User, exporter[0]),
        "export rows, every column": [_whole_row(r) for r in rows],
        "audit rows": len(audits),
        "export notifications": _export_notifications(org, org.ws),
        "columns naming the account's id or address, anywhere": _naming(lite, exporter[:2]),
    }
    _report("an erasure first, held before its first write", observed, {
        "the export waited in": SERIALIZE,
        "exports committed while the erasure was held": 0,
        "erasure": (200,),
        "export": ACCOUNT_GONE,
        "the account exists": False,
        "export rows, every column": [],
        "audit rows": 0,
        "export notifications": 0,
        "columns naming the account's id or address, anywhere": [],
    })
    assert _reads_of_the_export(before) == []


def test_sqlite_an_account_deletion_with_a_wrong_password_takes_no_writer_lock(
    lite: CreativeWorld,
):
    """The password is checked first: a wrong one answers its 422 at once while another
    connection holds the writer lock, and sends no ``BEGIN``."""
    org = _fresh_organization(lite, "sqwrongpw")
    with _writer_lock_held_elsewhere(lite):
        response, sent, seconds = _traced(
            lite,
            lambda: org.send("POST", "/account/delete", "admin",
                             {"current_password": PASSWORD + "x", "confirmation": "DELETE"}),
        )
    assert _answer(response)[:2] == (422, "current_password_incorrect")
    assert not [q for q in sent if q.startswith("begin")]
    assert seconds < BUSY_TIMEOUT_SECONDS / 2, seconds
    assert _exists(lite, User, org.users["admin"])


def test_sqlite_an_export_first_an_invitee_email_erasure_waits_and_clears_its_snapshot(
    lite: CreativeWorld, monkeypatch
):
    """The operator's erasure of an address (``erase_invitee_email``) reads the same way. An
    unverified account holds the address and stays; its export in flight is waited for, and the
    address is cleared from the export's snapshot (``exported_by`` stays: the account does)."""
    org = _fresh_organization(lite, "sqinvitee")
    made, approval_id = _exportable(org, org.ws, notifications=False)
    exporter = _account_nothing_names(lite, org)
    assert lite.row(User, exporter[0])["email_verified_at"] is None
    gate = _Gate()
    monkeypatch.setattr(export_service, "render_export", gate.before(export_service.render_export))
    exporting = _Runner(lambda: _export(org, org.ws, made, "owner"))
    exporting.start()

    def erase() -> dict:
        with lite.session() as s:
            return dict(erasure.erase_invitee_email(s, email=exporter[1]).anonymised)

    try:
        gate.wait("the export")
        with _Trace(lite.engine) as trace:
            erasing = _Runner(erase)
            erasing.start()
            waiting, before = trace.waiting(erasing)
    finally:
        gate.release.set()
    exported, anonymised = exporting.outcome(), erasing.outcome()
    rows, _audits = _export_records(lite, organization_id=org.org)
    observed = {
        "the erasure waited in": waiting,
        "its reads of exports before it waited": [q for q in before if " from exports" in q],
        "export": _answer(exported),
        "exports anonymised": anonymised.get("exports", 0),
        "export rows, every column": [_whole_row(r) for r in rows],
        "columns naming the address, anywhere": _naming(lite, exporter[1:2]),
    }
    _report("export first, invitee-email erasure", observed, {
        "the erasure waited in": SERIALIZE,
        "its reads of exports before it waited": [],
        "export": (201,),
        "exports anonymised": 1,
        "export rows, every column": [
            _expected_export_row(
                org, made, approval_id, exported_by=exporter[0], exported_by_snapshot="[erased]"
            )
        ],
        "columns naming the address, anywhere": [],
    })


# --------------------------------------------------------------------------- #
# The mechanism: where the writer lock is taken, and where it is not
# --------------------------------------------------------------------------- #
@contextmanager
def _writer_lock_held_elsewhere(w: CreativeWorld) -> Iterator[None]:
    """Another connection holds SQLite's writer lock (and has written nothing)."""
    with w.witness.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            conn.rollback()


def _traced(w: CreativeWorld, request: Callable) -> tuple:
    with _Trace(w.engine) as trace:
        started = time.monotonic()
        response = request()
        return response, trace.sent, time.monotonic() - started


def test_sqlite_a_dark_export_takes_no_writer_lock_and_waits_for_none(lite: CreativeWorld):
    """The capability is decided first: a dark workspace's export request issues no ``BEGIN
    IMMEDIATE`` and none of the four lock statements, and answers its 503 at once while another
    connection holds the writer lock."""
    ws = lite.workspace("i5esq dark", enabled=False)
    made = draft(lite, ws)
    decide(lite, made, "approved")
    with _writer_lock_held_elsewhere(lite):
        response, sent, seconds = _traced(lite, lambda: _export(lite, ws, made))
    assert _answer(response)[:2] == (503, "capability_unavailable")
    assert SERIALIZE not in sent and not [q for q in sent if q.startswith("begin")]
    assert _reads_of_the_export(sent) == []
    assert seconds < BUSY_TIMEOUT_SECONDS / 2, seconds


def test_sqlite_an_export_without_the_role_takes_no_writer_lock(lite: CreativeWorld):
    """The role is decided first too: a reviewer's request (403) issues no ``BEGIN IMMEDIATE``
    and does not wait for a writer, with export on for the workspace."""
    ws = lite.workspace("i5esq role", enabled=False)
    made, _approval = _exportable(lite, ws, notifications=False)
    with _writer_lock_held_elsewhere(lite):
        response, sent, seconds = _traced(lite, lambda: _export(lite, ws, made, "reviewer"))
    assert _answer(response)[:2] == (403, "permission_denied")
    assert not [q for q in sent if q.startswith("begin")]
    assert seconds < BUSY_TIMEOUT_SECONDS / 2, seconds
    assert _export_records(lite, workspace_id=ws) == ([], [])


@pytest.mark.parametrize("approved", [True, False], ids=["exported", "refused"])
def test_sqlite_the_writer_lock_is_taken_once_after_the_capability_and_before_every_read(
    lite: CreativeWorld, approved: bool
):
    """One ``BEGIN IMMEDIATE``: after the capability's override read, before the first of the
    four lock statements, the decision read and every insert. A refused export has taken it too,
    has written nothing, and has released it when it answers."""
    ws = lite.workspace("i5esq order", enabled=False)
    set_cap(lite, ws, "content_export", True)
    made = draft(lite, ws)
    if approved:
        decide(lite, made, "approved")
    response, sent, _seconds = _traced(lite, lambda: _export(lite, ws, made))
    assert response.status_code == (201 if approved else 409), response.text
    assert [q for q in sent if q.startswith("begin")] == [SERIALIZE]
    taken = sent.index(SERIALIZE)
    capability = min(  # the export capability's override read (the emitter reads later)
        i for i, q in enumerate(sent) if "from workspace_capability_overrides" in q
    )
    reads = _reads_of_the_export(sent)
    for prefix, _mode in LOCKS:
        assert len([q for q in reads if q.startswith(prefix)]) == 1, (prefix, reads)
    assert [q for q in reads if q.startswith("select approvals.")], reads
    assert capability < taken < min(sent.index(q) for q in reads), sent
    writes = [i for i, q in enumerate(sent) if _is_write(q)]
    assert all(i > taken for i in writes)
    assert bool(writes) is approved
    # Released: another connection takes the writer lock at once.
    with lite.witness.connect() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=0")
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        finally:
            conn.rollback()
            conn.exec_driver_sql("PRAGMA busy_timeout=5000")


def test_sqlite_a_caller_whose_transaction_has_already_written_is_not_refused(
    lite: CreativeWorld,
):
    """The service is also called inside a caller's transaction. One that has already written
    holds the writer lock (this driver opens a transaction only before a write), so no second
    ``BEGIN`` is sent (SQLite would refuse it) and the export is recorded in that transaction."""
    ws = lite.workspace("i5esq nested", enabled=False)
    made, approval_id = _exportable(lite, ws, notifications=False)
    with _Trace(lite.engine) as trace, lite.session() as s:
        user = s.get(User, lite.users["admin"])
        ctx = TenantContext(
            user=user,
            organization=s.get(Organization, lite.org),
            workspace=s.get(Workspace, ws),
            role=Role(s.scalar(
                select(OrganizationMember.role).where(
                    OrganizationMember.organization_id == lite.org,
                    OrganizationMember.user_id == user.id,
                )
            )),
        )
        set_marker = Approval.__table__.update().where(Approval.__table__.c.id == "nobody")
        s.execute(set_marker.values(note=None))  # a write: the transaction is open
        assert s.connection().connection.dbapi_connection.in_transaction is True
        result = export_service.create_export(
            s, ctx=ctx, draft_id=made, export_format=ExportFormat.TEXT_PLAIN,
            emitter=emit_notifications,
        )
        assert result.export.approval_id == approval_id
    assert not [q for q in trace.sent if q.startswith("begin")]
    assert len(lite.rows(Export, workspace_id=ws)) == 1


def test_postgresql_never_sees_the_sqlite_statement():
    """Engine-conditional: on PostgreSQL an export issues no ``BEGIN IMMEDIATE`` (PostgreSQL
    would refuse the statement) and its four row locks are what serialize it."""
    with postgres_creative_world("i5esqpg") as w:  # skips unless TEST_POSTGRES_URL is set
        ws = w.workspace("i5esq pg", enabled=False)
        made, _approval = _exportable(w, ws, notifications=False)
        response, sent, _seconds = _traced(w, lambda: _export(w, ws, made))
        assert response.status_code == 201, response.text
        assert not [q for q in sent if "immediate" in q]
        assert [q for q in sent if q.endswith(("for share", "for key share"))] == (
            _reads_of_the_export(sent)[:4]
        )


# --------------------------------------------------------------------------- #
# Consequences: what each side answers when the busy timeout expires
# --------------------------------------------------------------------------- #
def test_sqlite_an_export_that_cannot_get_the_writer_lock_in_time_answers_500_and_writes_nothing(
    lite: CreativeWorld,
):
    """A writer that stays uncommitted past the busy timeout (5 s): the export's ``BEGIN
    IMMEDIATE`` fails, the application answers its generic 500, nothing is written, and the
    writer is unaffected."""
    ws = lite.workspace("i5esq busy export", enabled=False)
    made, _approval = _exportable(lite, ws, notifications=False)
    with _writer_lock_held_elsewhere(lite):
        response, sent, seconds = _traced(lite, lambda: _export(lite, ws, made))
    assert _answer(response) == INTERNAL_ERROR
    assert sent[-1] == SERIALIZE and _reads_of_the_export(sent) == []
    assert seconds >= BUSY_TIMEOUT_SECONDS - 0.5, seconds
    assert _export_records(lite, workspace_id=ws) == ([], [])
    assert _answer(_export(lite, ws, made)) == (201,)  # and the next one succeeds


def test_sqlite_a_decision_that_cannot_get_the_writer_lock_in_time_answers_500_and_writes_nothing(
    lite: CreativeWorld, monkeypatch
):
    """An export that stays uncommitted past the busy timeout holds the writer lock from before
    its checks: a revocation of its draft's approval fails at its first write, answers the
    generic 500 and writes nothing; the export then commits and the next export is still
    allowed, because no decision was recorded."""
    ws = lite.workspace("i5esq busy decision")
    made, _approval = _exportable(lite, ws, notifications=False)
    gate = _Gate()
    monkeypatch.setattr(export_service, "render_export", gate.before(export_service.render_export))
    exporting = _Runner(lambda: _export(lite, ws, made))
    exporting.start()
    try:
        gate.wait("the export")
        started = time.monotonic()
        refused = _decide_draft(lite, ws, made, "revoked")
        seconds = time.monotonic() - started
    finally:
        gate.release.set()
    assert _answer(refused) == INTERNAL_ERROR
    assert seconds >= BUSY_TIMEOUT_SECONDS - 0.5, seconds
    assert _answer(exporting.outcome()) == (201,)
    assert _decisions(lite, made) == ["approved"]
    assert len(lite.rows(Export, workspace_id=ws)) == 1
    assert _answer(_export(lite, ws, made)) == (201,)
