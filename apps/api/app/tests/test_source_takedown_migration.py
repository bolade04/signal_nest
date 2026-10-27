"""Migration ``da25533622b2`` keeps a takedown's reach across a downgrade/upgrade (P6-PRIV-4).

The downgrade drops ``raw_signals.source_key`` / ``normalized_signals.source_key``; every
live signal still carries its source in ``raw_metadata`` / ``ingest_metadata``
(``provenance.source_key``). The upgrade reads that back, so after a round trip a
takedown still finds and purges every signal its source produced — instead of answering
"no such source" (or a verified zero) over rows it can no longer see.

Runs the real Alembic CLI against a throwaway SQLite database, as
``test_opportunity_feedback_migration.py`` does.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

API_DIR = Path(__file__).resolve().parents[2]
REVISION = "da25533622b2"
KEY = "metro-business"
#: The persistent suppression record (D-9d): key, time, counts, row bookkeeping.
MINIMAL_COLUMNS = {
    "id", "source_key", "taken_down_at", "purge_summary", "repeat_purges",
    "created_at", "updated_at",
}


def _previous_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    return script.get_revision(REVISION).down_revision


def _downgrade_confirmation(db_path: Path, args: tuple[str, ...]) -> list[str]:
    """The guard's confirmation for a downgrade of this temp database (computed per run;
    the digest itself is pinned in ``test_migrate_downgrade_target_guard.py``)."""
    if not args or args[0] != "downgrade":
        return []
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.db.downgrade_guard import (
        chain_identity,
        confirmation_token,
        live_identity,
        resolve_downgrade,
    )

    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    with engine.connect() as conn:
        heads = MigrationContext.configure(conn).get_current_heads()
        identity = live_identity(conn, url)
    engine.dispose()
    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    resolved, chain = resolve_downgrade(script, heads[0], args[1])
    token = confirmation_token(
        database_url=url,
        live_identity=identity,
        source_revision=heads[0],
        requested_target=args[1],
        resolved_destination=resolved,
        chain_identity=chain_identity(script, chain),
    )
    return ["-x", f"confirm={token}"]


def _alembic(db_path: Path, *args: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    confirm = _downgrade_confirmation(db_path, args)
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *confirm, *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _raw(con, row_id: str, metadata: dict, source_key: str | None) -> None:
    con.execute(
        "INSERT INTO raw_signals (id, source_type, content, raw_metadata, is_simulated,"
        " organization_id, workspace_id, scout_request_id, source_key)"
        " VALUES (?, 'rss_news', 'text', ?, 0, 'org-a', 'ws-a', 'req-a', ?)",
        (row_id, json.dumps(metadata), source_key),
    )


def _normalized(con, row_id: str, raw_id: str, metadata: dict, source_key: str | None) -> None:
    con.execute(
        "INSERT INTO normalized_signals (id, raw_signal_id, source_type, excerpt, content_hash,"
        " embedding, pre_analysis_score, is_noise, noise_reasons, is_duplicate, is_simulated,"
        " ingest_metadata, organization_id, workspace_id, scout_request_id, source_key)"
        " VALUES (?, ?, 'rss_news', 'text', 'h', '[]', 50, 0, '[]', 0, 0, ?,"
        " 'org-a', 'ws-a', 'req-a', ?)",
        (row_id, raw_id, json.dumps(metadata), source_key),
    )


def test_a_round_trip_restores_source_keys_so_a_takedown_still_reaches_them(tmp_path) -> None:
    db_path = tmp_path / "round-trip.db"
    _alembic(db_path, "upgrade", "head")
    live = {"market": "Toronto, ON", "provenance": {"source_key": KEY, "delivery": "live"}}
    con = sqlite3.connect(db_path)
    try:
        _raw(con, "raw-live", live, KEY)
        _normalized(con, "norm-live", "raw-live", live, KEY)
        _raw(con, "raw-fixture", {"market": "Dallas, TX"}, None)  # simulated: no source
        _raw(con, "raw-bad-key", {"provenance": {"source_key": "Not A Slug"}}, None)
        con.commit()
    finally:
        con.close()

    _alembic(db_path, "downgrade", _previous_revision())
    con = sqlite3.connect(db_path)
    try:
        assert "source_key" not in _columns(con, "raw_signals")
        assert "source_takedowns" not in {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()

    _alembic(db_path, "upgrade", "head")
    con = sqlite3.connect(db_path)
    try:
        raw = dict(con.execute("SELECT id, source_key FROM raw_signals"))
        norm = dict(con.execute("SELECT id, source_key FROM normalized_signals"))
        # D-9d: the migrated table is the suppression record only — no actor, reason or
        # reference column, hence no foreign key to users and no actor index.
        assert _columns(con, "source_takedowns") == MINIMAL_COLUMNS
        assert list(con.execute("PRAGMA foreign_key_list(source_takedowns)")) == []
        indexes = [r[1] for r in con.execute("PRAGMA index_list(source_takedowns)")]
        assert not any("actor" in name for name in indexes), indexes
    finally:
        con.close()
    assert raw == {"raw-live": KEY, "raw-fixture": None, "raw-bad-key": None}
    assert norm == {"norm-live": KEY}

    from app.connectors.takedown import source_signal_counts, takedown_source

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with sessionmaker(bind=engine)() as db:
            result = takedown_source(
                db, source_key=KEY, reference="TKD-RT", actor_user_id=None,
            )
            db.commit()
            assert result.purged["raw_signals"] == 1
            assert result.purged["normalized_signals"] == 1
            assert result.verified and source_signal_counts(db, KEY)["raw_signals"] == 0
            assert result.takedown.purge_summary == result.purged
            assert result.takedown.repeat_purges == []
    finally:
        engine.dispose()
    con = sqlite3.connect(db_path)
    try:
        assert {r[0] for r in con.execute("SELECT id FROM raw_signals")} == {
            "raw-fixture",
            "raw-bad-key",
        }
    finally:
        con.close()
