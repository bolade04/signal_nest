#!/usr/bin/env python3
"""INDEPENDENT canonicalization of the lifecycle graph (Gate 4N-I16, Defect 7, Phase U).

THE DEFECT. `tests/test_role_bootstrap_lifecycle.py::test_the_graph_hash_is_stable` read:

    assert lc.graph_hash() == lc.graph_hash()
    assert len(lc.graph_hash()) == 64

It compared the production hash function to ITSELF and checked a string length. The Gate
4N-I15 validator lane proved the consequence by replacing the function body with
`return "0" * 64` — ignoring the graph entirely — and the test stayed green. `graph_hash`
appeared in exactly five places repo-wide and nothing anywhere bound its value to an
independent reference.

WHAT THIS MODULE ACTUALLY IS — corrected at Gate 4N-I21 (ADV-F).

This module is THE PRODUCTION CANONICALISATION. `role_bootstrap_lifecycle.graph_hash()` is
`sha256(canonical_bytes(steps()))`, so anything here is on the production path.

It previously claimed the opposite: "this module implements the canonicalization AGAIN, from
the declared contract rather than by calling the production code, and never imports the
production hash. The test compares the two." That was false, and it was load-bearing — a test
comparing `graph_hash()` against `expected_hash()` compares one implementation with itself,
which is exactly the Gate 4N-I16 Defect 1 that the claim was supposed to rule out. Gate
4N-I15's validator lane had already proved the consequence by replacing the body with
`return "0" * 64` and watching the suite stay green.

THE INDEPENDENT CHECK LIVES ELSEWHERE: `tests/oracle/graph_oracle.py`, which is stdlib-only,
declares its own schema and ordering, and imports nothing from this module. That is the
comparison that means something. This module states the canonical CONTRACT below so the oracle
has something to be independent OF — but it is not, and must not be described as, an
independent reference.

THE CANONICAL CONTRACT — stated here because "canonical" is meaningless unstated:
  * only SEMANTIC fields participate; commentary fields are excluded by name;
  * steps are ordered by their explicit `sequence` key, which the DAG validator has already
    proven unique and integral — never by dict insertion order;
  * dependency lists are SORTED, because "depends on A and B" is the same graph as
    "depends on B and A";
  * every other list keeps its declared order, because it is semantically ordered;
  * JSON with sorted keys, no whitespace, UTF-8, ensure_ascii=True.
"""

from __future__ import annotations

import hashlib
import json

# Fields that carry MEANING. A change to any of these is a change to the graph.
SEMANTIC_FIELDS = (
    "step_id",
    "sequence",
    "owner",
    "action",
    "resource",
    "depends_on",
    "read_back",
    "evidence",
    "timeout_seconds",
    "rollback_owner",
    "is_mutation",
    "retires_principal_after",
    "requires_assignment",
    "expiry_dependent",
)

# Fields that are commentary. Editing prose must NOT change the graph identity, or every
# comment fix looks like a design change and reviewers stop reading the diff.
NON_SEMANTIC_FIELDS = ("note", "actor_class")

# Dependency order is not meaningful; step order is.
SORTED_LIST_FIELDS = ("depends_on",)


def canonical_steps(steps: list[dict]) -> list[dict]:
    ordered = sorted(steps, key=lambda s: s["sequence"])
    out = []
    for step in ordered:
        row = {}
        for field in SEMANTIC_FIELDS:
            value = step.get(field)
            if field in SORTED_LIST_FIELDS and isinstance(value, (list, tuple)):
                value = sorted(value)
            elif isinstance(value, tuple):
                value = list(value)
            row[field] = value
        out.append(row)
    return out


def canonical_bytes(steps: list[dict]) -> bytes:
    return json.dumps(canonical_steps(steps), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def expected_hash(steps: list[dict]) -> str:
    """The expected graph hash, computed WITHOUT calling the production implementation."""
    return hashlib.sha256(canonical_bytes(steps)).hexdigest()
