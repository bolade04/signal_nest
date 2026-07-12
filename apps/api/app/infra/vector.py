"""Vector search adapter.

Default: brute-force cosine similarity over an in-memory numpy index (zero deps).
Full mode: pgvector-backed similarity. The deterministic embedding function is a
lightweight hashing embedder so semantic dedup/clustering works offline without a
model download; real deployments swap in an embedding provider.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

from app.core.config import get_settings

_TOKEN_RE = re.compile(r"[a-z0-9]+")


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


def build_index() -> VectorIndex:
    # pgvector index is created per-query in full mode; local mode uses brute force.
    return BruteForceIndex()
