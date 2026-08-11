#!/usr/bin/env python3
"""Authoritative Docker assurance state, governed cache, and poisoning resistance (Gate 4N-I28BF-B1).

WHY THIS EXISTS. Through Gate 4N-I28BF-A4 the Docker per-site enforcement was proven end to end, but
`docker_boundary.per_site_state()` recomputed on every call and bound only category, policy, and
source-position identities — not the authorization pair, nor the production/independent universe
digests, nor a single canonical aggregate identity. I28BE-ASSURANCE-PART-B requires ONE authoritative
Docker assurance state and ONE dedicated, governed cache for it, so a warm session cannot serve a
stale or cross-tree answer and a cold session still derives the truth.

THREE SEPARATED LAYERS (Gate 4N-I28BF-B1 section 6), never merged into one opaque function:

  A. Fresh derivation      — `fresh_state()` loads the policy, derives the production and independent
                             Docker universes, reconciles them, derives every per-site record and the
                             aggregate, and stamps the deferred workflow marker. It consults NO cache.
  B. Authoritative state   — `authoritative_state()` / `validate_state()` / `state_digest()` give the
                             versioned, canonically ordered, digest-bound, deep-frozen state, and fail
                             closed on missing, unknown, malformed, or stale fields.
  C. Governed cache        — `_STATE_CACHE`, classified AUTHORITATIVE_CONTENT_BOUND_CACHE in
                             `cache-authority-policy.json`, keyed by the COMPLETE identity set and
                             validated on both key and value before any reuse. It may accelerate a
                             repeated identical derivation; it may never BE the answer at session
                             finish, and correctness never depends on a cache hit.

The cache stores only validated state, deep-frozen so a later mutation of the returned object cannot
reach the stored one; `reset_caches()` empties it; and `cache_authority` suspends it during every
fresh authoritative derivation, so a poisoned entry can never bless a fresh answer.
"""

from __future__ import annotations

import datetime as _dt
import os

from pathlib import Path

import cache_authority as _ca
import docker_boundary as _db
import expiry_authorization as _ea
import shell_positions as _shp

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSERTION_REGISTRY = REPO_ROOT / "tests" / "fixtures" / "assertion-contract-registry.json"

STATE_SCHEMA_VERSION = "i28bf-b1.1"
CACHE_SCHEMA_VERSION = "i28bf-b1.1"
POLICY_SCHEMA_VERSION = "docker-boundary-policy.v1"
PARSER_SCHEMA_VERSION = "i28bf-b1.1"
PROVENANCE_SCHEMA_VERSION = "i28bf-b1.1"
# The EXACT production marker docker_boundary emits. B1 closes cache/state only; workflow coverage
# stays deferred to Gate 4N-I28BG and this string must never become PASS/COMPLETE.
WORKFLOW_COVERAGE_MARKER = "NOT_ADJUDICATED — deferred to Gate 4N-I28BG (ADV-I28AX-ARCH-01 part B)"
_WORKFLOW_DEFERRED_GATE = "I28BG"

# The complete key identity set every governed cache entry binds. Authored here so a missing or
# unknown key component is a schema error, never a silent partial-key fallback.
CACHE_KEY_FIELDS = (
    "staged_tree", "source_content_token", "policy_digest", "category_table_digest",
    "normalization_version", "parser_schema_version", "parser_completion",
    "source_position_version", "production_universe_digest", "independent_universe_digest",
    "state_schema_version", "assertion_contract_digest", "authorization_pair_digest",
)

_STATE_TOP_FIELDS = (
    "schema_version", "authorization", "repository", "policy", "parser", "universe",
    "per_site", "aggregate",
)

_CACHE_VALUE_FIELDS = (
    "state", "state_digest", "cache_key_digest", "provenance", "validation_status",
    "cache_schema_version",
)

_PROVENANCE_FIELDS = (
    "creation_utc", "owning_callable", "process_identity", "staged_tree", "origin",
    "provenance_schema_version",
)

_VALIDATION_OK = "VALIDATED"


