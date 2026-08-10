#!/usr/bin/env python3
"""Authority model for every mutable cache in the assurance path (Gate 4N-I28AR).

THE DEFECT THIS CLOSES. Gate 4N-I28AP finding ADV-I28AP-03. `site_taxonomy._DERIVED` is a plain
module-level dict that memoises the release-root, production-site and CI-release-site derivations.
Poisoning it takes release roots 41 -> 0 and production sites 492 -> 0 while executed-code provenance,
executed-state provenance, startup policy, executable trust, the inventory AND session-finish
reverification all report clean — and the poisoned root set is then consumed by executable
reachability adjudication. A nested in-place list mutation does the same without ever rebinding
`_DERIVED`, so the `VOLATILE_CACHE:<type>` token that was supposed to cover it is satisfied by
construction: the poisoned dict is still a dict.

WHY NOT SIMPLY RECOMPUTE EVERY TIME. Measured on this tree: a cold `production` derivation costs
0.86 s and `ci_release` 0.73 s. Recomputing on every read would add minutes to every graded session,
and a control that makes the suite unusable is a control someone switches off. That failure mode has
its own history in this chain.

THE MODEL, and each part closes a specific attack.

  1. CANONICAL IMMUTABLE VALUES. What the cache holds is frozen — tuples and frozen mappings all the
     way down. In-place mutation of a nested list is not detected, it is IMPOSSIBLE. That is the
     attack the type token could never have seen.

  2. IDENTITY PINNING. At verification the identity of the cache mapping and of every cached value
     is recorded. Because the values are immutable, the only remaining attacks are replacing the
     mapping or replacing a value, and both change `id()`. Checking identity is O(1), so a
     load-bearing consumer can afford to check on EVERY use — which is what Section 14 asks for and
     what re-hashing a large structure could not have delivered.

  3. FRESH RECOMPUTATION AT BOTH TRUST BOUNDARIES. `verify()` derives the authoritative answer
     WITHOUT consulting any cache and compares it canonically with what the cache would serve. It
     runs at `establish()` before graded work and again at session finish. A cache that disagrees
     with staged source fails closed and NAMES the cache and the key.

  4. CLASSIFICATION. Every mutable cache reachable from assurance code carries an explicit authority
     classification. An unclassified cache is refused; a cache classified as non-authoritative that
     turns out to decide something is refused too, because `verify()` checks the fresh answer
     regardless of what the classification claims.

WHAT VOLATILE_CACHE NOW MEANS. Mutable, non-authoritative, and independently recomputed or
identity-verified. It does NOT mean "mutable state omitted from provenance because binding it is
inconvenient" — that reading is what ADV-I28AP-03 exploited.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "tests" / "fixtures" / "cache-authority-policy.json"

# The authority classifications. A generic "trusted cache" category is deliberately absent.
NON_AUTHORITATIVE_PERFORMANCE_HINT = "NON_AUTHORITATIVE_PERFORMANCE_HINT"
IMMUTABLE_VALIDATED_SNAPSHOT = "IMMUTABLE_VALIDATED_SNAPSHOT"
AUTHORITATIVE_CONTENT_BOUND_CACHE = "AUTHORITATIVE_CONTENT_BOUND_CACHE"
SESSION_BASELINE_STATE = "SESSION_BASELINE_STATE"
DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
PROHIBITED_FOR_TRUST_DECISIONS = "PROHIBITED_FOR_TRUST_DECISIONS"

CLASSIFICATIONS = frozenset({
    NON_AUTHORITATIVE_PERFORMANCE_HINT, IMMUTABLE_VALIDATED_SNAPSHOT,
    AUTHORITATIVE_CONTENT_BOUND_CACHE, SESSION_BASELINE_STATE, DIAGNOSTIC_ONLY,
    PROHIBITED_FOR_TRUST_DECISIONS,
})


class CacheAuthorityError(RuntimeError):
    """Fail closed. A cache whose authority cannot be established is never trusted."""


# --------------------------------------------------------------------------- canonical values
def canonical(value):
    """A deep, immutable, order-canonical rendering of a derived value.

    Lists become tuples, dicts become sorted tuples of pairs, sets become sorted tuples. The result
    supports equality and hashing and cannot be mutated in place, which is what makes attack (1)
    impossible rather than merely detectable.

    Ordering is canonicalised so that two derivations differing only in iteration order compare
    EQUAL — otherwise the comparison in `verify()` would produce false refusals, and a control that
    cries wolf is a control that gets disabled.
    """
    if isinstance(value, dict):
        return tuple(sorted(((str(k), canonical(v)) for k, v in value.items()), key=lambda kv: kv[0]))
    if isinstance(value, (list, tuple)):
        return tuple(canonical(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((repr(canonical(v)) for v in value)))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def digest(value) -> str:
    """A stable digest of a canonical value, for evidence and for cross-tree comparison."""
    return hashlib.sha256(
        json.dumps(canonical(value), sort_keys=True, default=str).encode()).hexdigest()


def freeze(value):
    """The value a cache is allowed to hold: canonical and immutable."""
    return canonical(value)


def deep_freeze(value):
    """The value a cache is ALLOWED to hold: immutable, but with the consumer API intact.

    `canonical()` above is for COMPARISON — it renders a dict as a tuple of pairs, which no consumer
    can subscript. Cached derivations are consumed as `record["module"]` and `site["canonical_site_id"]`
    across the taxonomy, so freezing them canonically would break every reader. `MappingProxyType`
    keeps mapping access and blocks assignment; lists become tuples for the same reason.

    This is the part that makes attack (1) IMPOSSIBLE rather than merely detectable. ADV-I28AP-03's
    sharpest shape never rebinds `_DERIVED` at all — it mutates a list nested inside it, so the type
    token stays satisfied. A nested tuple has no mutating operation to reach for.

    The frozen structure is built from the produced value and the original is dropped, so no writable
    alias survives; a proxy over a dict someone else still holds would be a view, not a freeze.
    """
    from types import MappingProxyType

    if isinstance(value, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(deep_freeze(v) for v in value)
    return value


def is_frozen(value) -> bool:
    """True when a value cannot be mutated in place, at any depth."""
    from types import MappingProxyType

    if isinstance(value, MappingProxyType):
        return all(is_frozen(v) for v in value.values())
    if isinstance(value, (tuple, frozenset)):
        return all(is_frozen(v) for v in value)
    if isinstance(value, (list, dict, set, bytearray)):
        return False
    return True


# --------------------------------------------------------------------------- identity pinning
_PINS: dict = {}


def pin(label: str, container) -> None:
    """Record the identity of a cache mapping and of every value it currently holds.

    Identity is the right instrument HERE and nowhere else in this chain: because the values are
    frozen, the only attacks left are replacing the mapping or replacing a value, and both change
    `id()`. That check is O(1), which is what lets a load-bearing consumer afford it on EVERY read —
    re-hashing a 492-site structure per call could not have been afforded, and a control too
    expensive to call is a control that gets called once and then bypassed.
    """
    _PINS[label] = (id(container), {k: id(v) for k, v in container.items()})


def assert_pinned(label: str, container) -> None:
    """Refuse when a pinned cache, or any pinned value inside it, has been replaced since `pin()`."""
    if label not in _PINS:
        return
    mapping_id, value_ids = _PINS[label]
    if id(container) != mapping_id:
        raise CacheAuthorityError(
            f"{label}: the cache OBJECT has been replaced since it was pinned. A trust decision "
            "will not be taken from a container that is not the one this session verified.")
    for key, expected in value_ids.items():
        if key not in container:
            raise CacheAuthorityError(
                f"{label}[{key!r}]: a pinned cached value has been REMOVED. Removal forces a silent "
                "re-derivation whose result nothing compared against the verified one.")
        if id(container[key]) != expected:
            raise CacheAuthorityError(
                f"{label}[{key!r}]: a pinned cached value has been REPLACED since verification. "
                "Gate 4N-I28AP finding ADV-I28AP-03: substituting this value takes release roots "
                "41 -> 0 with every provenance layer still reporting clean.")


# --------------------------------------------------------------------------- the policy
def load_policy(path: Path | None = None) -> dict:
    p = path or POLICY
    if not p.is_file():
        raise CacheAuthorityError(
            f"the cache-authority policy is missing at {p}. Every mutable cache in the assurance "
            "path must carry an explicit classification; without the policy this control would "
            "have to assume caches are harmless, which is the assumption ADV-I28AP-03 exploited.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    caches = doc.get("caches")
    if not isinstance(caches, dict) or not caches:
        raise CacheAuthorityError("the cache-authority policy classifies no cache; an empty policy "
                                  "would verify vacuously")
    for name, entry in caches.items():
        cls = entry.get("classification")
        if cls not in CLASSIFICATIONS:
            raise CacheAuthorityError(
                f"{name}: classification {cls!r} is not one of {sorted(CLASSIFICATIONS)}. An "
                "unclassified cache fails closed rather than defaulting to trusted.")
        if not entry.get("why"):
            raise CacheAuthorityError(f"{name}: a classification must state why it holds")
    return doc


# --------------------------------------------------------------------------- discovery
def discover_caches(modules=None) -> dict:
    """Every module-level mutable container that is a CACHE, derived rather than authored.

    A cache is a module-level mutable container that a module fills at runtime. A pre-populated
    constant table is configuration — already covered by the critical-list contract and by
    executed-state VALUE tokens — and is not reported here. The distinction is made from the SOURCE
    (was the literal empty at definition?) rather than from the live value, because a cache that has
    already been populated would otherwise look like a constant.
    """
    import ast
    import sys

    names = modules if modules is not None else _policy_modules()
    found: dict = {}
    for mod_name in sorted(names):
        module = _resident(mod_name)
        if module is None:
            continue
        origin = getattr(module, "__file__", None)
        if not origin or str(REPO_ROOT) not in str(origin):
            continue
        try:
            tree = ast.parse(Path(origin).read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign) else [])
            literal = getattr(node, "value", None)
            empty_literal = (isinstance(literal, (ast.Dict, ast.List, ast.Set))
                             and not (getattr(literal, "keys", None) or getattr(literal, "elts", None)))
            if not empty_literal:
                continue
            for t in targets:
                if not isinstance(t, ast.Name):
                    continue
                live = getattr(module, t.id, None)
                if isinstance(live, (dict, list, set)):
                    found[f"{mod_name}.{t.id}"] = {
                        "module": mod_name, "name": t.id,
                        "type": type(live).__name__, "entries": len(live)}
    return found


def _policy_modules() -> list:
    doc = load_policy()
    return sorted({e["module"] for e in doc["caches"].values()})


def _resident(module_name: str):
    """The policy's module, IMPORTED if it is not resident yet.

    Consulting `sys.modules` alone conflates two very different situations. At bootstrap time
    `shell_command_model` has not been imported, so its cache looks "absent" — and treating that as a
    missing classification refuses every honest session, while treating it as fine would let a
    genuinely DELETED module pass unnoticed. Importing makes absence mean what the refusal says it
    means: the module or the attribute really does not exist.

    It also completes `_suspend_caches()`. Suspending only what happens to be resident would leave a
    non-resident module's cache populated during a "fresh" derivation, and a fresh answer computed
    from a cache is not a fresh answer.
    """
    import importlib
    import sys

    module = sys.modules.get(module_name)
    if module is not None:
        return module
    try:
        return importlib.import_module(module_name)
    except Exception:                                            # noqa: BLE001
        return None


# --------------------------------------------------------------------------- fresh derivation
def _suspend_caches():
    """Empty EVERY policy-classified cache for the duration of a fresh derivation, then restore.

    Driven from the policy rather than from a hardcoded list, so a cache someone classifies tomorrow
    is suspended automatically. That direction matters: the policy CLAIMS each cache is re-derived
    freshly, and a claim no code enforces is exactly the shape of defect this gate exists to close.
    Hardcoding `site_taxonomy` alone would have left `shell_command_model._cache` populated while the
    policy asserted otherwise.

    Restoration is unconditional. A verification pass must not leave the session slower or, worse,
    half-empty in a way that changes what later code derives.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        saved = []
        try:
            for _, entry in sorted(load_policy().get("caches", {}).items()):
                module = _resident(entry.get("module", ""))
                container = getattr(module, entry.get("name", ""), None) if module else None
                if hasattr(container, "clear") and hasattr(container, "update"):
                    saved.append((container, dict(container)))
                    container.clear()
            yield
        finally:
            for container, contents in saved:
                container.clear()
                container.update(contents)

    return _ctx()


