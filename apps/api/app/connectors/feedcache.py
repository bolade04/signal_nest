"""Per-source feed cache: politeness interval, conditional GET and cool-down.

One entry per source key holds the last good feed body with its ``ETag`` /
``Last-Modified`` validators. The live connector consults it before any request:

* a body younger than the source's minimum fetch interval is reused with **no**
  request at all (many scout runs in one market cost one fetch per interval);
* otherwise the request is conditional, and a ``304 Not Modified`` reuses the cached
  body — validators are only ever sent while their body is held, so a 304 can always
  be answered;
* a ``429`` / ``503`` with ``Retry-After`` starts a cool-down during which the source
  is not contacted.

The cache is process-local and bounded; each worker process is polite on its own.
A takedown drops the source's entry (:meth:`FeedCache.forget`) so no cached
publisher bytes outlive it in memory.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.connectors.egress import FeedResponse, Validators

#: Longest cool-down honoured from a source's Retry-After (a day).
MAX_COOL_DOWN_SECONDS = 86400.0


@dataclass(frozen=True)
class CachedFeed:
    body: bytes
    etag: str | None
    last_modified: str | None
    sha256: str
    final_url: str
    retrieved_at: datetime
    fetched_at: float


class FeedCache:
    def __init__(
        self, *, max_entries: int = 64, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._feeds: OrderedDict[str, CachedFeed] = OrderedDict()
        self._cool_until: dict[str, float] = {}
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()

    def fresh(self, key: str, min_interval_seconds: float) -> CachedFeed | None:
        with self._lock:
            feed = self._feeds.get(key)
            if feed is not None and self._clock() - feed.fetched_at < min_interval_seconds:
                return feed
            return None

    def cached(self, key: str) -> CachedFeed | None:
        with self._lock:
            return self._feeds.get(key)

    def validators(self, key: str) -> Validators | None:
        feed = self.cached(key)
        if feed is None or not (feed.etag or feed.last_modified):
            return None
        return Validators(etag=feed.etag, last_modified=feed.last_modified)

    def store(self, key: str, response: FeedResponse) -> CachedFeed:
        feed = CachedFeed(
            body=response.body,
            etag=response.etag,
            last_modified=response.last_modified,
            sha256=response.sha256,
            final_url=response.final_url,
            retrieved_at=response.retrieved_at,
            fetched_at=self._clock(),
        )
        with self._lock:
            self._feeds[key] = feed
            self._feeds.move_to_end(key)
            while len(self._feeds) > self._max:
                self._feeds.popitem(last=False)
            self._cool_until.pop(key, None)
        return feed

    def revalidated(self, key: str, response: FeedResponse) -> CachedFeed | None:
        """Refresh the held body after a 304; ``None`` if nothing is held."""
        with self._lock:
            feed = self._feeds.get(key)
            if feed is None:
                return None
            feed = CachedFeed(
                body=feed.body,
                etag=response.etag or feed.etag,
                last_modified=response.last_modified or feed.last_modified,
                sha256=feed.sha256,
                final_url=feed.final_url,
                retrieved_at=response.retrieved_at,
                fetched_at=self._clock(),
            )
            self._feeds[key] = feed
            self._feeds.move_to_end(key)
            return feed

    def cool_down(self, key: str, seconds: float) -> None:
        with self._lock:
            self._cool_until[key] = self._clock() + min(max(seconds, 0.0), MAX_COOL_DOWN_SECONDS)

    def cooling(self, key: str) -> bool:
        with self._lock:
            until = self._cool_until.get(key)
            return until is not None and self._clock() < until

    def clear(self) -> None:
        with self._lock:
            self._feeds.clear()
            self._cool_until.clear()

    def forget(self, key: str) -> None:
        with self._lock:
            self._feeds.pop(key, None)
            self._cool_until.pop(key, None)


#: The process-wide cache used by the live connector.
feed_cache = FeedCache()
