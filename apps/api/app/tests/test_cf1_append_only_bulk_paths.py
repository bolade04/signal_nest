"""Project Phase 5 CF-1 (W21, review r1 DATA-C1): no ORM path updates or deletes an append-only row.

The unit-of-work guard (``before_update`` / ``before_delete``) is covered by
``test_cf1_append_only_models.py``. This module covers every other ORM write path the review
found open at ``30fc045c`` — ORM-enabled UPDATE/DELETE statements, ``Query.update/delete``,
the legacy bulk API and an ORM upsert — for each append-only model (the three of CF-1 and,
since CF-2, ``creative_drafts`` and ``content_reviews``), and shows:

* each path raises :class:`~app.db.append_only.AppendOnlyViolation` and the stored row is
  byte-identical afterwards;
* the sanctioned Core statements on the *table* (privacy anonymisation, the foreign-key walk)
  still work under the guard, and an ORM INSERT (plain or ``ON CONFLICT DO NOTHING``) is not
  refused;
* **static pin:** no production module outside ``app/privacy/`` builds an UPDATE or DELETE
  (function, attribute-call or method form, or ``Query``) or an upsert over these models or
  their tables, writes a literal unqualified UPDATE / DELETE FROM / INSERT … DO UPDATE string
  on the guarded tables, calls the bulk API or calls ``delete_rows``, following model import
  aliases and annotated, tuple, list and walrus aliases — with an in-test control per form
  and look-alikes that must pass. It is a backstop to the runtime guard; what it does not
  pin is listed on the production-scan test and in ``app.db.append_only``;
* a **mapped subclass** of a guarded model is guarded too (unit of work, statements, bulk
  API).

The rows live in the P6-PRIV-1 world (``_privacy_support``), so the same tests run on SQLite
and — when ``TEST_POSTGRES_URL`` is set — on PostgreSQL, with real parent rows and foreign
keys enforced.
"""

from __future__ import annotations

import ast
import itertools
import os
import re
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
from app.creative.models import ContentReview, CreativeDraft
from app.db.append_only import AppendOnlyViolation
from app.tests._privacy_support import ORG_A, WS_A, PrivacyEnv, privacy_world