class DockerAssuranceError(RuntimeError):
    """Fail closed. An unvalidatable state or an untrustworthy cache entry never decides a session."""


# The one governed cache. AUTHORITATIVE_CONTENT_BOUND_CACHE: it holds validated answers keyed by the
# complete content identity, and cache_authority suspends it during every fresh derivation.
_STATE_CACHE: dict = {}


def reset_caches() -> None:
    """Empty the governed Docker assurance cache. A test that changes the tree must call this."""
    _STATE_CACHE.clear()


# ============================================================ LAYER A — fresh derivation
def _authorization_identity() -> dict:
    # Bind the pair IDENTITY from the reviewed constants directly. active_pair() would additionally
    # run the full IAM date validation (a lazy iam_eval import); binding identity does not need it,
    # and avoiding it keeps this module's dependency footprint minimal. The window's AUTHORISATION
    # is enforced by expiry_authorization's own guard and by the graded session, not here.
    iss, exp = _ea.ACTIVE_ISSUANCE_UTC, _ea.ACTIVE_EXPIRY_UTC
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    dur = int((_dt.datetime.strptime(exp, fmt).replace(tzinfo=_dt.timezone.utc)
               - _dt.datetime.strptime(iss, fmt).replace(tzinfo=_dt.timezone.utc)).total_seconds())
    ident = {"issuance": iss, "expiry": exp, "duration_seconds": dur}
    ident["pair_digest"] = _ca.digest(ident)
    return ident


def _repository_identity() -> dict:
    src = _ca.source_identity()                       # {workflow_digests, staged_tree}
    source_token = _ca.digest(src["workflow_digests"])
    tree = src["staged_tree"]
    if not tree or tree == "<unavailable>":
        # A non-git materialisation (e.g. a synthetic sandbox) still needs a NON-EMPTY, deterministic
        # tree identity, or the cache key is incomplete. Fall back to a content-derived token so the
        # identity is never empty; git-bearing trees (the real repo, git-archive clones) use the real
        # write-tree hash and this branch is not taken.
        tree = "content:" + source_token
    return {"staged_tree": tree,
            "workflow_digests": src["workflow_digests"],
            "source_content_token": source_token}


def _policy_identity() -> dict:
    doc = _db.load_policy()
    policy_digest = _db._digest_bytes(_db.POLICY.read_bytes()) if _db.POLICY.is_file() else ""
    return {"policy_schema_version": POLICY_SCHEMA_VERSION,
            "policy_digest": policy_digest,
            "category_table_version": _db.CATEGORY_TABLE_VERSION,
            "category_table_digest": _db.category_table_digest(),
            "normalization_version": _db.NORMALIZATION_VERSION,
            "model": doc.get("model")}


def _parser_identity() -> dict:
    cd = _shp.completeness_digest()
    return {"parser_schema_version": PARSER_SCHEMA_VERSION,
            "source_position_version": _db.SOURCE_POSITION_VERSION,
            "parser_completion": cd["digest"],
            "parser_untrustworthy": tuple(cd["untrustworthy"]),
            "grammar_version": cd["grammar_version"]}


def _assertion_contract_digest() -> str:
    return _db._digest_bytes(ASSERTION_REGISTRY.read_bytes()) if ASSERTION_REGISTRY.is_file() else ""


def _universe_identity(ps: dict) -> dict:
    doc = _db.load_policy()
    production_ids = sorted(s.get("id") for s in (doc.get("call_sites") or []) if isinstance(s, dict))
    try:
        derived = _db.derive_call_sites()
        independent_ids = sorted(s.get("id") for s in (derived.get("sites") or []))
    except Exception as exc:                          # fail closed; never silently empty
        raise DockerAssuranceError(
            f"the independent Docker universe could not be derived ({type(exc).__name__}); an "
            "underivable universe is refused, never treated as empty agreement") from exc
    reconciliation = "AGREE" if set(production_ids) == set(independent_ids) else "DISAGREE"
    expected_positive = bool(production_ids) and bool(independent_ids) and ps["load_bearing"] > 0
    return {"production_universe_digest": _ca.digest(production_ids),
            "independent_universe_digest": _ca.digest(independent_ids),
            "reconciliation": reconciliation,
            "expected_positive": expected_positive,
            "site_ids": tuple(production_ids),
            "site_count": len(production_ids),
            "load_bearing_count": ps["load_bearing"],
            "class_counts": ps["classification_counts"]}


