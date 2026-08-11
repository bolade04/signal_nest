"""An INDEPENDENT deriver for command-position transfers, for Gate 4N-I28BB.

WHY THIS EXISTS, AND WHY IT MAY NOT IMPORT THE PRODUCTION PARSER
---------------------------------------------------------------
Gate 4N-I28AX rejected a candidate on ADV-I28AX-01, and the decisive detail was not that the
parser dropped `exec` children — it was that the "independent superset" agreed with it. Both
`shell_positions` and `docker_boundary._command_words` shared the same blind spot, so their
agreement was CORRELATED ERROR presented as confirmation. Gate 4N-I28AV had recorded a
reconciliation difference of 0 as evidence; it was evidence of nothing.

So this module derives transfers a different way on purpose:

  * it never imports or calls `shell_positions`;
  * it works line-oriented with its own quote/comment stripping, not a token stream;
  * it makes no attempt to model command position in general — only to find `exec`/`coproc`
    occurrences that are syntactically ACTIVE and to name the candidate child token.

Being independent means it is allowed to be cruder. It over-approximates on purpose: when the two
derivations disagree, that is a finding to adjudicate, not a bug to tune away. What it must never
do is share a mechanism with the thing it checks.

WHAT "POSITIVE PRESENCE" MEANS HERE
-----------------------------------
Raw equality between two derivations is not proof, because two empty results are equal. Every
comparison in this module is therefore paired with an EXPECTED-PRESENCE condition: for a fixture
known to contain an active transfer, both sides must be NON-EMPTY and must agree on identity and
line. Two empty results on such a fixture is a FAILURE, not a match.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TRANSFER_WORDS = ("exec", "coproc")

# A transfer word is active only in a command position. This oracle recognises the openers
# line-orientedly: start of line, or after one of these. Deliberately NOT the production model.
_ACTIVE = re.compile(
    r"(?:^|[;&|(]|\b(?:then|else|do|in)\b)\s*(exec|coproc)(?=\s|$|;)"
)
_VALUE_OPTS = {"-a"}
_FLAG_OPTS = {"-c", "-l"}
_REDIR = re.compile(r"^[0-9]*(>>?|<<<|<)&?[0-9-]*$")


def _strip_inert(line: str) -> str:
    """Remove comments and quoted regions so quoted or commented text cannot look active.

    Hand-rolled rather than reusing the production tokeniser — that reuse is precisely what would
    reintroduce correlated error.
    """
    out = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = None
            out.append(" ")            # keep offsets, drop content
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break                       # comment to end of line
        out.append(ch)
        i += 1
    return "".join(out)


def _candidate_child(rest: str) -> tuple[str, str, tuple]:
    """Given the text after the transfer word, name the candidate child and classify it.

    Returns (child, classification, options).
    """
    words = rest.split()
    opts: list = []
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--":
            opts.append(w)
            i += 1
            break
        if _REDIR.match(w):
            i += 1
            if not w.endswith(("&1", "&2")) and i < len(words):
                i += 1
            continue
        if not w.startswith("-") or w == "-":
            break
        if w in _VALUE_OPTS:
            opts.append(w)
            i += 1
            if i >= len(words):
                return "", "UNSUPPORTED_AND_FAIL_CLOSED", tuple(opts)
            opts.append(words[i])
            i += 1
            continue
        if w in _FLAG_OPTS or (len(w) > 1 and all(c in "cl" for c in w[1:])):
            opts.append(w)
            i += 1
            continue
        return "", "UNSUPPORTED_AND_FAIL_CLOSED", tuple(opts)
    if i >= len(words):
        return "", "EXEC_WITHOUT_CHILD", tuple(opts)
    child = words[i].strip('"\'')
    if child.startswith("$") or child.startswith("${") or "$" in child[:2] or "`" in child:
        return child, "DYNAMIC_CHILD_UNRESOLVED", tuple(opts)
    if not re.match(r"^[A-Za-z0-9_./+-]+$", child):
        return child, "MALFORMED_AND_FAIL_CLOSED", tuple(opts)
    return child, "STATIC_CHILD_DISCOVERED", tuple(opts)


def derive(text: str, origin: str = "<source>") -> list:
    """Every syntactically active transfer in one source, derived without the production parser."""
    sites = []
    in_heredoc = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        if in_heredoc is not None:
            if raw.strip() == in_heredoc:
                in_heredoc = None
            continue                    # heredoc bodies are data, not command positions
        hd = re.search(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", raw)
        line = _strip_inert(raw)
        for m in _ACTIVE.finditer(line):
            word = m.group(1)
            # Openers are located on the STRIPPED line, so quoted or commented text can never look
            # like a command position — but the child is read from the RAW line at the same offset,
            # because `_strip_inert` preserves offsets one-for-one and blanking quote CONTENT would
            # destroy the very child it is meant to name. My first version read `rest` from the
            # stripped line: `exec "$VENV_PY" -m uvicorn` became `exec   -m uvicorn`, whose first
            # word `-m` is an unknown option, so all three tracked sites were misreported as
            # UNSUPPORTED. A defect in this oracle, caught by the tracked-tree comparison.
            rest = raw[m.end():]
            if word == "coproc":
                sites.append({"origin": origin, "line": lineno, "word": word,
                              "child": rest.split()[0] if rest.split() else "",
                              "classification": "UNSUPPORTED_AND_FAIL_CLOSED", "options": []})
                continue
            child, classification, opts = _candidate_child(rest)
            sites.append({"origin": origin, "line": lineno, "word": word, "child": child,
                          "classification": classification, "options": list(opts)})
        if hd:
            in_heredoc = hd.group(2)
    return sites


def tracked_sources() -> dict:
    """Every tracked shell source and workflow run block, as text keyed by origin.

    FAILS CLOSED when the tree cannot be enumerated. The first version returned whatever
    `git ls-files` produced, and in a working tree without a Git index that is the EMPTY SET — so
    every downstream comparison compared nothing against nothing and reported agreement. That is
    the same fail-open shape ADV-I28AX-01 exploited, reintroduced in the very module written to
    detect it. It was caught by running the falsification harness in a `git archive` sandbox, which
    has no `.git`, and watching the positive-presence assertions fail rather than pass.
    """
    out = {}
    completed = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True)
    names = completed.stdout.split() if completed.returncode == 0 else []
    if not names:
        # No Git index — a sandbox materialised by `git archive`, or a tree checked out without
        # one. Enumerate the filesystem instead. The DANGER was never "a different enumerator", it
        # was returning the EMPTY SET silently, which makes every downstream comparison compare
        # nothing against nothing and report agreement — the same fail-open shape ADV-I28AX-01
        # exploited. A filesystem walk is a legitimate enumeration; emptiness is not.
        names = [str(p.relative_to(REPO)) for p in REPO.rglob("*")
                 if p.is_file() and ".git/" not in str(p.relative_to(REPO))
                 and (p.suffix in (".sh", ".bash", ".yml")
                      or str(p.relative_to(REPO)).startswith(".github/workflows/"))]
    if not names:
        raise RuntimeError(
            "the tracked file set is EMPTY under both git and a filesystem walk. An empty set "
            "would make the exec transfer reconciliation compare nothing against nothing and "
            "report agreement, so it fails closed instead.")
    for rel in names:
        if rel.endswith((".sh", ".bash")):
            out[rel] = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    for rel in [r for r in names if r.startswith(".github/workflows/")]:
        try:
            import yaml
        except ImportError as exc:                       # fail closed, never silently partial
            raise RuntimeError(
                f"PyYAML is unavailable, so workflow run blocks cannot be derived: {exc}") from exc
        doc = yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            for index, step in enumerate(job.get("steps") or []):
                if step.get("run"):
                    out[f"{rel}#{job_name}#{index}"] = step["run"]
    return out


def derive_tracked() -> list:
    sites = []
    for origin, text in sorted(tracked_sources().items()):
        sites.extend(derive(text, origin=origin))
    return sites


def _key(site) -> tuple:
    return (site["origin"], site["line"], site["word"], site["child"], site["classification"])


def compare(production: list, oracle: list, *, expect_present: bool) -> dict:
    """Compare two derivations WITH a positive-presence condition.

    `expect_present` is the whole point. Without it, two empty results compare equal and a
    correlated omission reads as agreement — which is exactly how ADV-I28AX-01 survived a
    reconciliation that reported difference 0.
    """
    prod = {_key(s) for s in production}
    orac = {_key(s) for s in oracle}
    problems = []
    if expect_present and not prod:
        problems.append("production derivation is EMPTY on a fixture known to contain a transfer")
    if expect_present and not orac:
        problems.append("oracle derivation is EMPTY on a fixture known to contain a transfer")
    for missing in sorted(orac - prod):
        problems.append(f"the oracle found a transfer the production parser did not: {missing}")
    for extra in sorted(prod - orac):
        problems.append(f"the production parser found a transfer the oracle did not: {extra}")
    return {"production": len(prod), "oracle": len(orac), "agree": prod == orac,
            "expected_present": expect_present, "problems": problems,
            "clean": not problems}


def main(argv=None) -> int:
    sites = derive_tracked()
    print(json.dumps({"tracked_transfer_sites": len(sites), "sites": sites}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
