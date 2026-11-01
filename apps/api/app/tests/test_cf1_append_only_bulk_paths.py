"""Project Phase 5 CF-1 (W21, review r1 DATA-C1): no ORM path updates or deletes an append-only row.

The unit-of-work guard (``before_update`` / ``before_delete``) is covered by
``test_cf1_append_only_models.py``. This module covers every other ORM write path the review
found open at ``30fc045c`` — ORM-enabled UPDATE/DELETE statements, ``Query.update/delete``,
the legacy bulk API and an ORM upsert — for each of the three append-only models, and shows:

* each path raises :class:`~app.db.append_only.AppendOnlyViolation` and the stored row is
  byte-identical afterwards;
* the sanctioned Core statements on the *table* (privacy anonymisation, the foreign-key walk)
  still work under the guard, and an ORM INSERT (plain or ``ON CONFLICT DO NOTHING``) is not
  refused;
* **static pin:** no production module outside ``app/privacy/`` builds an UPDATE or DELETE
  over these models or their tables, or calls the bulk API, with in-test controls proving the
  detector fires.

The rows live in the P6-PRIV-1 world (``_privacy_support``), so the same tests run on SQLite
and — when ``TEST_POSTGRES_URL`` is set — on PostgreSQL, with real parent rows and foreign
keys enforced.
"""

from __future__ import annotations

import ast
import itertools
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import delete, insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.db.append_only import AppendOnlyViolation
from app.tests._privacy_support import ORG_A, WS_A, PrivacyEnv, privacy_world

API_DIR = Path(__file__).resolve().parents[2]
GUARDED = (RecommendationBrief, Approval, AuditLog)
#: The column each probe tries to change (always allowed by the table's CHECKs).
FIELD = {RecommendationBrief: "summary", Approval: "note", AuditLog: "reason"}

BACKENDS = [
    "sqlite",
    pytest.param(
        "postgresql",
        marks=pytest.mark.skipif(
            not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL not set"
        ),
    ),
]


@pytest.fixture(scope="module", params=BACKENDS)
def world(request, tmp_path_factory) -> Iterator[PrivacyEnv]:
    db_file = None
    if request.param == "sqlite":
        db_file = tmp_path_factory.mktemp("cf1-bulk") / "bulk.db"
    with privacy_world(db_file) as env:
        yield env


_serial = itertools.count(1)


def _seed(world: PrivacyEnv) -> dict[type, str]:
    """One fresh committed brief, approval and audit record (Core inserts, as a fixture)."""
    n = next(_serial)
    ids = {RecommendationBrief: f"cf1b-brief-{n}", Approval: f"cf1b-appr-{n}",
           AuditLog: f"cf1b-log-{n}"}
    with world.engine.begin() as conn:
        conn.execute(
            insert(RecommendationBrief.__table__).values(
                id=ids[RecommendationBrief], organization_id=ORG_A, workspace_id=WS_A,
                opportunity_id="a1-opp", intelligence_record_id="a1-intel",
                brief_version=100 + n, analysis_version="1", scoring_version="1",
                prompt_name="recommendation_brief", prompt_version="1.0.0",
                model_identifier="mock", input_hash="0" * 64, is_simulated=True,
                created_by_snapshot="u Name <u@example.test>", summary="original",
                rationale="r", recommended_actions=[], suggested_angles=[], risk_notes=[],
                evidence_refs=[],
            )
        )
        conn.execute(
            insert(Approval.__table__).values(
                id=ids[Approval], organization_id=ORG_A, workspace_id=WS_A,
                subject_type="recommendation_brief", subject_id=ids[RecommendationBrief],
                subject_version=1, brief_id=ids[RecommendationBrief], decision="approved",
                decision_seq=1, note="original", decided_by_snapshot="u Name <u@example.test>",
                decided_role="reviewer", is_simulated=False,
            )
        )
        conn.execute(
            insert(AuditLog.__table__).values(
                id=ids[AuditLog], organization_id=ORG_A, workspace_id=WS_A,
                action="auth.register", reason="original", context={},
            )
        )
    return ids


def _stored(world: PrivacyEnv, model, row_id: str) -> dict | None:
    rows = world.rows(model, model.__table__.c.id == row_id)
    return rows[0] if rows else None


