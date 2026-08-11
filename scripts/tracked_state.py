#!/usr/bin/env python3
"""Authoritative git state model and predicted commit tree (Gate 4N-I20, ARCH-H3/AWS-3;
extended at Gate 4N-I23).

THE DEFECT THIS CLOSES. The tracked-anchor tests shelled out to `git ls-files`, which reports
THE INDEX. On this branch the fixtures are STAGED ADDITIONS and the branch is zero commits
ahead of `a4ec974`, so `git ls-files tests/fixtures` returns paths and every "tracked" assertion
went green — while `git ls-tree HEAD -- tests/fixtures` returns nothing. Two source comments
went further and claimed the fixtures were "committed". The stated purpose of the control was
that "an untracked anchor can be regenerated with no history and no review trail"; a STAGED
anchor has no history and no review trail either, so the property the tests named was never
established by their passing.

WHY NOT SIMPLY ASSERT AGAINST HEAD. Because on this branch that assertion would be FALSE, and
making it pass would require committing — which no gate in this chain has authorized. The
honest correction is to stop using one word for six different states and to assert the state
that actually holds, while separately proving the file will reach the commit that is eventually
made. That is what PREDICTED_IN_COMMIT below is for.

THE SEVEN STATES, kept distinct on purpose:

  UNTRACKED            in the working tree, unknown to the index
  IGNORED              deliberately excluded; cannot reach a commit without --force
  STAGED_ADDITION      in the index, absent from HEAD — no history, no review trail
  STAGED_MODIFICATION  in HEAD, with INDEX content differing from HEAD  (added at Gate 4N-I23)
  TRACKED_IN_HEAD      present in HEAD's tree, index content identical; has history
  MODIFIED_IN_WORKTREE tracked, with working-tree bytes differing from the index
  ABSENT               not present anywhere

STAGED_MODIFICATION was added at I23 because the six-state model could not describe the
repository once the full verification package was staged: a staged modification returned
TRACKED_IN_HEAD, exactly like a file nobody had touched. Six was not a principled number; it
was however many states had been needed so far.

"Tracked" is never used unqualified by this module, and `assert_state()` refuses the word.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

UNTRACKED = "UNTRACKED"
IGNORED = "IGNORED"
STAGED_ADDITION = "STAGED_ADDITION"
# GATE 4N-I23: a path in HEAD whose INDEX content differs from HEAD. Previously collapsed into
# TRACKED_IN_HEAD, which made a staged modification read exactly like an untouched file.
STAGED_MODIFICATION = "STAGED_MODIFICATION"
TRACKED_IN_HEAD = "TRACKED_IN_HEAD"
MODIFIED_IN_WORKTREE = "MODIFIED_IN_WORKTREE"
ABSENT = "ABSENT"

STATES = (UNTRACKED, IGNORED, STAGED_ADDITION, STAGED_MODIFICATION, TRACKED_IN_HEAD,
          MODIFIED_IN_WORKTREE, ABSENT)

# The word this module exists to stop people using loosely.
AMBIGUOUS_TERMS = ("tracked", "committed")


class TrackedStateError(Exception):
    """Fail-closed."""


def _git(*args, check: bool = False, env: dict | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    if check and proc.returncode != 0:
        raise TrackedStateError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def head_paths() -> set[str]:
    """Paths present in HEAD's tree. THIS is what 'committed' means."""
    out = _git("ls-tree", "-r", "--name-only", "HEAD")
    return {line for line in out.splitlines() if line}


def index_paths() -> set[str]:
    """Paths in the INDEX. This is what `git ls-files` reports — NOT history."""
    out = _git("ls-files")
    return {line for line in out.splitlines() if line}


def state_of(path: str) -> str:
    """The exact state of one repository-relative path."""
    rel = str(path)
    in_head = rel in head_paths()
    in_index = rel in index_paths()
    exists = (REPO_ROOT / rel).exists()

    if not exists and not in_index and not in_head:
        if _git("check-ignore", rel).strip():
            return IGNORED
        return ABSENT
    if _git("check-ignore", rel).strip():
        return IGNORED
    if in_head:
        if _git("diff", "--name-only", "--", rel).strip():
            return MODIFIED_IN_WORKTREE
        # GATE 4N-I23. A file that is in HEAD *and* staged with different content is NOT the
        # same thing as an untouched tracked file, and until now both returned
        # TRACKED_IN_HEAD — so a staged modification was indistinguishable from a file nobody
        # had touched. That is precisely the one-word-for-several-states defect this module
        # exists to remove (ARCH-H3/AWS-3), and the I23 packaging work made it load-bearing:
        # 12 of the paths in the commit are staged modifications.
        if _git("diff", "--cached", "--name-only", "--", rel).strip():
            return STAGED_MODIFICATION
        return TRACKED_IN_HEAD
    if in_index:
        return STAGED_ADDITION
    return UNTRACKED


