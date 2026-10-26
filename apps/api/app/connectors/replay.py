"""Replay feed captures through the live connector path (P6-CI-11).

The required Integration smoke gate must exercise the product's real data path, not
the fixture library, without any third-party egress from CI. This module serves the
capture corpus in ``app/connectors/captures/`` from a **loopback HTTP server** and
points the live RSS connector at it, so every replayed byte travels the same path a
live fetch does: source registry → fail-closed gate → egress guard (URL check,
address check, pinned connect, status / media-type / size / encoding checks,
conditional request) → feed parser → provenance-stamped signals → scout pipeline.

The only difference from live egress is ``connector_rss_replay_origin``, which admits
exactly this server's ``http://127.0.0.1:PORT`` origin and which settings validation
refuses outside the development and test environments.

Each capture in ``captures/manifest.json`` names the source it stands for and is
SHA-256-pinned. ``"synthetic": true`` marks bytes written for tests (IANA-reserved
example domains, no real publisher): their signals are stored as **simulated**, so a
synthetic corpus can never be presented as real data. A recorded capture
(``"synthetic": false``) turns the replay into real data only when an approved, enabled
:data:`APPROVED_SOURCES` record with the same key and feed URL covers its markets; it
then carries that record's approval and terms. Without one it replays as simulated.
Committing a recorded capture needs that source's terms to allow it (see
``docs/operations/live-data-sources.md``).

Commands (run from ``apps/api``)::

    python -m app.connectors.replay seed    # replay into the seeded demo database
    python -m app.connectors.replay serve   # serve the corpus for local debugging
    python -m app.connectors.replay capture SOURCE_KEY OUT_DIR   # operator only

``capture`` is the one command here that performs **live egress**: it fetches one
approved, enabled, not-taken-down source from :data:`APPROVED_SOURCES` through the same
egress guard and writes the bytes plus a ``"synthetic": false`` manifest entry. It is an
operator act under that source's approval and terms, never run by CI or tests.

``seed`` adds, to the demo workspace, one location + scout request per corpus market
and one for :data:`UNCOVERED_MARKET` (no source at all), runs each through the
pipeline, and exits non-zero unless every corpus market reports
``covered/live_sources`` with at least one opportunity and the uncovered market
reports ``not_covered/no_source_for_market``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy.orm import Session

from app.connectors.egress import SafeFeedFetcher
from app.connectors.live import SourceTarget, build_fetcher
from app.connectors.rss import parse_feed
from app.connectors.sources import APPROVED_SOURCES, LegalStatus, SourceRecord
from app.core.config import Settings, get_settings

CORPUS_DIR = Path(__file__).with_name("captures")
#: Approval reference stamped on replay records: a test value, never a real approval.
TEST_APPROVAL_REFERENCE = "TEST-REPLAY-ONLY"
_TEST_APPROVED_ON = date(2026, 10, 20)
#: A market no source and no demo fixture covers.
UNCOVERED_MARKET = "Winnipeg, MB"


class CorpusError(ValueError):
    """The capture corpus is malformed or a capture's bytes do not match its digest."""


@dataclass(frozen=True)
class Capture:
    capture_id: str
    source_key: str
    display_name: str
    publisher: str
    feed_url: str
    markets: tuple[str, ...]
    tos_url: str
    path: Path
    content_type: str
    etag: str | None
    last_modified: str | None
    sha256: str
    synthetic: bool
    captured_at: str | None
    keywords: tuple[str, ...]

    def body(self) -> bytes:
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise CorpusError(f"{self.capture_id}: bytes do not match the manifest sha256")
        return data


def load_corpus(directory: Path | None = None) -> list[Capture]:
    directory = directory or CORPUS_DIR
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        entries = manifest["captures"]
        captures = [
            Capture(
                capture_id=e["capture_id"],
                source_key=e["source_key"],
                display_name=e["display_name"],
                publisher=e["publisher"],
                feed_url=e["feed_url"],
                markets=tuple(e["markets"]),
                tos_url=e["tos_url"],
                path=directory / e["file"],
                content_type=e["content_type"],
                etag=e.get("etag"),
                last_modified=e.get("last_modified"),
                sha256=e["sha256"],
                synthetic=bool(e["synthetic"]),
                captured_at=e.get("captured_at"),
                keywords=tuple(e.get("keywords") or ()),
            )
            for e in entries
        ]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise CorpusError(f"unreadable capture manifest: {exc}") from exc
    ids = [c.capture_id for c in captures]
    if len(ids) != len(set(ids)):
        raise CorpusError("duplicate capture_id")
    for capture in captures:
        if Path(capture.path.name) != Path(capture.path.relative_to(directory)):
            raise CorpusError(f"{capture.capture_id}: file must sit in the corpus directory")
        capture.body()
    return captures