def _upsert(world: PrivacyEnv):
    return pg_insert if world.engine.dialect.name == "postgresql" else sqlite_insert


# --------------------------------------------------------------------------- #
# Every ORM path the review listed (raw/005), for each guarded model
# --------------------------------------------------------------------------- #
def _update_where(s, m, rid, w):
    s.execute(update(m).where(m.id == rid).values({FIELD[m]: "changed"}))


def _update_by_pk(s, m, rid, w):
    s.execute(update(m), [{"id": rid, FIELD[m]: "changed"}])


def _query_update(s, m, rid, w):
    s.query(m).filter(m.id == rid).update({FIELD[m]: "changed"})


def _bulk_update_mappings(s, m, rid, w):
    s.bulk_update_mappings(m, [{"id": rid, FIELD[m]: "changed"}])


def _bulk_save_objects(s, m, rid, w):
    obj = s.get(m, rid)
    s.expunge(obj)
    setattr(obj, FIELD[m], "changed")
    s.bulk_save_objects([obj])


def _delete_where(s, m, rid, w):
    s.execute(delete(m).where(m.id == rid))


def _query_delete(s, m, rid, w):
    s.query(m).filter(m.id == rid).delete()


def _orm_upsert(s, m, rid, w):
    row = _stored(w, m, rid)
    stmt = _upsert(w)(m).values(**row)
    s.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={FIELD[m]: "changed"}))


def _merge_detached(s, m, rid, w):
    obj = s.get(m, rid)
    s.expunge(obj)
    setattr(obj, FIELD[m], "changed")
    s.merge(obj)


PATHS: dict[str, Callable] = {
    "execute(update(Model).where(...))": _update_where,
    "execute(update(Model), [pk rows])": _update_by_pk,
    "Query.update()": _query_update,
    "bulk_update_mappings()": _bulk_update_mappings,
    "bulk_save_objects(detached, modified)": _bulk_save_objects,
    "execute(delete(Model).where(...))": _delete_where,
    "Query.delete()": _query_delete,
    "insert(Model).on_conflict_do_update": _orm_upsert,
    "merge(detached, modified)": _merge_detached,
}


@pytest.mark.parametrize("model", GUARDED, ids=lambda m: m.__tablename__)
@pytest.mark.parametrize("path", list(PATHS))
def test_every_orm_write_path_is_refused_and_the_row_is_unchanged(world, model, path):
    rid = _seed(world)[model]
    before = _stored(world, model, rid)
    factory = sessionmaker(bind=world.engine, expire_on_commit=False, future=True)
    with factory() as s:
        with pytest.raises(AppendOnlyViolation):
            PATHS[path](s, model, rid, world)
            s.commit()
        s.rollback()
    assert _stored(world, model, rid) == before


def test_the_sanctioned_core_statements_still_work_under_the_guard(world):
    """Privacy anonymisation and the foreign-key walk use the TABLE, through a Session."""
    ids = _seed(world)
    factory = sessionmaker(bind=world.engine, expire_on_commit=False, future=True)
    briefs, approvals, logs = (m.__table__ for m in GUARDED)
    with factory() as s:
        s.execute(
            update(briefs)
            .where(briefs.c.id == ids[RecommendationBrief])
            .values(created_by_snapshot="[erased]")
        )
        s.execute(update(logs).where(logs.c.id == ids[AuditLog]).values(reason=None))
        s.execute(delete(approvals).where(approvals.c.id == ids[Approval]))
        s.commit()
    assert _stored(world, RecommendationBrief, ids[RecommendationBrief])[
        "created_by_snapshot"
    ] == "[erased]"
    assert _stored(world, AuditLog, ids[AuditLog])["reason"] is None
    assert _stored(world, Approval, ids[Approval]) is None


def test_orm_inserts_are_not_refused(world):
    """Append-only means insert-only: a plain ORM insert and ON CONFLICT DO NOTHING pass."""
    ids = _seed(world)
    factory = sessionmaker(bind=world.engine, expire_on_commit=False, future=True)
    with factory() as s:
        s.execute(
            insert(AuditLog).values(
                id=ids[AuditLog] + "-2", organization_id=ORG_A, action="auth.register",
                context={},
            )
        )
        row = _stored(world, AuditLog, ids[AuditLog])
        s.execute(_upsert(world)(AuditLog).values(**row).on_conflict_do_nothing())
        s.commit()
    assert _stored(world, AuditLog, ids[AuditLog] + "-2") is not None
    assert _stored(world, AuditLog, ids[AuditLog]) == row