def assert_state(path: str, expected: str) -> None:
    """Assert an EXACT state. Refuses the ambiguous vocabulary outright."""
    if expected.lower() in AMBIGUOUS_TERMS:
        raise TrackedStateError(
            f"{expected!r} is not a state. Name one of {STATES}: 'tracked' conflates the index "
            "with history, which is the Gate 4N-I17 defect this module exists to remove.")
    if expected not in STATES:
        raise TrackedStateError(f"unknown state {expected!r}; expected one of {STATES}")
    actual = state_of(path)
    if actual != expected:
        raise TrackedStateError(f"{path}: state is {actual}, not {expected}")


# --- predicted commit tree ------------------------------------------------------------------


def index_tree_hash() -> str:
    """The tree the CURRENT INDEX would produce — computed from a COPY of .git/index.

    GATE 4N-I23. Three separate I22 lanes (scope, architect H1, adversarial F4) found this
    field carrying HEAD's tree under the index's name: `repository_state_record` returned
    `... if False else None` and `production_certification.resolve_repository_binding()` then
    substituted `record["head_tree_hash"]` for it. The two are provably different objects, and
    the adversarial lane demonstrated the consequence — staging a new file left the field
    unchanged. A field whose name asserts a property its value does not have is the exact
    ambiguity this module exists to remove.

    The original refusal to run `git write-tree` was sound in its reasoning (it writes objects
    into the database) but reached the wrong conclusion: writing tree objects is harmless and
    idempotent, whereas mislabelling a binding is not. What actually must be protected is the
    real index FILE, so this copies it and points GIT_INDEX_FILE at the copy — the real index
    is never opened for writing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "index.copy")
        shutil.copy2(os.path.join(str(REPO_ROOT), ".git", "index"), copy)
        return _git("write-tree", env={"GIT_INDEX_FILE": copy}, check=True).strip()


def predicted_commit_tree(extra_paths: list[str] | None = None) -> dict:
    """The tree `git commit` WOULD produce right now — i.e. the INDEX tree.

    GATE 4N-I24C, finding I24C-03. This previously unioned `git diff --cached` with
    `git diff` and re-added those paths with `git update-index --add`, which reads the
    WORKING TREE. Two consequences, both executed by reviewers:

      * an index-only tamper was silently HEALED: pointing the index entry for a file at its
        HEAD blob while the worktree stayed correct left the predicted tree unchanged and
        coherence reporting "coherent", even though `git commit` would have committed the
        tampered content; and
      * a worktree edit to a staged ADDITION moved the predicted tree even though `git commit`
        would not carry it.

    So the function predicted `git commit -a`, while its name promised `git commit`. It now
    measures the index. The worktree-inclusive projection still exists, under a name that says
    so: `predicted_tree_if_everything_were_added()`.
    """
    if extra_paths:
        return _predicted_tree_including_worktree(extra_paths)
    tree = index_tree_hash()
    listing = _git("ls-tree", "-r", tree).splitlines()
    entries = {}
    for line in listing:
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, _type, blob = meta.split()
        entries[path] = {"mode": mode, "blob": blob}
    staged = sorted(p for p in _git("diff", "--cached", "--name-only").splitlines() if p)
    return {
        "predicted_tree_hash": tree,
        "head_tree_hash": _git("rev-parse", "HEAD^{tree}").strip(),
        "path_count": len(entries),
        "entries": entries,
        "added_relative_to_head": sorted(set(entries) - head_paths()),
        "staged_additions_included": staged,
        "worktree_modifications_included": [],
        "extra_paths_included": [],
        "measures": "THE INDEX — what `git commit` produces. Not the working tree.",
    }


def _predicted_tree_including_worktree(extra_paths: list[str] | None = None) -> dict:
    """Build the tree a commit WOULD produce, without creating one and without touching the index.

    Uses a THROWAWAY index file (GIT_INDEX_FILE) seeded from HEAD, so the real index is never
    read for writing and never modified. This is what lets a manifest bind to the bytes that
    will actually be committed rather than to whatever happens to be in the working tree at
    review time — the two are not the same, and Gate 4N-I17's manifest could not tell them
    apart.
    """
    extra_paths = extra_paths or []
    with tempfile.TemporaryDirectory() as tmp:
        index_file = os.path.join(tmp, "predicted.index")
        env = {"GIT_INDEX_FILE": index_file}

        _git("read-tree", "HEAD", env=env, check=True)
        # EVERY path whose INDEX content differs from HEAD — additions AND modifications
        # to already-tracked files. Gate 4N-I23: this was `index_paths() - head_paths()`,
        # which yields additions ONLY. A staged modification to a tracked file is in HEAD,
        # so it was excluded here, and once staged it no longer appears in `git diff
        # --name-only` either — it fell through both sets and the predicted tree silently
        # kept HEAD's version of it. That was invisible at I22 only because the tracked
        # modifications happened to be unstaged; staging the full package exposed it.
        # A function whose name promises the commit tree must not be correct only under an
        # accidental precondition.
        staged = sorted(p for p in _git("diff", "--cached", "--name-only").splitlines() if p)
        modified = sorted(p for p in _git("diff", "--name-only").splitlines() if p)
        to_add = sorted(set(staged) | set(modified) | set(extra_paths))
        if to_add:
            _git("update-index", "--add", "--", *to_add, env=env, check=True)

        tree = _git("write-tree", env=env, check=True).strip()
        listing = _git("ls-tree", "-r", tree, env=env).splitlines()

    entries = {}
    for line in listing:
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode, _type, blob = meta.split()
        entries[path] = {"mode": mode, "blob": blob}

    return {
        "predicted_tree_hash": tree,
        "head_tree_hash": _git("rev-parse", "HEAD^{tree}").strip(),
        "path_count": len(entries),
        "entries": entries,
        "added_relative_to_head": sorted(set(entries) - head_paths()),
        "staged_additions_included": staged,
        "worktree_modifications_included": modified,
        "extra_paths_included": sorted(extra_paths),
    }


def repository_state_record(extra_paths: list[str] | None = None) -> dict:
    """Every distinct hash a manifest must bind to. None of these substitutes for another."""
    predicted = predicted_commit_tree(extra_paths)
    def h(*args):
        return hashlib.sha256(_git(*args).encode()).hexdigest()
    untracked = sorted(p for p in _git("ls-files", "--others", "--exclude-standard").splitlines() if p)
    return {
        "head": _git("rev-parse", "HEAD").strip(),
        "head_tree_hash": predicted["head_tree_hash"],
        "index_tree_hash": index_tree_hash(),
        "predicted_commit_tree_hash": predicted["predicted_tree_hash"],
        "unstaged_diff_sha256": h("diff"),
        "staged_diff_sha256": h("diff", "--cached"),
        "full_tracked_diff_sha256": h("diff", "HEAD"),
        "untracked_inventory_sha256": hashlib.sha256("\n".join(untracked).encode()).hexdigest(),
        "untracked_count": len(untracked),
        "head_path_count": len(head_paths()),
        "index_path_count": len(index_paths()),
        "staged_addition_count": len(index_paths() - head_paths()),
        "_note": ("index_tree_hash is the tree the CURRENT INDEX would produce, computed with "
                  "`git write-tree` against a COPY of .git/index so the real index file is "
                  "never opened for writing. Gate 4N-I23: it previously returned None and the "
                  "certification consumer substituted head_tree_hash for it, so a field named "
                  "for the index carried HEAD's tree. Writing tree objects is harmless and "
                  "idempotent; mislabelling a binding is not."),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--path", action="append", default=[])
    args = parser.parse_args()
    if args.path:
        for p in args.path:
            print(f"  {state_of(p):22} {p}")
        return 0
    record = repository_state_record()
    if args.json:
        print(json.dumps(record, indent=2, ensure_ascii=True))
    else:
        print(f"  HEAD {record['head'][:12]}  head_tree {record['head_tree_hash'][:12]}  "
              f"predicted {record['predicted_commit_tree_hash'][:12]}")
        print(f"  HEAD paths {record['head_path_count']}  index {record['index_path_count']}  "
              f"staged additions {record['staged_addition_count']}  untracked {record['untracked_count']}")
    print("TRACKED STATE: recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