def approved_record_for(
    capture: Capture, sources: tuple[SourceRecord, ...]
) -> SourceRecord | None:
    """The activatable registry record a recorded capture stands for, if any.

    A capture is real data only when an approved, enabled source with the same key and
    feed URL is cleared for every market the capture names; the manifest's
    ``"synthetic": false`` alone proves nothing (anyone can set the flag).
    """
    for source in sources:
        if (
            source.source_key == capture.source_key
            and source.feed_url == capture.feed_url
            and source.is_activatable()
            and all(source.serves_market(m) for m in capture.markets)
        ):
            return source
    return None


def replay_targets(
    origin: str,
    directory: Path | None = None,
    *,
    sources: tuple[SourceRecord, ...] | None = None,
) -> list[SourceTarget]:
    """One live-connector target per capture, fetched from ``origin``.

    A recorded capture that matches an approved source (:func:`approved_record_for`)
    replays as real data under that source's own record: its approval reference, date,
    terms and attribution. Every other capture — synthetic, or marked recorded without
    a matching approved source — replays as **simulated** under a test record.
    """
    sources = APPROVED_SOURCES if sources is None else sources
    targets: list[SourceTarget] = []
    for capture in load_corpus(directory):
        approved = None if capture.synthetic else approved_record_for(capture, sources)
        provenance: tuple[tuple[str, object], ...] = (
            ("capture_id", capture.capture_id),
            ("captured_at", capture.captured_at),
        )
        if approved is not None:
            record = approved
        else:
            record = SourceRecord(
                source_key=capture.source_key,
                connector="rss_news",
                display_name=capture.display_name,
                publisher=capture.publisher,
                feed_url=capture.feed_url,
                markets=capture.markets,
                tos_url=capture.tos_url,
                approval_reference=TEST_APPROVAL_REFERENCE,
                approved_on=_TEST_APPROVED_ON,
                legal_status=LegalStatus.APPROVED,
                enabled=True,
                attribution=f"Replayed capture {capture.capture_id}",
            )
            if not capture.synthetic:
                provenance += (("unverified_recorded_capture", True),)
        targets.append(
            SourceTarget(
                source=record,
                fetch_url=f"{origin.rstrip('/')}/captures/{capture.capture_id}",
                delivery="replay",
                is_simulated=approved is None,
                provenance=provenance,
            )
        )
    return targets


class _Handler(BaseHTTPRequestHandler):
    server: _CorpusServer

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        prefix = "/captures/"
        capture = None
        if self.path.startswith(prefix):
            capture = self.server.captures.get(self.path[len(prefix) :])
        if capture is None:
            self._reply(404, b"", {})
            return
        validators = {"ETag": capture.etag, "Last-Modified": capture.last_modified}
        headers = {k: v for k, v in validators.items() if v}
        if (capture.etag and self.headers.get("If-None-Match") == capture.etag) or (
            capture.last_modified
            and self.headers.get("If-Modified-Since") == capture.last_modified
        ):
            self._reply(304, b"", headers)
            return
        self._reply(200, capture.body(), {**headers, "Content-Type": capture.content_type})

    def _reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.server.hits.append((self.path, status, self.headers.get("User-Agent")))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


class _CorpusServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, captures: list[Capture]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.captures = {c.capture_id: c for c in captures}
        self.hits: list[tuple[str, int, str | None]] = []