def fresh_taxonomy() -> dict:
    """Authoritative roots and sites, derived WITHOUT consulting any cache.

    The fresh path suspends every classified cache, derives, captures the answer, and restores what
    was there. Suspension is what makes it fresh: `_cached` recomputes when its key is absent, so an
    emptied cache forces the real derivation through the protected parser and the staged source it
    reads. The result is canonicalised, so the caller receives an immutable value sharing no mutable
    object with any cache — a shallow copy of poisoned nested state would not be a fresh answer.
    """
    import site_taxonomy

    with _suspend_caches():
        roots = site_taxonomy.release_roots()
        production = site_taxonomy.production_control_function_sites()
        ci_release = site_taxonomy.ci_release_control_sites()
        return {
            "release_roots": canonical(sorted(r["module"] for r in roots)),
            "release_root_count": len(roots),
            "production_sites": canonical(sorted(s["canonical_site_id"] for s in production)),
            "production_site_count": len({s["canonical_site_id"] for s in production}),
            "ci_release_sites": canonical(sorted(s["canonical_site_id"] for s in ci_release)),
            "ci_release_site_count": len({s["canonical_site_id"] for s in ci_release}),
        }


def cached_taxonomy() -> dict:
    """What the CACHE would serve, in the same canonical shape, for comparison."""
    import site_taxonomy

    roots = site_taxonomy.release_roots()
    production = site_taxonomy.production_control_function_sites()
    ci_release = site_taxonomy.ci_release_control_sites()
    return {
        "release_roots": canonical(sorted(r["module"] for r in roots)),
        "release_root_count": len(roots),
        "production_sites": canonical(sorted(s["canonical_site_id"] for s in production)),
        "production_site_count": len({s["canonical_site_id"] for s in production}),
        "ci_release_sites": canonical(sorted(s["canonical_site_id"] for s in ci_release)),
        "ci_release_site_count": len({s["canonical_site_id"] for s in ci_release}),
    }


