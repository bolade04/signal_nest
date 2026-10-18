#!/usr/bin/env python3
"""Fetch the Semgrep registry rulesets the SAST gate runs, and record their provenance.

The registry serves each ruleset as one YAML document at https://semgrep.dev/c/<name>. The
registry is not version-pinned, so the gate cannot promise byte-identical rules across runs;
what it CAN do is refuse an empty or implausible download and record exactly which bytes it
ran: every file is written next to a PROVENANCE.json carrying the URL, byte count, sha256 and
rule count, and `semgrep --validate` is run on the directory by the workflow before any scan.

Standard library only. Exit 0 only when every ruleset was fetched and passed the checks.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
import re
import ssl
import sys
import urllib.error
import urllib.request

REGISTRY = "https://semgrep.dev/c/"
RULESETS = ("p/python", "p/typescript", "p/javascript", "p/react", "p/security-audit")
MIN_BYTES = 10_000
MIN_RULES = 4
TIMEOUT_SECONDS = 60
RULE_ID = re.compile(r"^- id: \S+$", re.M)


def _ssl_context() -> ssl.SSLContext:
    """The platform trust store, or certifi's bundle when the interpreter ships without one
    (python.org macOS builds). certifi is a dependency of the locked Semgrep install."""
    try:
        import certifi  # noqa: WPS433 - optional, only used to locate a CA bundle
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch(name: str) -> bytes:
    req = urllib.request.Request(REGISTRY + name, headers={"User-Agent": "signalnest-security-gate"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{name}: HTTP {resp.status}")
        return resp.read()


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        sys.stderr.write("usage: fetch_semgrep_rules.py <output directory> [<provenance copy path>]\n")
        return 5
    out = pathlib.Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for name in RULESETS:
        try:
            data = fetch(name)
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            sys.stderr.write(f"FAIL {name}: {exc}\n")
            return 1
        text = data.decode("utf-8", errors="replace")
        rules = len(RULE_ID.findall(text))
        if len(data) < MIN_BYTES or not text.startswith("rules:") or rules < MIN_RULES:
            sys.stderr.write(f"FAIL {name}: implausible ruleset ({len(data)} bytes, {rules} rule ids)\n")
            return 1
        path = out / (name.replace("/", "-") + ".yml")
        path.write_bytes(data)
        records.append({"ruleset": name, "url": REGISTRY + name, "file": path.name, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(), "rule_ids": rules})
        print(f"ok {name}: {rules} rules, {len(data)} bytes, sha256 {records[-1]['sha256']}")
    prov = {"fetched_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "registry": REGISTRY, "rulesets": records, "total_rule_ids": sum(r["rule_ids"] for r in records)}
    text = json.dumps(prov, indent=1) + "\n"
    (out / "PROVENANCE.json").write_text(text, encoding="utf-8")
    if len(argv) == 3:
        copy = pathlib.Path(argv[2])
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(text, encoding="utf-8")
    print(f"provenance: {out / 'PROVENANCE.json'} ({prov['total_rule_ids']} rule ids)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
