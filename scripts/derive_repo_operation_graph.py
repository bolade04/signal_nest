#!/usr/bin/env python3
"""SOURCE 1 of the closure join: the repository operation graph (Gate 4N-I5).

Derived by parsing `infra/aws/**/*.tf` directly. It knows nothing about IAM actions,
nothing about the generated policies, and nothing about the expected-closure contract —
it reports only what OpenTofu resources the composition declares and what lifecycle
operations each implies.

This exists because Gate 4N-I4's "independent" contract and the policy generator were
set-identical hand-authored lists sharing every resource ARN, so a SHARED OMISSION was
undetectable. The authority here is the .tf source, which neither hand-authored file
can silently agree to omit.

Usage:
    python3 scripts/derive_repo_operation_graph.py [--json]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra" / "aws"

RESOURCE_RE = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_]+)"\s*\{', re.MULTILINE)
DATA_RE = re.compile(r'^data\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_]+)"\s*\{', re.MULTILINE)
COUNT_RE = re.compile(r'^\s*count\s*=\s*(.+)$', re.MULTILINE)
TAGS_RE = re.compile(r'^\s*tags\s*=', re.MULTILINE)


def _block_body(text: str, start: int) -> str:
    depth, idx = 1, start
    while idx < len(text) and depth:
        if text[idx] == "{":
            depth += 1
        elif text[idx] == "}":
            depth -= 1
        idx += 1
    return text[start : idx - 1]


def derive(root: Path = INFRA) -> dict:
    """Return the declared resource/data graph, excluding provider caches."""
    resources: list[dict] = []
    data_sources: list[dict] = []

    files = sorted(p for p in root.rglob("*.tf") if ".terraform" not in p.parts)
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(REPO_ROOT))
        # A module directory is its own composition unit; bootstrap/ is a separate root.
        unit = "bootstrap" if "/bootstrap/" in f"/{rel}" else (
            rel.split("modules/")[1].split("/")[0] if "modules/" in rel else "root"
        )
        for match in RESOURCE_RE.finditer(text):
            rtype, name = match.groups()
            body = _block_body(text, match.end())
            count = COUNT_RE.search(body)
            resources.append({
                "type": rtype,
                "name": name,
                "unit": unit,
                "file": rel,
                "line": text.count("\n", 0, match.start()) + 1,
                "conditional": bool(count),
                "count_expression": count.group(1).strip() if count else None,
                "declares_tags": bool(TAGS_RE.search(body)),
            })
        for match in DATA_RE.finditer(text):
            dtype, name = match.groups()
            data_sources.append({
                "type": dtype, "name": name, "unit": unit, "file": rel,
                "line": text.count("\n", 0, match.start()) + 1,
            })

    by_type: dict[str, int] = {}
    for r in resources:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1

    return {
        "_source": "SOURCE 1 — parsed from infra/aws/**/*.tf. Knows nothing of IAM actions or of any policy file.",
        "resource_count": len(resources),
        "distinct_resource_types": sorted(by_type),
        "resource_type_counts": dict(sorted(by_type.items())),
        "resources": sorted(resources, key=lambda r: (r["file"], r["line"])),
        "data_source_types": sorted({d["type"] for d in data_sources}),
        "data_sources": sorted(data_sources, key=lambda d: (d["file"], d["line"])),
        "units": sorted({r["unit"] for r in resources}),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    graph = derive()
    if args.json:
        print(json.dumps(graph, indent=2))
    else:
        print(f"{graph['resource_count']} resources, {len(graph['distinct_resource_types'])} distinct types")
        for rtype, n in graph["resource_type_counts"].items():
            print(f"  {n:3d}  {rtype}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