# --------------------------------------------------------------------------- #
# Static pin: production code writes these rows after insert only in app/privacy/
# --------------------------------------------------------------------------- #
_MODELS = {m.__name__ for m in GUARDED}
_BULK = {"bulk_update_mappings", "bulk_insert_mappings", "bulk_save_objects"}


def _guarded_names(tree: ast.AST) -> set[str]:
    """The guarded model names plus every plain-name alias assigned from one in this module
    (``table = AuditLog.__table__``, ``briefs = RecommendationBrief.__table__``), to a fixed
    point, so an alias does not hide a statement from the pin."""
    names = set(_MODELS)
    while True:
        found = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and _is_guarded(node.value, names)
        }
        if found <= names:
            return names
        names |= found


def _is_guarded(node: ast.AST, names: set[str]) -> bool:
    """``Model``, ``Model.__table__`` or an alias of either."""
    if isinstance(node, ast.Name):
        return node.id in names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__table__"
        and isinstance(node.value, ast.Name)
        and node.value.id in names
    )


def _offences(tree: ast.AST) -> list[int]:
    names = _guarded_names(tree)

    def _names_guarded(node: ast.AST) -> bool:
        return _is_guarded(node, names)

    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", getattr(func, "attr", None))
        if name in _BULK:
            lines.append(node.lineno)
        elif name in {"update", "delete"}:
            # update(Model) / delete(Model.__table__) (function form)
            if isinstance(func, ast.Name) and any(_names_guarded(a) for a in node.args):
                lines.append(node.lineno)
            # Model.__table__.update() / .delete() (method form)
            elif isinstance(func, ast.Attribute) and _names_guarded(func.value):
                lines.append(node.lineno)
            # session.query(Model)....update() / .delete()
            elif isinstance(func, ast.Attribute) and any(
                isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "query"
                and any(_names_guarded(a) for a in inner.args)
                for inner in ast.walk(func.value)
            ):
                lines.append(node.lineno)
    return lines


def test_the_detector_fires_on_every_form() -> None:
    """Controls: each form the pin forbids is flagged; a SELECT and an INSERT are not."""
    flagged = [
        "db.execute(update(AuditLog).values(reason=None))",
        "db.execute(update(Approval.__table__).values(note=None))",
        "db.execute(delete(RecommendationBrief).where(x))",
        "db.execute(AuditLog.__table__.delete())",
        "db.execute(Approval.__table__.update().values(note=None))",
        "db.query(RecommendationBrief).filter(x).update({'summary': 's'})",
        "db.query(AuditLog).filter(x).delete()",
        "db.bulk_update_mappings(Approval, rows)",
        "db.bulk_save_objects(objs)",
        "t = AuditLog.__table__\ndb.execute(update(t).values(reason=None))",
        "a = Approval\nb = a.__table__\ndb.execute(delete(b))",
    ]
    for snippet in flagged:
        assert _offences(ast.parse(snippet)), snippet
    for clean in (
        "db.execute(select(AuditLog).where(x))",
        "db.execute(insert(AuditLog.__table__).values(id='x'))",
        "db.execute(update(Job).values(status='x'))",
        "db.query(Job).filter(x).delete()",
    ):
        assert not _offences(ast.parse(clean)), clean


def test_no_production_module_outside_privacy_writes_guarded_rows_after_insert() -> None:
    privacy_hits, offenders = 0, []
    for path in sorted((API_DIR / "app").rglob("*.py")):
        rel = path.relative_to(API_DIR)
        if "tests" in rel.parts:
            continue
        hits = _offences(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if rel.parts[:2] == ("app", "privacy"):
            privacy_hits += len(hits)  # the sanctioned writers (erasure; retention via it)
        else:
            offenders += [f"{rel}:{line}" for line in hits]
    assert offenders == []
    # The scan is not vacuous: it sees the sanctioned privacy statements.
    assert privacy_hits >= 4
