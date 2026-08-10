"""INDEPENDENT graph-hash oracle (Gate 4N-I17, Defect 1, Phase H).

WHY THIS FILE EXISTS, AND WHY IT LIVES OUTSIDE scripts/.

Gate 4N-I16 shipped `scripts/lifecycle_canonical.py` with a docstring claiming it "implements
the canonicalization AGAIN ... rather than by calling the production code". It did not. The
production hash was:

    def graph_hash():
        return sha256(lifecycle_canonical.canonical_bytes(steps()))

and the "independent" reference was:

    def expected_hash(steps):
        return sha256(canonical_bytes(steps))

Both sides were `sha256(canonical_bytes(x))` — ONE implementation invoked twice. The comparison
test could not fail for any canonicalization whatsoever. Replacing `canonical_bytes` with a
constant that discarded the graph entirely left it green. The guard written to protect the
independence checked that lifecycle_canonical does not import role_bootstrap_lifecycle; the real
coupling ran the other way and was unchecked.

THE RULE THIS FILE OBEYS. It imports NOTHING from the production canonicalization path:
  * not `lifecycle_canonical` (the production canonicalizer),
  * not `graph_hash` (the production hash),
  * not the production `SEMANTIC_FIELDS` / `NON_SEMANTIC_FIELDS` / `SORTED_LIST_FIELDS`.

It declares its OWN schema below, hand-written from the design contract rather than copied from
the production constant, and implements ordering, field selection and encoding independently. A
test asserting `production == oracle` therefore compares two implementations, and a defect in
either one makes them disagree.

WHAT IT MAY SHARE. The raw graph DATA — the list of step dicts — is the object under test, and
both sides must obviously read the same object or they would be hashing different things. Sharing
the subject is not sharing the method. It may also use stdlib json/hashlib: those are BENIGN
common ancestors, independently tested by their maintainers and not decisive for this assertion.

TWO IMPLEMENTATIONS OF ONE SPEC — AND THEY MUST AGREE. This oracle targets the SAME canonical
form as production, because Phase H requires the production hash to be compared against an
independently calculated one, and two values can only be compared if they are meant to be equal.
The point is not that the encodings differ; it is that the CODE differs. A defect in either
implementation makes the two disagree, which is the signal. A first draft of this file
deliberately diverged the encoding to avoid "accidental convergence" — that was a design error:
it made the required comparison impossible, leaving only the fixture as an anchor.

THE CANONICAL SPEC, restated here from the design contract so this file does not depend on
production's constants:
  * only semantic fields participate; commentary fields are excluded by name;
  * steps are ordered by their explicit numeric `sequence`;
  * `depends_on` is sorted, because "depends on A and B" is the same graph as "B and A";
  * every other list keeps declared order;
  * object keys are emitted in sorted order;
  * JSON, compact separators, UTF-8, ensure_ascii.

Independence is in the implementation: this file sorts keys itself rather than delegating to
`json.dumps(sort_keys=True)`, validates the schema before hashing (production does not), and
derives its field list from its own declaration above.
"""

from __future__ import annotations

import hashlib
import json

# ---------------------------------------------------------------------------------------
# SCHEMA — authored here, from the design contract. NOT imported from production.
#
# Ordering within this tuple is itself part of the oracle's canonical form, which is a second
# deliberate divergence: production sorts keys alphabetically, this oracle emits a fixed
# declared order. Two different routes to the same semantic content.
# ---------------------------------------------------------------------------------------

ORACLE_SEMANTIC_FIELDS = (
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

# Commentary. Editing prose must not change graph identity.
ORACLE_COMMENTARY_FIELDS = ("note", "actor_class")

# Fields whose list order carries no meaning.
ORACLE_UNORDERED_LIST_FIELDS = ("depends_on",)


class OracleSchemaError(Exception):
    """The graph does not satisfy the oracle's independently declared schema."""


def validate_schema(steps: list) -> None:
    """Validate INDEPENDENTLY. The oracle does not trust the production validator.

    A graph that fails here is rejected before hashing, so a malformed graph cannot silently
    produce a stable digest.
    """
    if not isinstance(steps, list) or not steps:
        raise OracleSchemaError("graph must be a non-empty list of steps")

    seen_ids, seen_sequences = set(), set()
    for step in steps:
        if not isinstance(step, dict):
            raise OracleSchemaError("each step must be a mapping")
        for field in ORACLE_SEMANTIC_FIELDS:
            if field not in step:
                raise OracleSchemaError(f"step is missing required semantic field {field!r}")

        sid = step["step_id"]
        if not isinstance(sid, str) or not sid:
            raise OracleSchemaError(f"step_id must be a non-empty string, got {sid!r}")
        if sid in seen_ids:
            raise OracleSchemaError(f"duplicate step_id {sid!r}")
        seen_ids.add(sid)

        seq = step["sequence"]
        if isinstance(seq, bool) or not isinstance(seq, (int, float)):
            raise OracleSchemaError(f"{sid}: sequence must be a number, got {seq!r}")
        if seq in seen_sequences:
            raise OracleSchemaError(f"{sid}: duplicate sequence {seq!r}")
        seen_sequences.add(seq)

        deps = step["depends_on"]
        if not isinstance(deps, (list, tuple)):
            raise OracleSchemaError(f"{sid}: depends_on must be a list")
        for dep in deps:
            if not isinstance(dep, str):
                raise OracleSchemaError(f"{sid}: dependency {dep!r} is not a string")

    for step in steps:
        for dep in step["depends_on"]:
            if dep not in seen_ids:
                raise OracleSchemaError(
                    f"{step['step_id']}: dependency {dep!r} does not name an existing step")


def _scalar(value):
    """Normalise one field value. Tuples become lists; nothing else is coerced."""
    if isinstance(value, tuple):
        return list(value)
    return value


def oracle_records(steps: list) -> list:
    """Semantic records, ordered by the explicit sequence key, keys emitted in sorted order.

    Key ordering is done HERE rather than by delegating to `json.dumps(sort_keys=True)`. That is
    the independence: the same canonical form, reached by different code.
    """
    validate_schema(steps)
    ordered = sorted(steps, key=lambda s: s["sequence"])
    records = []
    for step in ordered:
        record = {}
        for field in sorted(ORACLE_SEMANTIC_FIELDS):        # sorted by THIS module
            value = _scalar(step.get(field))
            if field in ORACLE_UNORDERED_LIST_FIELDS and isinstance(value, list):
                value = sorted(value)
            record[field] = value
        records.append(record)
    return records


def oracle_bytes(steps: list) -> bytes:
    """Canonical bytes. sort_keys is deliberately FALSE — the keys were already ordered above,
    by this module, so the ordering is this module's behaviour and not the library's."""
    return json.dumps(oracle_records(steps), separators=(",", ":"),
                      ensure_ascii=True, sort_keys=False).encode("utf-8")


def oracle_hash(steps: list) -> str:
    """The oracle's digest. Compared against production; never used to produce it."""
    return hashlib.sha256(oracle_bytes(steps)).hexdigest()


def semantic_projection(steps: list) -> list:
    """A comparable semantic view, so a test can report WHICH field diverged rather than only
    that two digests differ."""
    return oracle_records(steps)
