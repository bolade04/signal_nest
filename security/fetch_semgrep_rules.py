#!/usr/bin/env python3
"""Fetch the Semgrep registry rulesets the SAST gate runs, and record their provenance.

The registry serves each ruleset as one document at https://semgrep.dev/c/<name>, either as
YAML (`text/yaml`) or as JSON (`application/json`): on 2026-10-18 `p/javascript` switched
from YAML to JSON between two CI runs an hour apart while the other four stayed YAML. Both
are valid Semgrep configuration formats and Semgrep loads either from a config directory.
The registry is not version-pinned, so the gate cannot promise byte-identical rules across
runs. What this script does promise:

* every document is parsed structurally — JSON with the standard library, YAML with
  ruamel.yaml's safe loader (hash-locked in security/requirements-semgrep.txt) — and must be
  a mapping whose `rules` is a non-empty list of mappings, each carrying a non-empty string
  `id`; the rule count is the number of those entries. HTML, error bodies, empty, malformed
  or structurally invalid documents are refused;
* the existing floors still apply: at least MIN_BYTES bytes and MIN_RULES rules per ruleset;
* the RAW downloaded bytes are written unchanged, always under a `.yml` name: Semgrep's
  config-directory loader reads only `.yml`/`.yaml` files and silently skips `.json` ones
  (measured with Semgrep 1.181.0: a directory holding the JSON form as `.json` loads 0 rules
  and `--validate` still exits 0; the same bytes as `.yml` load all 74), and a JSON document
  is valid YAML. The detected format, sha256, byte count, content type and rule count are
  recorded in PROVENANCE.json;
* acquisition is all-or-nothing: rulesets are staged in `<out>.partial` and the directory is
  renamed to `<out>` only after all of them passed, so an incomplete population is never left
  where `semgrep --config <out>` would read it, and a pre-existing or stale `<out>` is refused
  rather than reused.

The workflow then runs `semgrep --validate` on the directory before any scan. Exit 0 only
when every ruleset was fetched and passed the checks; exit 1 otherwise (nothing at `<out>`).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
import shutil
import ssl
import sys
import urllib.error
import urllib.request

REGISTRY = "https://semgrep.dev/c/"
RULESETS = ("p/python", "p/typescript", "p/javascript", "p/react", "p/security-audit")
MIN_BYTES = 10_000
MIN_RULES = 4
TIMEOUT_SECONDS = 60


class RulesetError(Exception):
    """A downloaded document that is not a usable Semgrep configuration."""


def _ssl_context() -> ssl.SSLContext:
    """The platform trust store, or certifi's bundle when the interpreter ships without one
    (python.org macOS builds). certifi is a dependency of the locked Semgrep install."""
    try:
        import certifi  # noqa: WPS433 - optional, only used to locate a CA bundle
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _load_yaml(text: str) -> object:
    try:
        from ruamel.yaml import YAML  # hash-locked via security/requirements-semgrep.txt
        from ruamel.yaml.error import YAMLError
    except ImportError as exc:  # never fall back to a regex count
        raise RulesetError("no safe YAML parser available (ruamel.yaml missing)") from exc
    try:
        return YAML(typ="safe", pure=True).load(text)
    except YAMLError as exc:
        raise RulesetError(f"malformed YAML ({exc.__class__.__name__})") from exc


def parse_ruleset(data: bytes) -> tuple[int, str]:
    """(rule count, detected format "json"|"yaml") of a registry document; raises
    RulesetError otherwise."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulesetError("document is not UTF-8") from exc
    stripped = text.lstrip()
    if not stripped:
        raise RulesetError("empty document")
    if stripped.startswith("<"):
        raise RulesetError("HTML/XML body, not a Semgrep configuration")
    if stripped[0] in "{[":
        try:
            doc, fmt = json.loads(text), "json"
        except json.JSONDecodeError as exc:
            raise RulesetError(f"malformed JSON ({exc.msg})") from exc
    else:
        doc, fmt = _load_yaml(text), "yaml"
    if not isinstance(doc, dict) or "rules" not in doc:
        raise RulesetError("top level is not a mapping with a 'rules' key")
    rules = doc["rules"]
    if not isinstance(rules, list) or not rules:
        raise RulesetError("'rules' is not a non-empty list")
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"].strip():
            raise RulesetError(f"rule entry {index} is not a mapping with a non-empty string 'id'")
    return len(rules), fmt


def fetch(name: str) -> tuple[bytes, str]:
    req = urllib.request.Request(REGISTRY + name, headers={"User-Agent": "signalnest-security-gate"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=_ssl_context()) as resp:
        if resp.status != 200:
            raise RuntimeError(f"{name}: HTTP {resp.status}")
        return resp.read(), resp.headers.get("Content-Type", "")


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        sys.stderr.write("usage: fetch_semgrep_rules.py <output directory> [<provenance copy path>]\n")
        return 5
    out = pathlib.Path(argv[1])
    staging = out.with_name(out.name + ".partial")
    if out.exists():
        sys.stderr.write(f"FAIL: {out} already exists; refusing to reuse or mix stale rules\n")
        return 1
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    records = []
    for name in RULESETS:
        try:
            data, content_type = fetch(name)
            rules, fmt = parse_ruleset(data)
        except (urllib.error.URLError, OSError, RuntimeError, RulesetError) as exc:
            sys.stderr.write(f"FAIL {name}: {exc}\n")
            shutil.rmtree(staging)
            return 1
        if len(data) < MIN_BYTES or rules < MIN_RULES:
            sys.stderr.write(f"FAIL {name}: implausible ruleset ({len(data)} bytes, {rules} rules, {content_type!r})\n")
            shutil.rmtree(staging)
            return 1
        path = staging / (name.replace("/", "-") + ".yml")  # see the module docstring
        path.write_bytes(data)
        records.append({"ruleset": name, "url": REGISTRY + name, "file": path.name, "format": fmt,
                        "content_type": content_type, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(), "rule_ids": rules})
        print(f"ok {name}: {rules} rules, {fmt}, {len(data)} bytes, {content_type or 'no content type'}, "
              f"sha256 {records[-1]['sha256']}")
    prov = {"fetched_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "registry": REGISTRY, "rulesets": records, "total_rule_ids": sum(r["rule_ids"] for r in records)}
    text = json.dumps(prov, indent=1) + "\n"
    (staging / "PROVENANCE.json").write_text(text, encoding="utf-8")
    staging.rename(out)
    if len(argv) == 3:
        copy = pathlib.Path(argv[2])
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(text, encoding="utf-8")
    print(f"provenance: {out / 'PROVENANCE.json'} ({prov['total_rule_ids']} rule ids)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