class ReplayServer:
    """The corpus on ``http://127.0.0.1:<ephemeral port>``, for a ``with`` block."""

    def __init__(self, directory: Path | None = None) -> None:
        self._server = _CorpusServer(load_corpus(directory))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def hits(self) -> list[tuple[str, int, str | None]]:
        return list(self._server.hits)

    def __enter__(self) -> ReplayServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def seed_replay(
    directory: Path | None = None, *, session_factory: Callable[[], Session] | None = None
) -> dict[str, dict]:
    """Add the replay locations + scout requests to the demo workspace and run them.

    Requires the demo seed (``python -m app.db.seed``) and settings already pointing
    at a running replay origin with live egress on (``main`` arranges both). The
    demo seed's target guard applies: development/test SQLite only.
    """
    from sqlalchemy import select

    from app.brands.service import get_primary_brand
    from app.core.enums import ScoutRequestStatus
    from app.db.seed import DEMO_EMAIL, _require_safe_seed_target
    from app.db.session import SessionLocal
    from app.jobs.pipeline import _run
    from app.locations.models import BusinessLocation
    from app.organizations.models import OrganizationMember, User, Workspace
    from app.scouting_requests.models import ScoutRequest

    factory = session_factory or SessionLocal
    _require_safe_seed_target(session_factory=factory)
    captures = load_corpus(directory)
    keywords: dict[str, set[str]] = {}
    for capture in captures:
        for market in capture.markets:
            keywords.setdefault(market, set()).update(capture.keywords)
    keywords.setdefault(UNCOVERED_MARKET, {"coffee"})

    db = factory()
    try:
        user = db.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is None:
            raise CorpusError("the demo seed has not run (no demo user)")
        member = db.scalar(
            select(OrganizationMember).where(OrganizationMember.user_id == user.id)
        )
        workspace = db.scalar(
            select(Workspace)
            .where(Workspace.organization_id == member.organization_id)
            .order_by(Workspace.created_at)
        )
        brand = get_primary_brand(db, workspace.id)
        summary: dict[str, dict] = {}
        for market in sorted(keywords):
            city, _, region = (part.strip() for part in market.partition(","))
            location = BusinessLocation(
                organization_id=workspace.organization_id,
                workspace_id=workspace.id,
                brand_id=brand.id,
                name=f"Replay — {city}",
                city=city,
                state_province=region or None,
                is_active=True,
            )
            db.add(location)
            db.flush()
            request = ScoutRequest(
                organization_id=workspace.organization_id,
                workspace_id=workspace.id,
                brand_id=brand.id,
                location_id=location.id,
                name=f"{city} replay scout",
                status=ScoutRequestStatus.QUEUED.value,
                source_types=[],
                keywords=sorted(keywords[market]),
                resolved_market=market,
                notes="Replay of feed captures through the live connector (P6-CI-11).",
            )
            db.add(request)
            db.flush()
            summary[market] = _run(db, request.id)
        db.commit()
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _seed_problems(summary: dict[str, dict], corpus_markets: set[str]) -> list[str]:
    problems: list[str] = []
    for market, stats in summary.items():
        coverage = stats.get("coverage") or {}
        if market in corpus_markets:
            if coverage.get("reason") != "live_sources" or coverage.get("sources_failed"):
                problems.append(f"{market}: expected covered/live_sources, got {coverage}")
            if stats.get("signals_analyzed", 0) < 1 or stats.get("opportunities", 0) < 1:
                problems.append(f"{market}: replay produced no analyzed signal/opportunity")
        elif coverage.get("reason") != "no_source_for_market":
            problems.append(f"{market}: expected not_covered/no_source_for_market, got {coverage}")
    return problems