def fresh_state() -> dict:
    """Layer A + B assembly, consulting NO cache. The authoritative Docker assurance state."""
    ps = _db.per_site_state()                         # docker_boundary does not cache this
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "authorization": _authorization_identity(),
        "repository": _repository_identity(),
        "policy": _policy_identity(),
        "parser": _parser_identity(),
        "universe": _universe_identity(ps),
        "per_site": tuple(ps["per_site"]),
        "aggregate": {
            "decision_counts": ps["decision_counts"],
            "classification_counts": ps["classification_counts"],
            "docker_aggregate": ps["clean"],
            "docker_per_site_layer": ps["clean"],
            "workflow_coverage": ps["workflow_assurance_coverage"],
        },
    }
    return state


# ============================================================ LAYER B — authoritative state
def _normalize(state):
    """Impose a canonical ORDER on the sequences that are semantically sets, so two states that
    differ only in incidental ordering canonicalise — and therefore digest — identically."""
    s = _thaw(state)
    if isinstance(s, dict):
        per = s.get("per_site")
        if isinstance(per, list):
            s["per_site"] = sorted(per, key=lambda r: r.get("id") or "")
        uni = s.get("universe")
        if isinstance(uni, dict) and isinstance(uni.get("site_ids"), (list, tuple)):
            uni["site_ids"] = sorted(uni["site_ids"])
    return s


def canonical_state(state: dict):
    """A deterministic, order-free canonical form; semantically equal states canonicalise equal."""
    return _ca.canonical(_normalize(state))


def state_digest(state: dict) -> str:
    """The digest of the state's canonical form. Deterministic and independently reproducible."""
    return _ca.digest(_normalize(state))