API_DIR = Path(__file__).resolve().parents[2]
GUARDED = (RecommendationBrief, Approval, AuditLog, CreativeDraft, ContentReview)
#: The column each probe tries to change (always allowed by the table's CHECKs).
FIELD = {
    RecommendationBrief: "summary",
    Approval: "note",
    AuditLog: "reason",
    CreativeDraft: "body",
    ContentReview: "warnings",
}

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
    """One fresh committed brief, approval, audit record, creative draft and content review
    (Core inserts, as a fixture)."""
    n = next(_serial)
    ids = {RecommendationBrief: f"cf1b-brief-{n}", Approval: f"cf1b-appr-{n}",
           AuditLog: f"cf1b-log-{n}", CreativeDraft: f"cf2b-draft-{n}",
           ContentReview: f"cf2b-review-{n}"}
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
        conn.execute(
            insert(CreativeDraft.__table__).values(
                id=ids[CreativeDraft], organization_id=ORG_A, workspace_id=WS_A,
                brief_id=ids[RecommendationBrief], opportunity_id="a1-opp", draft_version=1,
                channel="email", body="original", prompt_name="generate_creative",
                prompt_version="1.0.0", model_identifier="mock", input_hash="0" * 64,
                is_simulated=True, input_tokens=0, output_tokens=0, estimated_cost_usd=0,
                created_by_snapshot="u Name <u@example.test>",
            )
        )
        conn.execute(
            insert(ContentReview.__table__).values(
                id=ids[ContentReview], organization_id=ORG_A, workspace_id=WS_A,
                draft_id=ids[CreativeDraft], draft_version=1, risk_level="low",
                is_blocked=False, findings=[], warnings=["original"], blocked_claim_ids=[],
                required_disclaimers=[], disclaimers_resolved=True, passed=True,
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
    briefs, approvals, logs = (m.__table__ for m in (RecommendationBrief, Approval, AuditLog))
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
_TABLES = {m.__tablename__ for m in GUARDED}
_BULK = {"bulk_update_mappings", "bulk_insert_mappings", "bulk_save_objects"}
_INSERT_FNS = {"insert", "pg_insert", "sqlite_insert", "mysql_insert"}
_UPSERTS = {"on_conflict_do_update", "on_duplicate_key_update"}
_TABLE_ALT = "|".join(sorted(_TABLES))
_SQL_WRITE = re.compile(
    rf"\bUPDATE\s+\"?(?:{_TABLE_ALT})\"?(?![\w])"
    rf"|\bDELETE\s+FROM\s+\"?(?:{_TABLE_ALT})\"?(?![\w])"
    rf"|\bINSERT\s+INTO\s+\"?(?:{_TABLE_ALT})\"?(?![\w]).*"
    r"(?:\bDO\s+UPDATE\b|\bON\s+DUPLICATE\s+KEY\s+UPDATE\b)",
    re.IGNORECASE | re.DOTALL,
)


def _callee(node: ast.Call) -> str | None:
    """The called name: ``f(...)`` -> ``f``; ``mod.f(...)`` / ``obj.f(...)`` -> ``f``."""
    return getattr(node.func, "id", getattr(node.func, "attr", None))


def _is_guarded(node: ast.AST, names: set[str]) -> bool:
    """``Model``, ``Model.__table__`` or an alias of either."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.NamedExpr):
        return _is_guarded(node.value, names)
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__table__"
        and isinstance(node.value, ast.Name)
        and node.value.id in names
    )


def _bindings(tree: ast.AST):
    """Every (target name, value) the module binds: ``=`` (tuple and list targets element-wise),
    annotated ``=``, and walrus ``:=``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        elif isinstance(node, ast.NamedExpr):
            pairs = [(node.target, node.value)]
        else:
            continue
        for target, value in pairs:
            if isinstance(target, ast.Name):
                yield target.id, value
            elif (
                isinstance(target, ast.Tuple | ast.List)
                and isinstance(value, ast.Tuple | ast.List)
                and len(target.elts) == len(value.elts)
            ):
                for elt, val in zip(target.elts, value.elts, strict=True):
                    if isinstance(elt, ast.Name):
                        yield elt.id, val


def _guarded_names(tree: ast.AST) -> set[str]:
    """The guarded model names plus every alias of one in this module, to a fixed point:
    ``from ... import Approval as Ap``, ``t = AuditLog.__table__``, ``t: Table = ...``,
    ``a, b = Approval.__table__, ...`` and ``(t := Approval.__table__)``."""
    names = set(_MODELS)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname for a in node.names if a.asname and a.name in _MODELS}
    bindings = list(_bindings(tree))
    while True:
        found = {name for name, value in bindings if _is_guarded(value, names)}
        if found <= names:
            return names
        names |= found


def _guarded_insert(node: ast.AST, names: set[str]) -> bool:
    """An ``insert(...)`` / ``pg_insert(...)`` / ... call of a guarded name inside ``node``."""
    return any(
        isinstance(inner, ast.Call)
        and _callee(inner) in _INSERT_FNS
        and any(_is_guarded(a, names) for a in inner.args)
        for inner in ast.walk(node)
    )


def _docstrings(tree: ast.AST) -> set[int]:
    """ids of the docstring constants (prose, never executed SQL)."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _chain_root(node: ast.AST) -> ast.AST:
    while isinstance(node, ast.Attribute | ast.Call):
        node = node.value if isinstance(node, ast.Attribute) else node.func
    return node


def _offences(tree: ast.AST) -> list[int]:
    names = _guarded_names(tree)
    upsert_vars = {name for name, value in _bindings(tree) if _guarded_insert(value, names)}
    prose = _docstrings(tree)
    lines = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in prose
            and _SQL_WRITE.search(" ".join(node.value.split()))
        ):
            lines.append(node.lineno)  # UPDATE / DELETE FROM / INSERT ... DO UPDATE as SQL text
            continue
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node)
        func = node.func
        if name in _BULK or name == "delete_rows":
            lines.append(node.lineno)
        elif name in {"update", "delete"}:
            # update(Model) / sa.update(Model) / sqlalchemy.delete(Model.__table__)
            if any(_is_guarded(a, names) for a in node.args):
                lines.append(node.lineno)
            # Model.__table__.update() / .delete() (method form)
            elif isinstance(func, ast.Attribute) and _is_guarded(func.value, names):
                lines.append(node.lineno)
            # session.query(Model)....update() / .delete()
            elif isinstance(func, ast.Attribute) and any(
                isinstance(inner, ast.Call)
                and _callee(inner) == "query"
                and any(_is_guarded(a, names) for a in inner.args)
                for inner in ast.walk(func.value)
            ):
                lines.append(node.lineno)
        elif name in _UPSERTS and isinstance(func, ast.Attribute):
            # insert(Model)....on_conflict_do_update(...), chained or through a variable
            root = _chain_root(func.value)
            if _guarded_insert(func.value, names) or (
                isinstance(root, ast.Name) and root.id in upsert_vars
            ):
                lines.append(node.lineno)
    return lines


#: Each form the pin forbids (review r1 DATA-C1 b; review r2 DATA-C4 b).
FLAGGED_FORMS = {
    "function form": "db.execute(update(AuditLog).values(reason=None))",
    "function form on the table": "db.execute(update(Approval.__table__).values(note=None))",
    "delete function form": "db.execute(delete(RecommendationBrief).where(x))",
    "attribute-call form": (
        "import sqlalchemy as sa\ndb.execute(sa.update(Approval).values(note=None))"
    ),
    "attribute-call delete on the table": "db.execute(sqlalchemy.delete(AuditLog.__table__))",
    "method form": "db.execute(AuditLog.__table__.delete())",
    "method form update": "db.execute(Approval.__table__.update().values(note=None))",
    "Query.update": "db.query(RecommendationBrief).filter(x).update({'summary': 's'})",
    "Query.delete": "db.query(AuditLog).filter(x).delete()",
    "bulk_update_mappings": "db.bulk_update_mappings(Approval, rows)",
    "bulk_save_objects": "db.bulk_save_objects(objs)",
    "assignment alias": "t = AuditLog.__table__\ndb.execute(update(t).values(reason=None))",
    "alias of an alias": "a = Approval\nb = a.__table__\ndb.execute(delete(b))",
    "import alias": "from app.approvals.models import Approval as Ap\ndb.execute(update(Ap))",
    "annotated alias": "t: Table = AuditLog.__table__\ndb.execute(delete(t))",
    "tuple alias": "a, b = Approval.__table__, AuditLog.__table__\ndb.execute(delete(a))",
    "list alias": "[a, b] = [Approval.__table__, AuditLog.__table__]\ndb.execute(delete(b))",
    "walrus alias": "db.execute(delete(t := Approval.__table__))\ndb.execute(delete(t))",
    "chained ORM upsert": (
        "conn.execute(pg_insert(Approval).values(**r)"
        ".on_conflict_do_update(index_elements=['id'], set_={'note': 'n'}))"
    ),
    "chained Core upsert": (
        "db.execute(pg_insert(RecommendationBrief.__table__).values(**r)"
        ".on_conflict_do_update(index_elements=['id'], set_={'summary': 's'}))"
    ),
    "upsert through a variable": (
        "stmt = sqlite_insert(AuditLog).values(**r)\n"
        "db.execute(stmt.on_conflict_do_update(index_elements=['id'], set_={'reason': None}))"
    ),
    "mysql upsert": "db.execute(insert(Approval).values(**r).on_duplicate_key_update(note='n'))",
    "SQL text UPDATE": "db.execute(text('update  approvals set note = null'))",
    "SQL text DELETE": "db.execute(text('DELETE FROM audit_logs WHERE id = :i'))",
    "SQL text upsert": (
        'SQL = """INSERT INTO recommendation_briefs (id) VALUES (:i)\n'
        '    ON CONFLICT (id) DO UPDATE SET summary = :s"""'
    ),
    "delete_rows of a guarded table": "delete_rows(db, Approval.__table__, ids)",
    "delete_rows of any table": "delete_rows(db, Opportunity.__table__, ids)",
}
#: Look-alikes the pin must not flag.
CLEAN_FORMS = {
    "a select": "db.execute(select(AuditLog).where(x))",
    "an insert": "db.execute(insert(AuditLog.__table__).values(id='x'))",
    "ON CONFLICT DO NOTHING": "db.execute(insert(Approval).values(**r).on_conflict_do_nothing())",
    "another model's update": "db.execute(update(Job).values(status='x'))",
    "another model's upsert": (
        "db.execute(pg_insert(Job).values(**r).on_conflict_do_update(index_elements=['id']))"
    ),
    "another model's Query.delete": "db.query(Job).filter(x).delete()",
    "a SELECT string": "SQL = 'SELECT * FROM audit_logs'",
    "a similar table name": "SQL = 'UPDATE approvals_archive SET x = 1'",
    "an INSERT without DO UPDATE": "SQL = 'INSERT INTO approvals (id) VALUES (:i)'",
    "a docstring": 'def f():\n    """Never UPDATE approvals here."""\n    return 1',
}


