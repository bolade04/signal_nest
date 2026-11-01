"""Per-source feed cache: politeness interval, conditional GET and cool-down.

One entry per source key holds the last good feed body with its ``ETag`` /
``Last-Modified`` validators. The live connector consults it before any request:

* a body younger than the source's minimum fetch interval is reused with **no**
  request at all (many scout runs in one market cost one fetch per interval);
* otherwise the request is conditional, and a ``304 Not Modified`` reuses the cached
  body — validators are only ever sent while their body is held, so a 304 can always
  be answered;
* every failed fetch - once its bounded retries are spent, whatever the failure: a
  ``5xx``, a timeout or connection failure, a ``404`` or other ``4xx``, a ``429`` with or
  without ``Retry-After``, a refused response, or a body the connector cannot read -
  starts a cool-down (:meth:`FeedCache.fetch_failed`) during which the source is not
  contacted. It lasts one politeness interval and doubles with each further consecutive
  failure, up to :data:`MAX_FAILURE_BACKOFF_DOUBLINGS` doublings (bounded back-off); a
  ``Retry-After`` can only lengthen it. A good read or a ``304`` ends the streak. So a
  failing publisher costs at most one fetch (``connector_rss_max_attempts`` requests)
  per interval, never one per scout run (#255 review r2-MINOR-1);
* while a source cools down, its last good body (if any) is served; a body the
  connector cannot read is never stored, so it is neither re-served nor re-fetched on
  every run.

The cache is process-local and bounded; each worker process is polite on its own.
A source's entry is dropped (:meth:`FeedCache.forget`) by the process that performs
its takedown, and by every other process (the worker that runs scouts) at its next
scout run, before any fetch (:func:`app.connectors.live.live_connector_for`). Until
then a taken-down source's bytes may sit in another process's memory, unused: no run
reads a taken-down key.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.connectors.base import FailureKind
from app.connectors.egress import FeedResponse, Validators

#: Longest cool-down honoured from a source's Retry-After, and the ceiling of the failure
#: back-off unless the source's own interval is longer (a day).
MAX_COOL_DOWN_SECONDS = 86400.0

#: Bounded back-off after failed fetches: the first failure holds a source off for one
#: politeness interval, and each further consecutive failure doubles that, at most this
#: many times (2 -> at most four intervals; one hour at the default 900 s interval).
MAX_FAILURE_BACKOFF_DOUBLINGS = 2


def failure_hold_off(interval_seconds: float, consecutive_failures: int) -> float:
    """How long a source is not contacted after its ``consecutive_failures``-th failed
    fetch in a row: one politeness interval, doubled per further consecutive failure up to
    :data:`MAX_FAILURE_BACKOFF_DOUBLINGS` times, and never beyond a day unless the
    interval itself is longer - never shorter than one interval."""
    interval = max(interval_seconds, 0.0)
    doublings = min(max(consecutive_failures, 1) - 1, MAX_FAILURE_BACKOFF_DOUBLINGS)
    return max(interval, min(interval * (1 << doublings), MAX_COOL_DOWN_SECONDS))


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
        self._cool_reason: dict[str, tuple[FailureKind, str]] = {}
        #: Consecutive failed fetches per source (reset by a good read or a 304).
        self._failures: dict[str, int] = {}
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
            self._cool_reason.pop(key, None)
            self._failures.pop(key, None)
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
            self._failures.pop(key, None)
            return feed

    def fetch_failed(
        self,
        key: str,
        interval_seconds: float,
        *,
        retry_after_seconds: float | None = None,
        kind: FailureKind = FailureKind.RATE_LIMITED,
        detail: str = "source is cooling down",
    ) -> float:
        """Record a failed fetch of ``key`` and start its cool-down; return its length.

        The cool-down is :func:`failure_hold_off` of the source's politeness interval and
        its consecutive failures; a ``Retry-After`` (capped at a day) can only lengthen
        it. ``kind``/``detail`` are what a run reports while the source cools down and no
        good body is held."""
        with self._lock:
            failures = self._failures.get(key, 0) + 1
            self._failures[key] = failures
            seconds = failure_hold_off(interval_seconds, failures)
            if retry_after_seconds is not None:
                asked = min(max(retry_after_seconds, 0.0), MAX_COOL_DOWN_SECONDS)
                seconds = max(seconds, asked)
            self._cool_until[key] = self._clock() + seconds
            self._cool_reason[key] = (kind, detail)
            return seconds

    def consecutive_failures(self, key: str) -> int:
        with self._lock:
            return self._failures.get(key, 0)

    def cooling(self, key: str) -> bool:
        with self._lock:
            until = self._cool_until.get(key)
            return until is not None and self._clock() < until

    def cooling_reason(self, key: str) -> tuple[FailureKind, str]:
        with self._lock:
            return self._cool_reason.get(key, (FailureKind.RATE_LIMITED, "source is cooling down"))

    def clear(self) -> None:
        with self._lock:
            self._feeds.clear()
            self._cool_until.clear()
            self._cool_reason.clear()
            self._failures.clear()

    def forget(self, key: str) -> None:
        with self._lock:
            self._feeds.pop(key, None)
            self._cool_until.pop(key, None)
            self._cool_reason.pop(key, None)
            self._failures.pop(key, None)


#: The process-wide cache used by the live connector.
feed_cache = FeedCache()