def validate_state(state: object) -> list:
    """Every reason a state is not a trustworthy authoritative Docker state. Empty means valid."""
    problems: list = []
    if not isinstance(state, dict):
        return [f"state is {type(state).__name__}, not a mapping"]
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        problems.append(f"stale or missing state schema version {state.get('schema_version')!r}; "
                        f"expected {STATE_SCHEMA_VERSION!r}")
        return problems                               # every field meaning depends on the version
    unknown = sorted(set(state) - set(_STATE_TOP_FIELDS))
    if unknown:
        problems.append(f"unknown top-level field(s) {unknown}")
    missing = sorted(set(_STATE_TOP_FIELDS) - set(state))
    if missing:
        problems.append(f"missing required top-level field(s) {missing}")
    if problems:
        return problems
    # authorization
    auth = state["authorization"]
    if set(auth) != {"issuance", "expiry", "duration_seconds", "pair_digest"}:
        problems.append("authorization identity has an unexpected field set")
    elif auth["pair_digest"] != _ca.digest({k: auth[k] for k in
                                            ("issuance", "expiry", "duration_seconds")}):
        problems.append("authorization pair_digest does not match its own fields")
    # policy
    pol = state["policy"]
    if pol.get("policy_schema_version") != POLICY_SCHEMA_VERSION:
        problems.append("stale policy schema version")
    for k in ("policy_digest", "category_table_digest", "normalization_version"):
        if not pol.get(k):
            problems.append(f"policy identity missing {k}")
    # parser
    par = state["parser"]
    if par.get("parser_schema_version") != PARSER_SCHEMA_VERSION:
        problems.append("stale parser schema version")
    if par.get("parser_untrustworthy"):
        problems.append(f"the parser reports untrustworthy sources {par['parser_untrustworthy']}")
    if par.get("source_position_version") != _db.SOURCE_POSITION_VERSION:
        problems.append("stale source-position schema version")
    # universe
    uni = state["universe"]
    if uni.get("reconciliation") != "AGREE":
        problems.append(f"the production and independent universes do not reconcile "
                        f"({uni.get('reconciliation')})")
    if not uni.get("expected_positive"):
        problems.append("expected-positive coverage is not met; an empty or non-load-bearing "
                        "universe cannot establish a Docker assurance baseline")
    if not uni.get("site_ids"):
        problems.append("the Docker universe is empty")
    if len(set(uni.get("site_ids", ()))) != len(uni.get("site_ids", ())):
        problems.append("duplicate site id in the universe")
    if uni.get("site_count") != len(uni.get("site_ids", ())):
        problems.append("universe site_count disagrees with the site id set")
    # per-site
    per = state["per_site"]
    if not per:
        problems.append("per-site state is empty")
    ids = [r.get("id") for r in per]
    if len(set(ids)) != len(ids):
        problems.append("duplicate per-site id")
    for r in per:
        if not r.get("position"):
            problems.append(f"per-site record {r.get('id')!r} has no source position")
    # GATE 4N-I28BF-B2, finding B2-FIND-01: reconcile the per-site records against the universe both
    # ways. Before this, a per-site DECISION for a site absent from both universes (falsification
    # arm 15) and a universe site with no per-site record went undetected. The per-site set and the
    # universe site-id set must be identical.
    uni_ids = set(uni.get("site_ids", ()))
    per_ids = set(ids)
    for ghost in sorted(per_ids - uni_ids):
        problems.append(f"per-site record {ghost!r} decides a site that is not in the universe")
    for undecided in sorted(uni_ids - per_ids):
        problems.append(f"universe site {undecided!r} has no per-site record")
    # aggregate
    agg = state["aggregate"]
    if agg.get("workflow_coverage") != WORKFLOW_COVERAGE_MARKER:
        problems.append("the workflow-coverage marker is missing, changed, or falsely completed; "
                        f"it must be exactly {WORKFLOW_COVERAGE_MARKER!r}")
    if agg.get("docker_aggregate") is not True:
        problems.append("the Docker aggregate is not clean")
    # A forced-clean aggregate cannot coexist with a non-PASS site: the aggregate is DERIVED from
    # the per-site decisions, so this cross-check refuses a value that forced one without the other.
    non_pass = [r.get("id") for r in per if r.get("decision") != _db.SITE_PASS]
    if agg.get("docker_aggregate") is True and non_pass:
        problems.append(f"docker_aggregate is clean while site(s) {non_pass} are not PASS; the "
                        "aggregate cannot be forced clean over a failing site")
    if agg.get("docker_per_site_layer") is not agg.get("docker_aggregate"):
        problems.append("the docker_per_site layer disagrees with the Docker aggregate")
    return problems


def authoritative_state() -> dict:
    """A freshly derived, validated authoritative state. Raises when it cannot be trusted."""
    state = fresh_state()
    problems = validate_state(state)
    if problems:
        raise DockerAssuranceError("the authoritative Docker state is invalid: " + "; ".join(problems))
    return _ca.deep_freeze(state)


# ============================================================ LAYER C — governed cache
def cache_key(state: dict) -> dict:
    """The COMPLETE identity key. A missing or unknown component is a schema error, not a fallback."""
    key = {
        "staged_tree": state["repository"]["staged_tree"],
        "source_content_token": state["repository"]["source_content_token"],
        "policy_digest": state["policy"]["policy_digest"],
        "category_table_digest": state["policy"]["category_table_digest"],
        "normalization_version": state["policy"]["normalization_version"],
        "parser_schema_version": state["parser"]["parser_schema_version"],
        "parser_completion": state["parser"]["parser_completion"],
        "source_position_version": state["parser"]["source_position_version"],
        "production_universe_digest": state["universe"]["production_universe_digest"],
        "independent_universe_digest": state["universe"]["independent_universe_digest"],
        "state_schema_version": state["schema_version"],
        "assertion_contract_digest": _assertion_contract_digest(),
        "authorization_pair_digest": state["authorization"]["pair_digest"],
    }
    return key