@pytest.mark.parametrize("form", list(FLAGGED_FORMS))
def test_the_detector_fires_on_every_form(form: str) -> None:
    assert _offences(ast.parse(FLAGGED_FORMS[form])), form


@pytest.mark.parametrize("form", list(CLEAN_FORMS))
def test_the_detector_ignores_look_alikes(form: str) -> None:
    assert not _offences(ast.parse(CLEAN_FORMS[form])), form


def test_no_production_module_outside_privacy_writes_guarded_rows_after_insert() -> None:
    """A backstop to the runtime guard, not the enforcement layer. It follows **model** import
    aliases and annotated, tuple and walrus aliases, and matches only literal, unqualified
    ``UPDATE`` / ``DELETE FROM`` / ``INSERT … DO UPDATE`` strings on the guarded tables.

    Not pinned: the statement functions (``update``, ``delete``, ``insert``, dialect inserts)
    imported under another name; models referenced through a module (``models.Approval``);
    tables built or reflected by name (``table("approvals")``, ``Table("approvals", …)``) or
    reached through ``Base.metadata``, ``getattr``, ``inspect(...).local_table`` or
    ``Model.__mapper__.local_table``; tables passed as parameters; SQL that schema-qualifies
    the table or uses TRUNCATE or MERGE; and SQL assembled at run time. (The guarded,
    development-only demo reset in ``app.db.seed`` writes through ``Base.metadata`` and is the
    one sanctioned writer outside ``app/privacy/``.)"""
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