def capture_source(
    source_key: str,
    out_dir: Path,
    *,
    takedowns: frozenset[str],
    settings: Settings | None = None,
    sources: tuple[SourceRecord, ...] = APPROVED_SOURCES,
    fetcher: SafeFeedFetcher | None = None,
) -> dict:
    """Record one approved source's feed (live egress) and return its manifest entry."""
    settings = settings or get_settings()
    if settings.connector_rss_replay_origin:
        raise CorpusError("capture reads a live source; unset connector_rss_replay_origin")
    if settings.connector_rss_kill_switch or not settings.connector_rss_live_enabled:
        raise CorpusError("live egress is disabled (connector_rss_live_enabled / kill switch)")
    matches = [s for s in sources if s.source_key == source_key]
    if not matches:
        raise CorpusError(f"{source_key}: not in APPROVED_SOURCES")
    source = matches[0]
    if not source.is_activatable() or source_key in takedowns:
        raise CorpusError(f"{source_key}: not enabled and approved, or taken down")
    target = SourceTarget(source=source, fetch_url=source.feed_url, delivery="live")
    response = (fetcher or build_fetcher(settings)).fetch(
        source.feed_url, allowed_hosts=target.allowed_hosts
    )
    parse_feed(response.body)  # never record something the connector cannot read
    stamp = response.retrieved_at.strftime("%Y%m%d")
    capture_id = f"{source_key}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{capture_id}.xml").write_bytes(response.body)
    return {
        "capture_id": capture_id,
        "source_key": source.source_key,
        "display_name": source.display_name,
        "publisher": source.publisher,
        "feed_url": source.feed_url,
        "markets": list(source.markets),
        "tos_url": source.tos_url,
        "file": f"{capture_id}.xml",
        "content_type": response.media_type,
        "etag": response.etag,
        "last_modified": response.last_modified,
        "sha256": response.sha256,
        "synthetic": False,
        "captured_at": response.retrieved_at.isoformat(),
        "keywords": [],
    }


def _point_settings_at(origin: str) -> None:
    os.environ["CONNECTOR_RSS_REPLAY_ORIGIN"] = origin
    os.environ["CONNECTOR_RSS_LIVE_ENABLED"] = "true"
    # Live feeds are RSS: the per-workspace capability decision (P6-CAP-1) needs the
    # global RSS flag on as well.
    os.environ["CONNECTOR_RSS_ENABLED"] = "true"
    get_settings.cache_clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.connectors.replay")
    parser.add_argument("command", choices=("seed", "serve", "capture"))
    parser.add_argument("source_key", nargs="?")
    parser.add_argument("out_dir", nargs="?", type=Path)
    parser.add_argument(
        "--corpus", type=Path, default=None, help="a corpus directory other than the bundled one"
    )
    args = parser.parse_args(argv)

    if args.command == "capture":
        if not args.source_key or args.out_dir is None:
            parser.error("capture needs SOURCE_KEY and OUT_DIR")
        from app.connectors.takedown import active_takedowns
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            takedowns = active_takedowns(db)
        entry = capture_source(args.source_key, args.out_dir, takedowns=takedowns)
        print(json.dumps(entry, indent=2))
        return 0

    corpus = args.corpus.resolve() if args.corpus is not None else None
    if corpus is not None:
        # Run as ``-m`` this file is ``__main__``; the live connector imports the module
        # under its package name, so the corpus is set there for both sides to agree.
        importlib.import_module("app.connectors.replay").CORPUS_DIR = corpus
    with ReplayServer(corpus) as server:
        _point_settings_at(server.origin)
        if args.command == "serve":
            print(f"serving the capture corpus on {server.origin}/captures/<capture_id>")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                return 0
        captures = load_corpus(corpus)
        summary = seed_replay(corpus)
        hits = server.hits
    corpus_markets = {m for c in captures for m in c.markets}
    problems = _seed_problems(summary, corpus_markets)
    synthetic = sorted(c.capture_id for c in captures if c.synthetic)
    unverified = sorted(
        c.capture_id
        for c in captures
        if not c.synthetic and approved_record_for(c, APPROVED_SOURCES) is None
    )
    print(
        json.dumps(
            {
                "summary": summary,
                "replay_requests": len(hits),
                "synthetic_captures": synthetic,
                "recorded_captures": sorted(c.capture_id for c in captures if not c.synthetic),
                "unverified_recorded_captures": unverified,
                "problems": problems,
            },
            indent=2,
            default=str,
        )
    )
    if synthetic:
        print(
            "NOTICE: the replay corpus holds synthetic captures; their signals are "
            "simulated, so this replay proves the live code path, not real data.",
            file=sys.stderr,
        )
    if unverified:
        print(
            "NOTICE: recorded captures without a matching approved source replay as "
            f"simulated: {', '.join(unverified)}",
            file=sys.stderr,
        )
    return 1 if problems or not hits else 0


if __name__ == "__main__":
    sys.exit(main())
