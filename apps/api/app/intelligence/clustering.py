"""Deterministic duplicate/theme clustering.

Groups related candidates without embeddings or randomness so the same set of
candidates always clusters identically. The cluster key is chosen from the
strongest available theme signal — pain-point DNA, then signal type, then a
neutral ``"general"`` bucket — which keeps clusters human-labelable and stable
across runs.
"""

from __future__ import annotations

import hashlib

from app.intelligence.models import ExtractedIntelligence, OpportunityCandidate


def cluster_key(intelligence: ExtractedIntelligence) -> str:
    """Stable theme key for a signal's inference. Never random."""
    if intelligence.pain_point_dna is not None:
        return intelligence.pain_point_dna.value
    if intelligence.signal_type is not None:
        return intelligence.signal_type.value
    return "general"


def content_fingerprint(text: str) -> str:
    """Deterministic content hash used as a duplicate tiebreak."""
    return hashlib.sha256(text.strip().lower().encode()).hexdigest()[:16]


def cluster_candidates(
    candidates: list[OpportunityCandidate],
) -> dict[str, list[OpportunityCandidate]]:
    """Group accepted candidates by their (already-assigned) cluster key.

    Insertion order within a cluster is preserved, and keys are returned sorted so
    iteration order is deterministic for callers and tests.
    """
    groups: dict[str, list[OpportunityCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.cluster_key, []).append(candidate)
    return {key: groups[key] for key in sorted(groups)}