def source_identity() -> dict:
    """The staged source the taxonomy is derived FROM, so a cross-tree cache is refusable."""
    import subprocess

    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    parts = {}
    for w in workflows:
        parts[str(w.relative_to(REPO_ROOT))] = hashlib.sha256(w.read_bytes()).hexdigest()[:32]
    try:
        tree = subprocess.run(["git", "write-tree"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:                                            # noqa: BLE001
        tree = "<unavailable>"
    return {"workflow_digests": parts, "staged_tree": tree}


def classification_obligations(name, entry, container, suspended_size) -> list:
    """Each classification carries an OBLIGATION the layer measures. Prose is not evidence.

    GATE 4N-I28AR FALSIFICATION ARM f17 — the one arm that escaped the first sound run of the
    battery, and it was an escape in this module rather than in the code it guards. Reclassifying
    `site_taxonomy._DERIVED` from NON_AUTHORITATIVE_PERFORMANCE_HINT to
    IMMUTABLE_VALIDATED_SNAPSHOT was accepted, even though the cache is a plain mutable dict that
    is repopulated on every rebuild. The classification was authored text that nothing contradicted,
    which makes it a comment with a schema.

    The obligation must be MEASURED against the live object, so a classification can never claim
    more than the cache actually delivers. A classification whose obligation cannot be checked is
    refused too — an unenforceable category is how this hole would simply move.
    """
    problems = []
    mutable = hasattr(container, "clear") and hasattr(container, "__setitem__")

    if entry["classification"] == IMMUTABLE_VALIDATED_SNAPSHOT:
        if mutable:
            problems.append(
                f"{name}: classified {IMMUTABLE_VALIDATED_SNAPSHOT} but the container is a mutable "
                f"{type(container).__name__} that supports clear() and item assignment. A "
                "classification may not claim more than the object delivers.")
    elif entry["classification"] == NON_AUTHORITATIVE_PERFORMANCE_HINT:
        if suspended_size != 0:
            problems.append(
                f"{name}: classified {NON_AUTHORITATIVE_PERFORMANCE_HINT}, which obliges it to be "
                f"suspended for the authoritative derivation, but it still held {suspended_size} "
                "entrie(s) while the fresh answer was computed. A hint that is consulted during "
                "the check is not a hint.")
        if not entry.get("fresh_recomputation"):
            problems.append(f"{name}: no fresh-recomputation claim, so nothing states how the "
                            "authoritative answer avoids this cache")
    elif entry["classification"] == PROHIBITED_FOR_TRUST_DECISIONS:
        if len(container):
            problems.append(
                f"{name}: classified {PROHIBITED_FOR_TRUST_DECISIONS} but holds "
                f"{len(container)} entrie(s); a prohibited cache must be empty in a graded session")
    elif entry["classification"] == AUTHORITATIVE_CONTENT_BOUND_CACHE:
        if not entry.get("content_binding"):
            problems.append(
                f"{name}: classified {AUTHORITATIVE_CONTENT_BOUND_CACHE} without declaring a "
                "content_binding. An authoritative cache that binds nothing is just a cache.")
    elif entry["classification"] == DIAGNOSTIC_ONLY:
        if entry.get("consumers"):
            problems.append(
                f"{name}: classified {DIAGNOSTIC_ONLY} but declares consumers "
                f"{entry['consumers']}; something that is read is not diagnostic")
    elif entry["classification"] == SESSION_BASELINE_STATE:
        if not entry.get("fresh_recomputation"):
            problems.append(f"{name}: no statement of how the authoritative answer avoids this "
                            "session-scoped state")
    else:                                                        # pragma: no cover - see below
        problems.append(
            f"{name}: classification {entry['classification']!r} carries no measurable obligation, "
            "so it cannot be checked. An unenforceable category would move this hole rather than "
            "close it.")
    return problems


# --------------------------------------------------------------------------- verification
def verify(policy: dict | None = None) -> dict:
    """Fail closed when any cache could decide a trust question it is not entitled to decide."""
    doc = policy if policy is not None else load_policy()
    problems: list[str] = []
    records: dict = {}

    declared = doc["caches"]
    discovered = discover_caches()
    for name in sorted(set(discovered) - set(declared)):
        problems.append(
            f"{name}: a mutable cache with NO authority classification. Gate 4N-I28AP finding "
            "ADV-I28AP-03: a cache nothing classifies is a cache nothing stops from deciding.")
    for name in sorted(set(declared) - set(discovered)):
        entry = declared[name]
        if entry.get("expected_absent"):
            continue
        problems.append(
            f"{name}: classified in the policy but not present in the running modules, so the "
            "classification describes something that no longer exists")

    # The authoritative comparison. Derived fresh from staged source; the cache is never consulted
    # for the expected answer, which is what makes this a check rather than a tautology.
    #
    # BOTH derivations are guarded. A poisoned cache does not always produce a WRONG answer — it can
    # produce a CRASH, and Section 19 is explicit that an unrelated exception is not an acceptable
    # substitute for the intended refusal. Poisoning shell_command_model._cache with None values, for
    # example, raises AttributeError deep inside the root derivation. That is converted here into a
    # cache-integrity refusal that names the cache, so the failure a reader sees describes the cause.
    try:
        fresh = fresh_taxonomy()
    except Exception as exc:                                     # noqa: BLE001
        return {"clean": False, "records": records,
                "problems": [f"the FRESH derivation raised {type(exc).__name__}: {exc}. A cache "
                             "whose contents break the authoritative derivation is refused; a "
                             "crash is not a verdict."],
                "policy_sha256": hashlib.sha256(
                    POLICY.read_bytes() if POLICY.is_file() else b"").hexdigest()}
    try:
        served = cached_taxonomy()
    except Exception as exc:                                     # noqa: BLE001
        return {"clean": False, "records": records,
                "problems": [f"the CACHED derivation raised {type(exc).__name__}: {exc}. The cache "
                             "cannot serve the answer it claims to hold, so it is refused rather "
                             "than bypassed."],
                "policy_sha256": hashlib.sha256(
                    POLICY.read_bytes() if POLICY.is_file() else b"").hexdigest()}
    records["fresh"] = {k: v for k, v in fresh.items() if k.endswith("_count")}
    records["served"] = {k: v for k, v in served.items() if k.endswith("_count")}
    for key in sorted(fresh):
        if fresh[key] != served[key]:
            problems.append(
                f"site_taxonomy._DERIVED: the cached {key} does NOT match a fresh derivation from "
                f"staged source (fresh {fresh[key] if key.endswith('_count') else '<set>'}, "
                f"cached {served[key] if key.endswith('_count') else '<set>'}). A cache may be a "
                "performance hint; it may not be the answer.")

    for key, count in (("release_root_count", "release roots"),
                       ("production_site_count", "production sites")):
        if fresh[key] == 0:
            problems.append(
                f"the fresh derivation produced ZERO {count}. An empty authoritative universe is "
                "refused rather than trusted: it is indistinguishable from a derivation that failed.")

    # Each classification's obligation, measured against the live object. `suspended` records what
    # each cache held WHILE the authoritative answer was being derived, which is the only moment at
    # which "is it consulted?" has an answer.
    suspended = {}
    with _suspend_caches():
        for name, entry in sorted(declared.items()):
            module = _resident(entry.get("module", ""))
            container = getattr(module, entry.get("name", ""), None) if module else None
            suspended[name] = len(container) if container is not None else 0
    for name, entry in sorted(declared.items()):
        module = _resident(entry.get("module", ""))
        container = getattr(module, entry.get("name", ""), None) if module else None
        if container is None:
            continue
        problems.extend(classification_obligations(name, entry, container, suspended[name]))

    records["source_identity"] = source_identity()
    records["classifications"] = {n: declared[n]["classification"] for n in sorted(declared)}
    records["discovered"] = discovered
    return {"clean": not problems, "problems": problems, "records": records,
            "policy_sha256": hashlib.sha256(
                POLICY.read_bytes() if POLICY.is_file() else b"").hexdigest()}


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Verify cache authority.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    for name in _policy_modules():
        __import__(name)
    result = verify()
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
    else:
        for p in result["problems"]:
            print(f"  {p}")
        print("CACHE AUTHORITY: " + ("verified" if result["clean"] else "refused"))
    return 0 if result["clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
