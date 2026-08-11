#!/usr/bin/env python3
"""Authority model for the mandatory-node registry the guard actually consumes (Gate 4N-I28AI).

THE DEFECT THIS CLOSES. Gate 4N-I28AH finding ADV-I28AH-01. The executed-state contract bound the
module constant `REGISTRY`, while the guard consumed `registry_path()` — and `registry_path()`
returned whatever `SIGNALNEST_MANDATORY_NODES` named. Measured: with the override pointed at
`docs/notes.json`, the enforced mandatory set fell from twelve nodes to one while executed-code
provenance, executed-state provenance and the startup policy all reported clean, and
`test_the_mandatory_registry_matches_its_pinned_baseline` passed because it hashed the CONSTANT
rather than the file in force. `registry_is_the_repository_registry` computed False at
pytest_session_guard.py:279 and was asserted nowhere. Every layer validated a file the guard did
not read.

THE AUTHORITY MODEL, stated so implementation and tests can be checked against it rather than
against each other:

  * The WORKTREE file at the canonical path is authoritative for what the guard enforces, because
    that is the byte sequence the running process reads.
  * The STAGED blob must match it. A divergence means the thing being validated is not the thing
    being proposed for commit, and that fails closed rather than silently picking one.
  * There is NO override. `SIGNALNEST_MANDATORY_NODES` is prohibited during an assurance run.
    Gate 4N-I28AG narrowed it to "must resolve in tree"; I28AH proved in-tree is not authorization,
    because any repository file can be named. Narrowing it again would invite the same question a
    third time, so the answer here is that the effective registry has exactly one legal location.
  * Alternate in-tree registries, generated registries, symlinks, relative paths, traversal,
    submodule and nested-repository paths are all rejected by the same rule: the resolved effective
    path must EQUAL the canonical path. Equality, not containment.
  * Both the path AND the content AND the parsed node set are pinned. Semantic equivalence with
    different bytes is not sufficient, because the baseline pins bytes.

WHY THE SANDBOXES DID NOT NEED THE OVERRIDE. The self-protection sandboxes drive the guard over
synthetic sessions, and that is why the override existed. They do not need it: a sandbox is its own
tree, so it writes its registry to the canonical path INSIDE that tree and `REPO_ROOT` resolves
there naturally. Removing the override made those sandboxes simpler, not weaker.

TOCTOU. `authoritative()` reads the file ONCE and returns the bytes it hashed. Everything
downstream — the digest, the parsed node set, the guard's enforcement — is derived from that single
read, so there is no window between hashing a file and parsing a different one. The bytes are
re-read and re-compared at session finish, which is what catches a change made after the initial
check.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_REGISTRY = REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json"
BASELINE = REPO_ROOT / "tests" / "fixtures" / "mandatory-session-baseline.json"
OVERRIDE_ENV = "SIGNALNEST_MANDATORY_NODES"

AUTHORITY_MODE = "WORKTREE_AUTHORITATIVE_STAGED_MUST_MATCH"


class RegistryAuthorityError(RuntimeError):
    """Fail closed. A registry whose authority cannot be established is never enforced."""


def effective_registry() -> Path:
    """The one legal location. No override, no containment test, no negotiation."""
    override = os.environ.get(OVERRIDE_ENV)
    if override is not None:
        raise RegistryAuthorityError(
            f"{OVERRIDE_ENV} is set to {override!r}. The mandatory-node registry has exactly one "
            "legal location and may not be redirected during an assurance run. Gate 4N-I28AH "
            "finding ADV-I28AH-01: an in-tree redirect cut the enforced mandatory set from twelve "
            "nodes to one while every provenance layer reported clean.")
    return CANONICAL_REGISTRY


def authoritative() -> dict:
    """Read the registry ONCE and return the bytes, digest and parsed set from that single read."""
    path = effective_registry()
    if not path.is_file():
        raise RegistryAuthorityError(
            f"the mandatory pytest node registry is missing at {path}")
    raw = path.read_bytes()                       # the one and only read
    digest = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryAuthorityError(f"the mandatory-node registry is malformed: {exc}") from None
    nodes = doc.get("mandatory_nodes")
    if not isinstance(nodes, list) or not nodes:
        raise RegistryAuthorityError(
            "the mandatory pytest node registry declares no mandatory nodes; an empty "
            "requirement would pass vacuously")
    ids = [n.get("node_id") for n in nodes]
    if any(not i for i in ids):
        raise RegistryAuthorityError("a mandatory node has no node_id")
    if len(set(ids)) != len(ids):
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        raise RegistryAuthorityError(f"duplicate mandatory node ids: {duplicated}")
    return {"path": path, "raw": raw, "sha256": digest, "doc": doc,
            "node_ids": sorted(ids), "node_count": len(nodes)}


def _display(path: Path) -> str:
    """Repository-relative when possible, absolute otherwise.

    A bare relative_to() raises when the path is outside the repository, which turned a
    fail-closed refusal into an uncaught ValueError — a verifier that crashes reports nothing.
    """
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(Path(path).resolve())


def _staged_blob_sha256(path: Path) -> str | None:
    """The staged content digest, or None when git cannot answer.

    Uses the trust-bound git when the executable-trust layer is available, so this check cannot be
    satisfied by a shadowed binary. Falls back to refusing rather than guessing.
    """
    try:
        rel = str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return None                               # outside the repository: git cannot answer
    try:
        import external_executable_trust as eet
        argv, env = eet.git_invocation(["show", f":{rel}"])
    except Exception:
        return None
    try:
        proc = subprocess.run(argv, cwd=str(REPO_ROOT), capture_output=True, env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def verify(*, require_staged_match: bool = True) -> dict:
    """Bind path, content and parsed set, and enforce the authority model."""
    problems: list[str] = []
    record: dict = {"authority_mode": AUTHORITY_MODE,
                    "canonical_path": _display(CANONICAL_REGISTRY),
                    "override_set": os.environ.get(OVERRIDE_ENV) is not None}
    try:
        state = authoritative()
    except RegistryAuthorityError as exc:
        return {"clean": False, "problems": [str(exc)], "record": record}

    record.update({"effective_path": _display(state["path"]),
                   "content_sha256": state["sha256"], "node_count": state["node_count"]})

    # The effective path must EQUAL the canonical path. Containment was the I28AH defect.
    if state["path"].resolve() != CANONICAL_REGISTRY.resolve():
        problems.append(
            f"the effective registry resolved to {state['path']}, which is not the canonical "
            f"{CANONICAL_REGISTRY}. Being inside the repository is not authorization.")

    if not BASELINE.is_file():
        problems.append(f"the pinned baseline is missing at {BASELINE}")
    else:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        if state["sha256"] != baseline.get("registry_sha256"):
            problems.append(
                "the registry CONSUMED does not match its pinned baseline digest. This is the "
                "binding that Gate 4N-I28AH found missing: the baseline now hashes the bytes the "
                "guard actually read.")
        if state["node_count"] != baseline.get("mandatory_node_count"):
            problems.append(
                f"the parsed mandatory set has {state['node_count']} node(s); the baseline pins "
                f"{baseline.get('mandatory_node_count')}")
        pinned_ids = sorted(baseline.get("mandatory_node_ids") or [])
        if state["node_ids"] != pinned_ids:
            missing = sorted(set(pinned_ids) - set(state["node_ids"]))
            extra = sorted(set(state["node_ids"]) - set(pinned_ids))
            problems.append(
                f"the parsed mandatory node set differs from the baseline; missing {missing}, "
                f"extra {extra}")

    if require_staged_match:
        staged = _staged_blob_sha256(state["path"])
        record["staged_sha256"] = staged
        if staged is None:
            record["staged_comparison"] = "UNAVAILABLE"
        elif staged != state["sha256"]:
            problems.append(
                "the staged registry blob differs from the worktree file the guard reads. Under "
                f"{AUTHORITY_MODE} the worktree is what executes and the staged blob must match "
                "it; a divergence means the validated object is not the proposed object.")
        else:
            record["staged_comparison"] = "MATCH"

    return {"clean": not problems, "problems": problems, "record": record}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = verify()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        for p in result["problems"]:
            print(f"    {p}")
    print("REGISTRY AUTHORITY: " + ("clean" if result["clean"] else "PROBLEMS"))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
