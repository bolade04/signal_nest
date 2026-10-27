"""Vector search adapter.

Similarity is brute-force cosine over an in-memory numpy index (zero deps); the
pipeline builds one per scout run for its dedupe step. It is in-process per-run
similarity, bounded at ``settings.vector_inprocess_max_signals_per_run`` signals per
run (P6-PLAT-1 strategy B, operator decision 2026-10-27: an engineering capacity
bound, not a quota - a run that would index more is refused before any work by
:func:`check_inprocess_bound`, never truncated or chunked). There is no cross-run,
cross-workspace or history-wide similarity. ``vector_backend=pgvector`` names a
database-backed path this build does not implement: settings refuse it, and
:func:`build_index` raises rather than quietly substituting brute force. The
deterministic embedding function is a lightweight hashing embedder so
dedup/clustering works offline without a model download; real deployments swap in
an embedding provider (P6-LLM-7, deferred).
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

from app.core.config import Settings, get_settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class VectorBackendUnavailableError(RuntimeError):
    """The configured vector backend has no implementation in this build."""


#: Stable reason code of a scout run refused at the per-run bound. It is recorded on
#: the request's ``stats`` and as the job's error summary, so the refusal is visible.
SIGNAL_VOLUME_ABOVE_BOUND = "signal_volume_above_bound"


class SignalVolumeAboveBoundError(RuntimeError):
    """A run would index more signals than the in-process per-run bound allows.

    Raised by :func:`check_inprocess_bound` *before* the run's index is built: nothing
    is embedded, compared, truncated, chunked or partially deduplicated. The counts
    are safe aggregates (no content).
    """

    reason = SIGNAL_VOLUME_ABOVE_BOUND

    def __init__(self, *, scanned: int, bound: int, fetch_limits: list[dict] | None = None) -> None:
        self.scanned = scanned
        self.bound = bound
        #: Per-source fetch-limit facts of the refused run (``stats["fetch_limits"]``), so
        #: the refusal shows what each source delivered. ``None`` when not recorded.
        self.fetch_limits = fetch_limits
        super().__init__(
            f"{SIGNAL_VOLUME_ABOVE_BOUND}: {scanned} signals exceed the per-run "
            f"in-process bound of {bound}"
        )


def check_inprocess_bound(signal_count: int, settings: Settings | None = None) -> int:
    """Return the per-run bound when ``signal_count`` signals fit in one run.

    Raises :class:`SignalVolumeAboveBoundError` otherwise. The bound is
    ``settings.vector_inprocess_max_signals_per_run`` (validated at or below
    ``VECTOR_INPROCESS_HARD_CEILING``); it is applied per run, per workspace, so one
    workspace's refusal never touches another's run.
    """
    settings = settings or get_settings()
    bound = settings.vector_inprocess_max_signals_per_run
    if signal_count > bound:
        raise SignalVolumeAboveBoundError(scanned=signal_count, bound=bound)
    return bound


def embed_text(text: str, dim: int | None = None) -> list[float]:
    """Deterministic hashing embedding. Same text -> same vector."""
    dim = dim or get_settings().embedding_dim
    vec = np.zeros(dim, dtype=np.float32)
    tokens = _TOKEN_RE.findall(text.lower())
    for tok in tokens:
        h = int(hashlib.sha1(tok.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a), np.array(b)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    return float(np.dot(va, vb) / denom) if denom else 0.0


class VectorIndex(Protocol):
    def add(self, key: str, vector: list[float]) -> None: ...
    def search(self, vector: list[float], top_k: int = 5) -> list[tuple[str, float]]: ...


class BruteForceIndex:
    def __init__(self) -> None:
        self._items: dict[str, list[float]] = {}

    def add(self, key: str, vector: list[float]) -> None:
        self._items[key] = vector

    def search(self, vector: list[float], top_k: int = 5) -> list[tuple[str, float]]:
        scored = [(k, cosine(vector, v)) for k, v in self._items.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def build_index(settings: Settings | None = None) -> VectorIndex:
    """Build an empty index for the configured ``vector_backend``.

    Only ``bruteforce`` exists. Any other backend raises
    :class:`VectorBackendUnavailableError` - it is never silently replaced by brute
    force, which is what made a ``pgvector`` setting indistinguishable from the
    default (P6-PLAT-1).
    """
    settings = settings or get_settings()
    if settings.vector_backend == "bruteforce":
        return BruteForceIndex()
    raise VectorBackendUnavailableError(
        f"vector_backend={settings.vector_backend} is not implemented in this build"
    )
