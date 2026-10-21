"""Vector search adapter.

Similarity is brute-force cosine over an in-memory numpy index (zero deps); the
pipeline builds one per scout run for its dedupe step. ``vector_backend=pgvector``
names a database-backed path this build does not implement (P6-PLAT-1): settings
refuse it, and :func:`build_index` raises rather than quietly substituting brute
force. The deterministic embedding function is a lightweight hashing embedder so
dedup/clustering works offline without a model download; real deployments swap in
an embedding provider.
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