def validate_cache_key(key: object) -> list:
    problems = []
    if not isinstance(key, dict):
        return [f"cache key is {type(key).__name__}, not a mapping"]
    unknown = sorted(set(key) - set(CACHE_KEY_FIELDS))
    if unknown:
        problems.append(f"unknown cache-key component(s) {unknown}")
    missing = sorted(set(CACHE_KEY_FIELDS) - set(key))
    if missing:
        problems.append(f"missing cache-key component(s) {missing}")
    for f in CACHE_KEY_FIELDS:
        if f in key and (key[f] is None or key[f] == ""):
            problems.append(f"cache-key component {f!r} is empty")
    return problems


def cache_key_digest(state: dict) -> str:
    key = cache_key(state)
    problems = validate_cache_key(key)
    if problems:
        raise DockerAssuranceError("cannot digest an invalid cache key: " + "; ".join(problems))
    return _ca.digest(key)


def _provenance(origin: str, staged_tree: str) -> dict:
    return {
        "creation_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "owning_callable": "docker_assurance_state.store",
        "process_identity": str(os.getpid()),
        "staged_tree": staged_tree,
        "origin": origin,
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
    }


def store(state: dict, *, origin: str = "cold") -> str:
    """Validate, deep-freeze, and store one authoritative state. Returns its cache-key digest."""
    problems = validate_state(state)
    if problems:
        raise DockerAssuranceError("refusing to cache an invalid state: " + "; ".join(problems))
    kd = cache_key_digest(state)
    value = {
        "state": _ca.deep_freeze(state),
        "state_digest": state_digest(state),
        "cache_key_digest": kd,
        "provenance": _provenance(origin, state["repository"]["staged_tree"]),
        "validation_status": _VALIDATION_OK,
        "cache_schema_version": CACHE_SCHEMA_VERSION,
    }
    _STATE_CACHE[kd] = _ca.deep_freeze(value)
    return kd


def validate_cache_value(value: object, *, expected_key_digest: str | None = None) -> list:
    """Every reason a cached value is untrustworthy. A valid key never excuses an invalid value."""
    problems = []
    if not isinstance(value, dict) and not _is_frozen_mapping(value):
        return [f"cache value is {type(value).__name__}, not a mapping"]
    keys = set(value.keys())
    unknown = sorted(keys - set(_CACHE_VALUE_FIELDS))
    if unknown:
        problems.append(f"unknown cache-value field(s) {unknown}")
    missing = sorted(set(_CACHE_VALUE_FIELDS) - keys)
    if missing:
        problems.append(f"missing cache-value field(s) {missing}")
    if problems:
        return problems
    if value["cache_schema_version"] != CACHE_SCHEMA_VERSION:
        problems.append("stale cache schema version")
    if value["validation_status"] != _VALIDATION_OK:
        problems.append(f"cache value validation status is {value['validation_status']!r}")
    state = value["state"]
    state_problems = validate_state(_thaw(state))
    if state_problems:
        problems.append("cached state is invalid: " + state_problems[0])
        return problems
    if value["state_digest"] != state_digest(_thaw(state)):
        problems.append("cached state_digest does not match the cached state")
    if value["cache_key_digest"] != cache_key_digest(_thaw(state)):
        problems.append("cached cache_key_digest does not match the cached state's key")
    if expected_key_digest is not None and value["cache_key_digest"] != expected_key_digest:
        problems.append("cached entry is filed under a key that is not its own state's key")
    prov = value["provenance"]
    prov_keys = set(prov.keys()) if hasattr(prov, "keys") else set()
    if prov_keys != set(_PROVENANCE_FIELDS):
        problems.append("cache provenance is missing, malformed, or a stale schema")
    return problems


def lookup(state: dict) -> tuple:
    """Return (validated frozen state, provenance-tag). Never substitutes for a fresh answer.

    The tag is one of HIT / MISS / REJECTED:<reason>, so a caller can distinguish a validated hit
    from a refused one and from a cold miss without inspecting the cache internals.
    """
    kd = cache_key_digest(state)
    value = _STATE_CACHE.get(kd)
    if value is None:
        return None, "MISS"
    problems = validate_cache_value(value, expected_key_digest=kd)
    if problems:
        return None, "REJECTED:" + problems[0]
    return value["state"], "HIT"


# ============================================================ session integration (real path)
def establish_state() -> dict:
    """Session establishment: derive fresh, validate, populate the cache cold, bind provenance.

    Called from signalnest_bootstrap.establish(). Independent positive-presence and completeness are
    enforced by validate_state(); the cache is a cold population here, never the source of truth.
    """
    state = fresh_state()
    problems = validate_state(state)
    if problems:
        raise DockerAssuranceError("Docker assurance baseline refused: " + "; ".join(problems))
    reset_caches()
    kd = store(state, origin="cold")
    # The CACHE holds the deep-frozen object (immutable); the attestation carries a PLAIN, safely
    # copied snapshot so the session baseline stays picklable/deep-copyable for downstream consumers.
    # Mutating this copy cannot reach the frozen cached object.
    return {"state": _thaw(state), "state_digest": state_digest(state),
            "cache_key_digest": kd, "provenance": _thaw(_STATE_CACHE[kd])["provenance"],
            "schema_version": STATE_SCHEMA_VERSION}


def compare_states(before: dict, after: dict) -> list:
    """Field-by-field comparison of the bound baseline against a freshly derived final state."""
    problems = []
    b, a = _thaw(before), _thaw(after)
    if state_digest(b) != state_digest(a):
        # Name exactly which identity moved rather than only reporting a digest change.
        for section in _STATE_TOP_FIELDS:
            if _ca.canonical(b.get(section)) != _ca.canonical(a.get(section)):
                problems.append(f"docker_assurance: {section} changed between establishment and "
                                f"session finish")
    return problems


def reverify_state(baseline: object) -> dict:
    """Session finish: FRESHLY derive, validate, and compare to the baseline. No cache substitution.

    Called from signalnest_bootstrap.reverify(). This never consults _STATE_CACHE for the answer; a
    warm cache accelerates establishment, but the finish derivation is always fresh, so a poisoned
    cache cannot mask a late mutation.
    """
    problems = []
    try:
        fresh = fresh_state()
    except Exception as exc:                          # a crash is not a verdict; fail closed
        return {"clean": False, "problems": [
            f"docker_assurance: fresh session-finish derivation raised {type(exc).__name__}: {exc}; "
            "a cache may not stand in for a derivation that cannot run"]}
    problems.extend(f"docker_assurance: {p}" for p in validate_state(fresh))
    bound = baseline.get("state") if isinstance(baseline, dict) else None
    if bound is None:
        problems.append("docker_assurance: the baseline bound no authoritative state to compare")
    else:
        problems.extend(compare_states(bound, fresh))
    return {"clean": not problems, "problems": problems}


# ============================================================ helpers
def _is_frozen_mapping(value) -> bool:
    from types import MappingProxyType
    return isinstance(value, MappingProxyType)


def _thaw(value):
    """A plain, comparable copy of a possibly deep-frozen structure (never mutates the original)."""
    from types import MappingProxyType
    if isinstance(value, (dict, MappingProxyType)):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(_thaw(v) for v in value)
    return value


def main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Authoritative Docker assurance state.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        state = authoritative_state()
    except DockerAssuranceError as exc:
        print(f"  {exc}")
        print("DOCKER ASSURANCE STATE: refused")
        return 2
    if args.json:
        print(json.dumps(_thaw(state), indent=1, sort_keys=True, default=str))
    else:
        print(f"  sites {state['universe']['site_count']} | load-bearing "
              f"{state['universe']['load_bearing_count']} | digest {state_digest(state)[:16]}")
        print("DOCKER ASSURANCE STATE: authoritative")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
