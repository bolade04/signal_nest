#!/usr/bin/env python3
"""Merge-path security gate (P6-CI-6).

Reads the machine-readable reports the scanners wrote in CI (Trivy JSON, Trivy CycloneDX,
Semgrep JSON), validates that each report is well-formed and describes the artifact the
workflow claims it describes, checks that the analysis actually ran over a non-empty
population, evaluates the findings against security/policy.json, writes a summary file into
the reports directory and exits with a code that keeps the three failure classes apart:

    0  PASS — the analysis ran, the population is what the workflow expects, no finding
       violates the policy (reported findings and explicit exclusions are listed).
    2  FAIL (report missing or malformed) — the scanner left nothing usable behind.
    3  FAIL (policy violation) — the analysis ran and at least one finding violates policy.
    4  FAIL (scanner execution incomplete) — the report exists but the analysis is not what
       the workflow claims: wrong artifact, empty population, scanner errors, an SBOM that
       describes a different image than the vulnerability report, missing coverage.
    5  usage error.
    6  a negative control did not behave as intended (see --expect-fail).

Nothing here invokes a scanner, reads the network, or imports anything outside the
standard library. The policy values are PROPOSED (see security/policy.json); this module
enforces whatever that file says and adopts nothing on its own.

IaC exceptions (P6-CI-6, operator order 2026-10-18). `misconfig --exceptions FILE` applies the
tracked, reviewed security/misconfig-exceptions.json. An exception never hides a finding: an
excepted finding is printed as EXCEPTED, and the exception holds only while the finding's
check, file and ENCLOSING RESOURCE BLOCK (resolved in the checked-out source, because Trivy
reports module addresses only) match an entry, the matched instance count equals the entry's
`expected_instances`, every condition holds against LITERAL values in the source (an
expression cannot be confirmed and fails closed), and today is before `expires`. An expired,
stale (matches nothing), miscounted or condition-failing entry is itself a policy violation,
as is any construct that would let configuration escape a textual reading (an override file
or a .tofu file beside the matched block; for tree-scoped conditions, a .tofu file, a
non-local module source, a provisioner or a generic-provisioning resource anywhere in the
scope). See security/README.md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import posixpath
import re
import sys
from dataclasses import dataclass, field

EXIT_OK = 0
EXIT_REPORT = 2
EXIT_POLICY = 3
EXIT_INCOMPLETE = 4
EXIT_USAGE = 5
EXIT_CONTROL = 6

RESULT_PASS = "RESULT: PASS"
RESULT_POLICY = "RESULT: FAIL (policy violation)"
RESULT_INCOMPLETE = "RESULT: FAIL (scanner execution incomplete)"
RESULT_REPORT = "RESULT: FAIL (report missing or malformed)"
RESULT_CONTROL_OK = "RESULT: NEGATIVE CONTROL DETECTED (policy violation raised as intended)"
RESULT_CONTROL_BAD = "RESULT: NEGATIVE CONTROL FAILED"

# Trivy reports a plain directory as "filesystem" and a directory that is a git repository root
# (the CI checkout) as "repository"; both are filesystem scans of the checked-out tree.
ARTIFACT_TYPES = {"image": ("container_image",), "filesystem": ("filesystem", "repository")}
SBOM_COMPONENT_TYPES = {"image": "container", "filesystem": "application"}
IMAGE_ID_PROPERTY = "aquasecurity:trivy:ImageID"


class ReportError(Exception):
    """The report is missing, unreadable or not the document shape the scanner emits."""


class IncompleteError(Exception):
    """The report exists but the analysis it records is not the analysis the workflow claims."""


class PolicyViolation(Exception):
    """The analysis ran and at least one finding violates the policy."""

    def __init__(self, violations: list[str]):
        super().__init__(f"{len(violations)} policy violation(s)")
        self.violations = violations


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: pathlib.Path, what: str) -> object:
    if not path.is_file():
        raise ReportError(f"{what}: report file is missing: {path}")
    if path.stat().st_size == 0:
        raise ReportError(f"{what}: report file is empty: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportError(f"{what}: report is not valid JSON ({exc.__class__.__name__}): {path}") from exc


def load_policy(path: pathlib.Path) -> dict:
    doc = load_json(path, "policy")
    if not isinstance(doc, dict):
        raise ReportError("policy: top level must be an object")
    for key in ("vulnerabilities", "misconfigurations", "sast", "sbom"):
        if key not in doc or not isinstance(doc[key], dict):
            raise ReportError(f"policy: missing section '{key}'")
    return doc


def _require(cond: bool, message: str, error=IncompleteError) -> None:
    if not cond:
        raise error(message)


PACKAGE_COMPONENT_TYPES = {"library", "application", "framework"}


IDENTITY_PROPERTIES = {IMAGE_ID_PROPERTY, "aquasecurity:trivy:RepoDigest"}


def _props(component: dict) -> dict:
    """Properties of a component, first occurrence wins — except for the IDENTITY properties the
    gate relies on, where a second occurrence with a different value is refused rather than
    resolved (review finding N2). Trivy legitimately repeats other names (one DiffID per layer)."""
    out: dict = {}
    for prop in component.get("properties") or []:
        if isinstance(prop, dict) and "name" in prop:
            name, value = prop["name"], prop.get("value")
            if name in IDENTITY_PROPERTIES and name in out and out[name] != value:
                raise IncompleteError(f"sbom identity property {name!r} appears with conflicting values {out[name]!r} and {value!r}")
            out.setdefault(name, value)
    return out


# --------------------------------------------------------------------------- vulnerabilities
def evaluate_vuln(report: object, policy: dict, *, kind: str, artifact: str,
                  expect_targets: list[str], min_lang_packages: int) -> tuple[list[str], list[str]]:
    """Trivy JSON (SchemaVersion 2) vulnerability report. Returns (summary lines, violations)."""
    vp = policy["vulnerabilities"]
    fail_sev = set(vp["fail_severities"])
    needs_fix = bool(vp.get("fail_requires_fix_available", True))
    actions = vp["scope_actions"]
    dev_targets = set(vp.get("development_targets") or [])
    lines: list[str] = []

    _require(isinstance(report, dict), "trivy vulnerability report: top level is not an object", ReportError)
    _require(report.get("SchemaVersion") == 2, f"trivy vulnerability report: SchemaVersion is {report.get('SchemaVersion')!r}, expected 2", ReportError)
    _require(isinstance(report.get("Results"), list), "trivy vulnerability report: 'Results' missing or not a list", ReportError)
    _require(report.get("ArtifactType") in ARTIFACT_TYPES[kind],
             f"artifact type is {report.get('ArtifactType')!r}, expected one of {ARTIFACT_TYPES[kind]} for kind {kind}")
    _require(report.get("ArtifactName") == artifact,
             f"artifact name is {report.get('ArtifactName')!r}, expected {artifact!r} — the report describes something else")
    results = report["Results"]
    lines.append(f"artifact: {report['ArtifactName']} ({report['ArtifactType']}); created {report.get('CreatedAt')}")

    if kind == "image":
        meta = report.get("Metadata") or {}
        image_id = meta.get("ImageID")
        _require(isinstance(image_id, str) and image_id.startswith("sha256:"), "image report carries no Metadata.ImageID — not a scan of a built image")
        os_meta = meta.get("OS") or {}
        lines.append(f"image id: {image_id}; os: {os_meta.get('Family')} {os_meta.get('Name')}")
        os_pkgs = sum(len(r.get("Packages") or []) for r in results if r.get("Class") == "os-pkgs")
        lang_pkgs = sum(len(r.get("Packages") or []) for r in results if r.get("Class") == "lang-pkgs")
        _require(os_pkgs >= 1, "image report lists no OS packages — the OS layer was not analysed")
        _require(lang_pkgs >= min_lang_packages,
                 f"image report lists {lang_pkgs} language packages, expected at least {min_lang_packages} — the application dependency layer was not analysed")
        lines.append(f"population: {os_pkgs} OS packages, {lang_pkgs} language packages (floor {min_lang_packages})")
    else:
        by_target = {r.get("Target"): r for r in results}
        for target in expect_targets:
            _require(target in by_target, f"expected target {target!r} is absent from the filesystem report — the lock was not analysed")
            n = len(by_target[target].get("Packages") or [])
            _require(n >= 1, f"target {target!r} lists no packages — an empty population is not coverage")
        lines.append("population: " + ", ".join(
            f"{r.get('Target')} ({r.get('Type')}): {len(r.get('Packages') or [])} packages, "
            f"{sum(1 for p in r.get('Packages') or [] if p.get('Dev'))} development"
            for r in results))

    violations: list[str] = []
    excluded: list[str] = []
    reported: list[str] = []
    lower: list[str] = []
    for result in results:
        target = result.get("Target")
        packages = {p.get("Identifier", {}).get("UID"): p for p in result.get("Packages") or [] if isinstance(p, dict)}
        for vuln in result.get("Vulnerabilities") or []:
            sev = vuln.get("Severity", "UNKNOWN")
            fixed = vuln.get("FixedVersion") or ""
            uid = (vuln.get("PkgIdentifier") or {}).get("UID")
            pkg = packages.get(uid) or {}
            if kind == "image":
                scope = "image"
            elif pkg.get("Dev") or target in dev_targets:
                scope = "development"
            else:
                scope = "runtime"
            desc = (f"{vuln.get('VulnerabilityID')} {vuln.get('PkgName')} {vuln.get('InstalledVersion')} -> "
                    f"{fixed or 'no fix'} [{sev}] scope={scope} status={vuln.get('Status')} target={target}")
            if sev not in fail_sev:
                lower.append(desc)
                continue
            if needs_fix and not fixed:
                excluded.append(desc)
                continue
            if actions.get(scope) == "fail":
                violations.append(desc)
            else:
                reported.append(desc)
    lines.append(f"findings at gating severity {sorted(fail_sev)}: {len(violations)} violating, "
                 f"{len(reported)} reported (non-gating scope), {len(excluded)} excluded (no fix available); "
                 f"{len(lower)} at lower severity")
    lines += [f"  VIOLATION {d}" for d in violations]
    lines += [f"  REPORTED  {d}" for d in reported]
    lines += [f"  EXCLUDED  {d}" for d in excluded]
    lines += [f"  lower     {d}" for d in lower]
    return lines, violations


# --------------------------------------------------------------------------- HCL reader (exceptions only)
class HclError(Exception):
    """The configuration could not be read with the certainty an exception condition needs."""


_OPERATORS = ("==", "!=", "<=", ">=", "&&", "||", "=>", "...")
_SINGLE_PUNCT = set("{}[]()=,:?!.+-*/%<>")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_NUMBER_RE = re.compile(r"\d+(\.\d+)?([eE][+-]?\d+)?")
_HEREDOC_RE = re.compile(r"<<(-?)([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\n")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _skip_template(text: str, j: int, line: int) -> tuple[int, int]:
    """`j` is just after `${` or `%{`; return the index after the matching `}` (nested strings honoured)."""
    depth = 1
    while j < len(text):
        c = text[j]
        if c == '"':
            _, _, j, line = _lex_string(text, j, line)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1, line
        elif c == "\n":
            line += 1
        j += 1
    raise HclError(f"line {line}: unterminated template interpolation")


def _lex_string(text: str, i: int, line: int) -> tuple[str, bool, int, int]:
    """`text[i]` is a double quote. Returns (decoded value, contains a template, index after, line)."""
    j, out, template = i + 1, [], False
    while j < len(text):
        c = text[j]
        if c == '"':
            return "".join(out), template, j + 1, line
        if c == "\n":
            raise HclError(f"line {line}: newline inside a quoted string")
        if c == "\\":
            esc = text[j + 1:j + 2]
            if esc in _ESCAPES:
                out.append(_ESCAPES[esc]); j += 2; continue
            width = {"u": 4, "U": 8}.get(esc)
            if width and re.fullmatch(r"[0-9A-Fa-f]{%d}" % width, text[j + 2:j + 2 + width] or ""):
                out.append(chr(int(text[j + 2:j + 2 + width], 16))); j += 2 + width; continue
            raise HclError(f"line {line}: unsupported string escape \\{esc}")
        if text.startswith("$${", j) or text.startswith("%%{", j):
            out.append(text[j + 1:j + 3]); j += 3; continue
        if text.startswith("${", j) or text.startswith("%{", j):
            template = True
            j, line = _skip_template(text, j + 2, line)
            out.append("\x00")
            continue
        out.append(c); j += 1
    raise HclError(f"line {line}: unterminated quoted string")


def hcl_tokens(text: str) -> list[tuple[str, object, int]]:
    """Tokens of HCL native syntax: NL, IDENT, NUM, STR ((value, is_template)), HEREDOC, PUNCT, EOF."""
    toks: list[tuple[str, object, int]] = []
    i, line = 0, 1
    while i < len(text):
        c = text[i]
        if c == "\n":
            toks.append(("NL", None, line)); line += 1; i += 1; continue
        if c in " \t\r\ufeff":
            i += 1; continue
        if c == "#" or text.startswith("//", i):
            k = text.find("\n", i); i = len(text) if k < 0 else k; continue
        if text.startswith("/*", i):
            k = text.find("*/", i + 2)
            if k < 0:
                raise HclError(f"line {line}: unterminated block comment")
            line += text.count("\n", i, k); i = k + 2; continue
        if c == '"':
            start_line = line
            value, template, i, line = _lex_string(text, i, line)
            toks.append(("STR", (value, template), start_line)); continue
        m = _HEREDOC_RE.match(text, i)
        if m:
            marker, pos = m.group(2), m.end()
            while True:
                k = text.find("\n", pos)
                body_line = text[pos:len(text) if k < 0 else k]
                if body_line.strip() == marker:
                    end = len(text) if k < 0 else k
                    break
                if k < 0:
                    raise HclError(f"line {line}: unterminated heredoc {marker}")
                pos = k + 1
            toks.append(("HEREDOC", text[m.end():pos], line)); line += text.count("\n", i, end); i = end; continue
        if c.isdigit():
            m = _NUMBER_RE.match(text, i); toks.append(("NUM", m.group(0), line)); i = m.end(); continue
        m = _IDENT_RE.match(text, i)
        if m:
            toks.append(("IDENT", m.group(0), line)); i = m.end(); continue
        op = next((o for o in _OPERATORS if text.startswith(o, i)), None)
        if op:
            toks.append(("PUNCT", op, line)); i += len(op); continue
        if c in _SINGLE_PUNCT:
            toks.append(("PUNCT", c, line)); i += 1; continue
        raise HclError(f"line {line}: unexpected character {c!r}")
    toks.append(("EOF", None, line))
    return toks


@dataclass
class HclAttr:
    name: str
    tokens: list
    line: int


@dataclass
class HclBlock:
    type: str
    labels: list
    body: list = field(default_factory=list)
    start: int = 0
    end: int = 0


def _parse_body(toks: list, pos: int, nested: bool) -> tuple[list, int]:
    items: list = []
    while True:
        kind, value, line = toks[pos]
        if kind == "NL":
            pos += 1; continue
        if nested and kind == "PUNCT" and value == "}":
            return items, pos
        if not nested and kind == "EOF":
            return items, pos
        if kind != "IDENT":
            raise HclError(f"line {line}: expected an attribute or a block, found {value!r}")
        pos += 1
        if toks[pos][0] == "PUNCT" and toks[pos][1] == "=":
            pos += 1; start, depth = pos, 0
            while True:
                k, v, ln = toks[pos]
                if k == "EOF":
                    if depth == 0:
                        break
                    raise HclError(f"line {ln}: unterminated expression for {value!r}")
                if k == "PUNCT" and v in ("(", "[", "{"):
                    depth += 1
                elif k == "PUNCT" and v in (")", "]", "}"):
                    if depth == 0:
                        break
                    depth -= 1
                elif k == "NL" and depth == 0:
                    break
                pos += 1
            items.append(HclAttr(value, toks[start:pos], line))
            continue
        labels = []
        while toks[pos][0] in ("STR", "IDENT"):
            k, v, ln = toks[pos]
            if k == "STR" and v[1]:
                raise HclError(f"line {ln}: a block label cannot be a template")
            labels.append(v[0] if k == "STR" else v); pos += 1
        if not (toks[pos][0] == "PUNCT" and toks[pos][1] == "{"):
            raise HclError(f"line {toks[pos][2]}: expected '{{' to open block {value!r}")
        body, pos = _parse_body(toks, pos + 1, nested=True)
        items.append(HclBlock(value, labels, body, line, toks[pos][2]))
        pos += 1
    # unreachable


def parse_hcl(text: str) -> list:
    body, _ = _parse_body(hcl_tokens(text), 0, nested=False)
    return body


NON_LITERAL = object()


def hcl_literal(tokens: list) -> object:
    """The value of an attribute expression that is ONE literal token, else NON_LITERAL."""
    toks = [t for t in tokens if t[0] != "NL"]
    if len(toks) != 1:
        return NON_LITERAL
    kind, value, _ = toks[0]
    if kind == "STR" and not value[1]:
        return value[0]
    if kind == "NUM":
        return int(value) if value.isdigit() else float(value)
    if kind == "IDENT" and value in ("true", "false"):
        return value == "true"
    if kind == "IDENT" and value == "null":
        return None
    return NON_LITERAL


def json_literal(value: object) -> object:
    """Terraform JSON syntax: every string is a template, so one carrying ${ or %{ is an expression."""
    if isinstance(value, str):
        return NON_LITERAL if ("${" in value or "%{" in value) else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return NON_LITERAL


# --------------------------------------------------------------------------- the configuration tree
CONFIG_SUFFIXES = (".tf.json", ".tofu.json", ".tf", ".tofu")
OPAQUE_RESOURCE_TYPES = frozenset({
    "aws_cloudformation_stack", "aws_cloudformation_stack_set", "aws_cloudformation_stack_instances",
    "aws_cloudformation_stack_set_instance", "aws_cloudcontrolapi_resource", "aws_servicecatalog_provisioned_product",
})
OPAQUE_RESOURCE_PREFIXES = ("awscc_",)
OPAQUE_DATA_TYPES = frozenset({"external"})
LOCAL_MODULE_SOURCE = re.compile(r"^\.\.?/")


@dataclass
class Declared:
    """One `resource`/`data`/`module` declaration in one configuration file."""
    kind: str            # resource | data | module
    type: str            # resource/data type, or "" for a module
    name: str
    file: str            # path relative to the source root
    start: int           # first line (0 for JSON syntax, where lines are not tracked)
    end: int
    hcl: HclBlock | None
    json_body: object = None


def _config_suffix(path: pathlib.Path) -> str | None:
    return next((sfx for sfx in CONFIG_SUFFIXES if path.name.endswith(sfx)), None)


def _declarations_hcl(rel: str, items: list) -> list[Declared]:
    out = []
    for it in items:
        if isinstance(it, HclBlock) and it.type in ("resource", "data") and len(it.labels) == 2:
            out.append(Declared(it.type, it.labels[0], it.labels[1], rel, it.start, it.end, it))
        elif isinstance(it, HclBlock) and it.type == "module" and len(it.labels) == 1:
            out.append(Declared("module", "", it.labels[0], rel, it.start, it.end, it))
    return out


def _json_items(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _declarations_json(rel: str, doc: object) -> list[Declared]:
    if not isinstance(doc, dict):
        raise HclError(f"{rel}: top level of a JSON configuration file must be an object")
    out = []
    for kind in ("resource", "data"):
        for part in _json_items(doc.get(kind) or {}):
            if not isinstance(part, dict):
                raise HclError(f"{rel}: malformed {kind} section")
            for rtype, named in part.items():
                for group in _json_items(named):
                    if not isinstance(group, dict):
                        raise HclError(f"{rel}: malformed {kind} {rtype}")
                    for name, body in group.items():
                        for b in _json_items(body):
                            out.append(Declared(kind, rtype, name, rel, 0, 0, None, b))
    for part in _json_items(doc.get("module") or {}):
        if not isinstance(part, dict):
            raise HclError(f"{rel}: malformed module section")
        for name, body in part.items():
            for b in _json_items(body):
                out.append(Declared("module", "", name, rel, 0, 0, None, b))
    return out


def read_config_file(root: pathlib.Path, rel: str) -> list[Declared]:
    path = root / rel
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HclError(f"{rel}: unreadable ({exc.__class__.__name__})") from exc
    if rel.endswith(".json"):
        try:
            return _declarations_json(rel, json.loads(text))
        except json.JSONDecodeError as exc:
            raise HclError(f"{rel}: not valid JSON ({exc})") from exc
    try:
        return _declarations_hcl(rel, parse_hcl(text))
    except HclError as exc:
        raise HclError(f"{rel}: {exc}") from exc


def config_files(root: pathlib.Path, scope: str, recursive: bool = True) -> list[str]:
    base = root / scope
    if not base.is_dir():
        raise HclError(f"configuration scope {scope!r} is not a directory under the source root")
    # No directory is skipped, .terraform included: a configuration file committed there and referenced as
    # a local module would be loaded by OpenTofu, so the reading must see it too (provider caches hold no
    # configuration files, so nothing else is read from them).
    return [str(p.relative_to(root)) for p in sorted(base.rglob("*") if recursive else base.iterdir())
            if p.is_file() and _config_suffix(p)]


# --------------------------------------------------------------------------- exception conditions
def _lookup_hcl(block: HclBlock, path: str) -> tuple[str, object]:
    """('ok', [literal values at path]) or ('nonliteral', reason). Every nested block named by a path
    segment is followed; a `dynamic` block or an attribute where a block is expected fails closed."""
    segs = path.split(".")
    bodies = [block.body]
    for seg in segs[:-1]:
        nxt = []
        for body in bodies:
            for it in body:
                if isinstance(it, HclBlock) and it.type == "dynamic" and it.labels[:1] == [seg]:
                    return "nonliteral", f"'{seg}' is generated by a dynamic block (line {it.start})"
                if isinstance(it, HclAttr) and it.name == seg:
                    return "nonliteral", f"'{seg}' is set by an expression (line {it.line})"
                if isinstance(it, HclBlock) and it.type == seg:
                    nxt.append(it.body)
        bodies = nxt
    leaf, values = segs[-1], []
    for body in bodies:
        for it in body:
            if isinstance(it, HclBlock) and it.type == "dynamic" and it.labels[:1] == [leaf]:
                return "nonliteral", f"'{leaf}' is generated by a dynamic block (line {it.start})"
            if isinstance(it, HclBlock) and it.type == leaf:
                return "nonliteral", f"'{leaf}' is a block, not a literal attribute (line {it.start})"
            if isinstance(it, HclAttr) and it.name == leaf:
                v = hcl_literal(it.tokens)
                if v is NON_LITERAL:
                    return "nonliteral", f"'{path}' is an expression, not a literal (line {it.line})"
                values.append(v)
    return "ok", values


def _lookup_json(body: object, path: str) -> tuple[str, object]:
    segs = path.split(".")
    nodes = [body]
    for seg in segs[:-1]:
        nxt = []
        for node in nodes:
            if not isinstance(node, dict):
                return "nonliteral", f"'{seg}' parent is not an object"
            if "dynamic" in node:
                return "nonliteral", "a dynamic block is present"
            if seg in node:
                for item in _json_items(node[seg]):
                    if not isinstance(item, dict):
                        return "nonliteral", f"'{seg}' is not a block"
                    nxt.append(item)
        nodes = nxt
    values = []
    for node in nodes:
        if not isinstance(node, dict):
            return "nonliteral", "attribute parent is not an object"
        if "dynamic" in node:
            return "nonliteral", "a dynamic block is present"
        if segs[-1] in node:
            v = json_literal(node[segs[-1]])
            if v is NON_LITERAL:
                return "nonliteral", f"'{path}' is an expression, not a literal"
            values.append(v)
    return "ok", values


def _lookup(decl: Declared, path: str) -> tuple[str, object]:
    return _lookup_hcl(decl.hcl, path) if decl.hcl is not None else _lookup_json(decl.json_body, path)


def _same_literal(a: object, b: object) -> bool:
    return type(a) is type(b) and a == b


def check_condition(decl: Declared, cond: dict) -> str | None:
    """None when the condition holds, else the violation text (fails closed on any expression)."""
    path = cond["attribute"]
    status, got = _lookup(decl, path)
    where = f"{decl.type}.{decl.name} ({decl.file}{':' + str(decl.start) if decl.start else ''})"
    if status != "ok":
        return f"EXCEPTION_CONDITION_NONLITERAL {where}: {got} — a condition is confirmed only on literal values, so it fails closed"
    if cond.get("absent"):
        present = [v for v in got if v is not None]
        return None if not present else f"EXCEPTION_CONDITION_FAILED {where}: '{path}' must be absent, found {present!r}"
    want = cond["equals"]
    if not got:
        return f"EXCEPTION_CONDITION_FAILED {where}: '{path}' is not set; the condition requires the literal {want!r}"
    bad = [v for v in got if not _same_literal(v, want)]
    return None if not bad else f"EXCEPTION_CONDITION_FAILED {where}: '{path}' is {bad!r}; the condition requires the literal {want!r}"


def tree_integrity(decls: list[Declared], files: list[str], root: pathlib.Path, scope: str) -> list[str]:
    """Constructs a textual reading of the tree cannot see through. Any one fails tree-scoped conditions."""
    problems = []
    base = (root / scope).resolve()
    problems += symlinks_in(root, scope)
    for rel in files:
        if rel.endswith((".tofu", ".tofu.json")):
            problems.append(f"EXCEPTION_TREE_OPAQUE {rel}: an OpenTofu-only file (it replaces the .tf file of the same name); refused rather than reasoned about")
    for d in decls:
        where = f"{d.file}{':' + str(d.start) if d.start else ''}"
        if d.kind == "module":
            status, got = _lookup(d, "source")
            sources = got if status == "ok" else []
            if status != "ok" or len(sources) != 1 or not isinstance(sources[0], str) or not LOCAL_MODULE_SOURCE.match(sources[0]):
                problems.append(f"EXCEPTION_TREE_OPAQUE module.{d.name} ({where}): source {sources!r} is not a local path, so the blocks it declares cannot be read here")
            else:
                # A local path is read only if it lies inside the scope BOTH as OpenTofu computes it (a lexical clean of
                # the joined path, Go path.Clean — review FREEZE-2, permissions lane R2-1: "./link/../../x" is cleaned
                # lexically, while Path.resolve() would follow "link" before applying "..") AND physically (review FREEZE-1,
                # adversarial BLOCKER-1: "../outside"). Symlinks anywhere in the scope are refused separately above.
                lexical = posixpath.normpath(posixpath.join(posixpath.dirname(d.file), sources[0]))
                target = (root / lexical).resolve()
                if not (lexical == scope or lexical.startswith(scope.rstrip("/") + "/")) \
                        or not (target == base or base in target.parents) or not target.is_dir():
                    shown = os.path.relpath(target, root.resolve())
                    problems.append(f"EXCEPTION_TREE_OPAQUE module.{d.name} ({where}): local source {sources[0]!r} resolves to {shown} "
                                    f"(relative to the source root), which is not a directory inside {scope}, so the blocks it declares are not read here")
        elif d.kind == "resource" and (d.type in OPAQUE_RESOURCE_TYPES or d.type.startswith(OPAQUE_RESOURCE_PREFIXES)):
            problems.append(f"EXCEPTION_TREE_OPAQUE {d.type}.{d.name} ({where}): a generic provisioning resource can create blocks no textual reading sees")
        elif d.kind == "data" and d.type in OPAQUE_DATA_TYPES:
            problems.append(f"EXCEPTION_TREE_OPAQUE data.{d.type}.{d.name} ({where}): runs an external program")
        if d.kind == "resource":
            has_prov = (any(isinstance(it, HclBlock) and it.type == "provisioner" for it in d.hcl.body) if d.hcl is not None
                        else isinstance(d.json_body, dict) and "provisioner" in d.json_body)
            if has_prov:
                problems.append(f"EXCEPTION_TREE_OPAQUE {d.type}.{d.name} ({where}): a provisioner can create resources no textual reading sees")
    return problems


def symlinks_in(root: pathlib.Path, scope: str) -> list[str]:
    """Every symlink at or under the scope, refused: a symlink makes the path OpenTofu cleans lexically and the path a
    reader follows physically disagree (review FREEZE-2, permissions lane R2-1). The tree holds none today."""
    found = []
    top = root / scope
    if top.is_symlink():
        found.append(scope)
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        for name in sorted(dirnames + filenames):
            p = pathlib.Path(dirpath) / name
            if p.is_symlink():
                found.append(str(p.relative_to(root)))
    return [f"EXCEPTION_TREE_OPAQUE {rel}: a symlink in {scope} (the path OpenTofu cleans lexically and the path a reader "
            "follows can differ); refused rather than reasoned about" for rel in found]


def _is_override(rel: str) -> bool:
    name = pathlib.PurePosixPath(rel).name
    stem = name[:-len(".json")] if name.endswith(".json") else name
    stem = stem[:-len(".tf")] if stem.endswith(".tf") else stem[:-len(".tofu")] if stem.endswith(".tofu") else stem
    return stem == "override" or stem.endswith("_override")


# --------------------------------------------------------------------------- exceptions file
EXCEPTION_SCHEMA_VERSION = 1
EXCEPTION_CLASSES = ("intended_design", "waf_deferral", "verified_limitation")
EXCEPTION_KEYS = {"id", "class", "check", "target", "resource", "expected_instances", "resource_conditions",
                  "tree_conditions", "expires", "no_expiry_reason", "review", "authority", "statement"}
_PATH_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")
_RESOURCE_RE = re.compile(r"^([a-z0-9_]+)\.([A-Za-z0-9_-]+)$")
_CHECK_RE = re.compile(r"^[A-Z]+-[0-9]{4}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_ADDRESS_RE = re.compile(r"^([a-z0-9_]+)\.([A-Za-z0-9_-]+)(\[.*\])?$")


def _plain_relpath(value: object) -> bool:
    return (isinstance(value, str) and value and not value.startswith("/") and "\\" not in value
            and all(part not in ("", ".", "..") for part in value.split("/")))


def load_exceptions(path: pathlib.Path) -> dict:
    """Schema-checked exceptions document. Anything unexpected is refused (a typo must not widen or narrow)."""
    doc = load_json(path, "misconfiguration exceptions")
    def bad(msg: str) -> ReportError:
        return ReportError(f"misconfiguration exceptions: {msg}")
    if not isinstance(doc, dict):
        raise bad("top level must be an object")
    unknown = {k for k in doc if not k.startswith("_")} - {"schema_version", "tree_scope", "exceptions"}
    if unknown:
        raise bad(f"unknown top-level keys {sorted(unknown)}")
    if doc.get("schema_version") != EXCEPTION_SCHEMA_VERSION or isinstance(doc.get("schema_version"), bool):
        raise bad(f"schema_version must be {EXCEPTION_SCHEMA_VERSION}")
    scope = doc.get("tree_scope")
    if not _plain_relpath(scope):
        raise bad("tree_scope must be a plain relative directory path")
    entries = doc.get("exceptions")
    if not isinstance(entries, list) or not entries:
        raise bad("'exceptions' must be a non-empty list")
    ids, keys = set(), set()
    for n, e in enumerate(entries):
        where = f"entry {n}"
        if not isinstance(e, dict):
            raise bad(f"{where} is not an object")
        extra, missing = set(e) - EXCEPTION_KEYS, (EXCEPTION_KEYS - {"no_expiry_reason"}) - set(e)
        if extra or missing:
            raise bad(f"{where}: unknown keys {sorted(extra)}, missing keys {sorted(missing)}")
        for k in ("id", "check", "target", "resource", "review", "authority", "statement"):
            if not isinstance(e[k], str) or not e[k].strip():
                raise bad(f"{where}: '{k}' must be a non-empty string")
        where = f"entry {e['id']!r}"
        if e["id"] in ids:
            raise bad(f"{where}: duplicate id")
        ids.add(e["id"])
        if e["class"] not in EXCEPTION_CLASSES:
            raise bad(f"{where}: class must be one of {EXCEPTION_CLASSES}")
        if not _CHECK_RE.match(e["check"]):
            raise bad(f"{where}: check {e['check']!r} is not a check id such as AWS-0104")
        if not _plain_relpath(e["target"]) or not e["target"].endswith(".tf") or not e["target"].startswith(scope.rstrip("/") + "/"):
            raise bad(f"{where}: target must be a .tf file path inside tree_scope {scope!r} (JSON and .tofu files cannot be keyed)")
        if not _RESOURCE_RE.match(e["resource"]):
            raise bad(f"{where}: resource must be <type>.<name>")
        if not isinstance(e["expected_instances"], int) or isinstance(e["expected_instances"], bool) or e["expected_instances"] < 1:
            raise bad(f"{where}: expected_instances must be a positive integer")
        key = (e["check"], e["target"], e["resource"])
        if key in keys:
            raise bad(f"{where}: a second entry for the same check, target and resource")
        keys.add(key)
        if e["expires"] is None:
            if e["class"] != "verified_limitation" or not isinstance(e.get("no_expiry_reason"), str) or not e["no_expiry_reason"].strip():
                raise bad(f"{where}: only a verified_limitation entry may omit an expiry, and it must state no_expiry_reason")
        else:
            if "no_expiry_reason" in e:
                raise bad(f"{where}: no_expiry_reason is only for an entry without an expiry")
            if not isinstance(e["expires"], str) or not _DATE_RE.match(e["expires"]):
                raise bad(f"{where}: expires must be YYYY-MM-DD or null")
            try:
                _dt.date.fromisoformat(e["expires"])
            except ValueError as exc:
                raise bad(f"{where}: expires is not a calendar date") from exc
        if not isinstance(e["resource_conditions"], list) or not isinstance(e["tree_conditions"], list):
            raise bad(f"{where}: resource_conditions and tree_conditions must be lists")
        for c in e["resource_conditions"]:
            if not isinstance(c, dict) or not _PATH_RE.match(str(c.get("attribute", ""))):
                raise bad(f"{where}: a resource condition needs an 'attribute' path")
            if set(c) == {"attribute", "absent"} and c["absent"] is True:
                continue
            if set(c) != {"attribute", "equals"} or not isinstance(c["equals"], (str, int, bool)):
                raise bad(f"{where}: a resource condition is exactly {{attribute, equals: <literal>}} or {{attribute, absent: true}}")
        for c in e["tree_conditions"]:
            if not isinstance(c, dict) or set(c) != {"resource_types", "attribute", "equals", "min_blocks"}:
                raise bad(f"{where}: a tree condition is exactly {{resource_types, attribute, equals, min_blocks}}")
            if not isinstance(c["resource_types"], list) or not c["resource_types"] or not all(isinstance(t, str) and re.match(r"^[a-z0-9_]+$", t) for t in c["resource_types"]):
                raise bad(f"{where}: tree condition resource_types must be a non-empty list of resource types")
            if not _PATH_RE.match(str(c["attribute"])) or not isinstance(c["equals"], (str, int, bool)):
                raise bad(f"{where}: tree condition needs an attribute path and a literal")
            if not isinstance(c["min_blocks"], int) or isinstance(c["min_blocks"], bool) or c["min_blocks"] < 1:
                raise bad(f"{where}: tree condition min_blocks must be a positive integer")
    return doc


# --------------------------------------------------------------------------- misconfigurations
@dataclass
class _Candidate:
    desc: str
    entry: dict
    resource: str


def _finding_desc(m: dict, target: str) -> str:
    start = (m.get("CauseMetadata") or {}).get("StartLine")
    return f"{m.get('ID')} [{m.get('Severity', 'UNKNOWN')}] {target}:{start} {m.get('Title')}"


def _resolve_enclosing(root: pathlib.Path, target: str, m: dict, prefix: str, parsed: dict) -> tuple[str | None, str | None]:
    """(resource type.name, None) or (None, violation). Raises IncompleteError if the report does not
    describe the checked-out source (a cause line that differs from the file)."""
    cm = m.get("CauseMetadata") or {}
    start = cm.get("StartLine")
    if not isinstance(start, int) or start < 1:
        return None, f"EXCEPTION_UNRESOLVED {_finding_desc(m, target)}: no start line to resolve"
    path = root / target
    if not path.is_file():
        raise IncompleteError(f"the report names {target}, which is not in the source root {root} — the report does not describe this checkout")
    lines = path.read_text(encoding="utf-8").splitlines()
    for cl in (cm.get("Code") or {}).get("Lines") or []:
        num, truncated = cl.get("Number"), cl.get("Truncated")
        if isinstance(num, int) and num >= 1 and not truncated:
            here = lines[num - 1].rstrip() if num <= len(lines) else None
            if here != str(cl.get("Content", "")).rstrip():
                raise IncompleteError(f"{target}:{num} differs from the report's code line — the report does not describe this checkout")
    if target not in parsed:
        parsed[target] = read_config_file(root, target)
    hits = [d for d in parsed[target] if d.kind == "resource" and d.start <= start <= d.end]
    if len(hits) != 1:
        return None, f"EXCEPTION_UNRESOLVED {_finding_desc(m, target)}: line {start} is not inside exactly one resource block ({len(hits)})"
    resolved = f"{hits[0].type}.{hits[0].name}"
    for occ in cm.get("Occurrences") or []:
        fn = str(occ.get("Filename", ""))
        fn = fn[len(prefix):] if prefix and fn.startswith(prefix) else fn
        mm = _ADDRESS_RE.match(str(occ.get("Resource", "")))
        if fn == target and mm and not str(occ.get("Resource")).startswith("module."):
            if f"{mm.group(1)}.{mm.group(2)}" != resolved:
                return None, f"EXCEPTION_RESOURCE_MISMATCH {_finding_desc(m, target)}: the report names {occ.get('Resource')}, the source block at line {start} is {resolved}"
    return resolved, None


def apply_exceptions(gating: list[tuple[dict, str]], doc: dict, root: pathlib.Path, prefix: str,
                     today: _dt.date) -> tuple[list[str], list[str], list[str]]:
    """Returns (summary lines, excepted descriptions, violations) for the gating findings."""
    entries = doc["exceptions"]
    by_key: dict = {}
    for e in entries:
        by_key.setdefault((e["check"], e["target"]), []).append(e)
    parsed: dict = {}
    violations: list[str] = []
    matched: dict = {e["id"]: [] for e in entries}
    for m, target in gating:
        cands = by_key.get((m.get("ID"), target))
        if not cands:
            violations.append(_finding_desc(m, target)); continue
        try:
            resolved, problem = _resolve_enclosing(root, target, m, prefix, parsed)
        except HclError as exc:
            raise IncompleteError(f"exception resolution could not read the source: {exc}") from exc
        if problem:
            violations += [problem, _finding_desc(m, target)]; continue
        entry = next((e for e in cands if e["resource"] == resolved), None)
        if entry is None:
            violations.append(_finding_desc(m, target) + f" ({resolved}; no exception entry for this resource)"); continue
        matched[entry["id"]].append(_Candidate(_finding_desc(m, target) + f" ({resolved})", entry, resolved))
    lines = [f"exceptions: {len(entries)} entries; tree scope {doc['tree_scope']}; evaluated on {today.isoformat()} (UTC)"]
    excepted: list[str] = []
    tree_cache: dict = {}
    for e in entries:
        got = matched[e["id"]]
        head = f"  exception {e['id']} [{e['class']}] {e['check']} {e['resource']} in {e['target']}"
        failures: list[str] = []
        if not got:
            violations.append(f"EXCEPTION_STALE {e['id']}: matches no gating finding — remove it or correct it")
            lines.append(f"{head}: matched 0/{e['expected_instances']} -> STALE (fails)"); continue
        if len(got) != e["expected_instances"]:
            failures.append(f"EXCEPTION_COUNT_MISMATCH {e['id']}: {len(got)} finding(s) match, expected_instances is {e['expected_instances']}")
        if e["expires"] is not None and today >= _dt.date.fromisoformat(e["expires"]):
            failures.append(f"EXCEPTION_EXPIRED {e['id']}: expired on {e['expires']} (an exception holds only before its expiry date)")
        try:
            failures += _entry_conditions(e, root, doc["tree_scope"], tree_cache)
        except HclError as exc:
            raise IncompleteError(f"exception {e['id']}: the conditions could not be evaluated: {exc}") from exc
        expiry = "no expiry (" + e["no_expiry_reason"] + ")" if e["expires"] is None else f"expires {e['expires']} ({(_dt.date.fromisoformat(e['expires']) - today).days} days)"
        n_cond = len(e["resource_conditions"]) + len(e["tree_conditions"])
        if failures:
            violations += failures + [c.desc + f" — exception {e['id']} NOT applied" for c in got]
            lines.append(f"{head}: matched {len(got)}/{e['expected_instances']}; {expiry} -> NOT APPLIED ({len(failures)} failure(s))")
        else:
            excepted += [f"{c.desc} by {e['id']}; {expiry}" for c in got]
            lines.append(f"{head}: matched {len(got)}/{e['expected_instances']}; {n_cond}/{n_cond} conditions hold; {expiry} -> APPLIED")
    return lines, excepted, violations


def _entry_conditions(e: dict, root: pathlib.Path, scope: str, cache: dict) -> list[str]:
    failures: list[str] = []
    rtype, rname = _RESOURCE_RE.match(e["resource"]).groups()
    if e["resource_conditions"]:
        module_dir = str(pathlib.PurePosixPath(e["target"]).parent)
        files = config_files(root, module_dir, recursive=False)
        for rel in files:
            if rel.endswith((".tofu", ".tofu.json")) or _is_override(rel) or (root / rel).is_symlink():
                failures.append(f"EXCEPTION_MODULE_OVERRIDE {e['id']}: {rel} can replace or override the matched block's attributes invisibly, so its conditions cannot be confirmed")
        if (root / module_dir).is_symlink():
            failures.append(f"EXCEPTION_MODULE_OVERRIDE {e['id']}: the module directory {module_dir} is a symlink")
        decls = [d for rel in files for d in read_config_file(root, rel) if d.kind == "resource" and d.type == rtype and d.name == rname]
        if len(decls) != 1:
            failures.append(f"EXCEPTION_MODULE_OVERRIDE {e['id']}: {e['resource']} is declared {len(decls)} times in {module_dir}; exactly one declaration can be confirmed")
        elif decls[0].file != e["target"]:
            failures.append(f"EXCEPTION_RESOURCE_MISMATCH {e['id']}: {e['resource']} is declared in {decls[0].file}, not {e['target']}")
        else:
            failures += [f for f in (check_condition(decls[0], c) for c in e["resource_conditions"]) if f]
    if e["tree_conditions"]:
        if scope not in cache:
            files = config_files(root, scope)
            decls = [d for rel in files for d in read_config_file(root, rel)]
            cache[scope] = (files, decls, tree_integrity(decls, files, root, scope))
        files, decls, opaque = cache[scope]
        failures += [f"{p} (exception {e['id']})" for p in opaque]
        for c in e["tree_conditions"]:
            blocks = [d for d in decls if d.kind == "resource" and d.type in c["resource_types"]]
            failures += [f for f in (check_condition(d, {"attribute": c["attribute"], "equals": c["equals"]}) for d in blocks) if f]
            if len(blocks) < c["min_blocks"]:
                failures.append(f"EXCEPTION_CONDITION_FAILED {e['id']}: {len(blocks)} {'/'.join(c['resource_types'])} block(s) under {scope}, at least {c['min_blocks']} required")
    return failures


def evaluate_misconfig(report: object, policy: dict, *, expect_types: list[str], exceptions: dict | None = None,
                       source_root: pathlib.Path | None = None, report_prefix: str = "",
                       today: _dt.date | None = None) -> tuple[list[str], list[str]]:
    mp = policy["misconfigurations"]
    fail_sev = set(mp["fail_severities"])
    lines: list[str] = []
    _require(isinstance(report, dict), "trivy misconfiguration report: top level is not an object", ReportError)
    _require(report.get("SchemaVersion") == 2, "trivy misconfiguration report: SchemaVersion is not 2", ReportError)
    _require(isinstance(report.get("Results"), list), "trivy misconfiguration report: 'Results' missing or not a list", ReportError)
    results = report["Results"]
    if report_prefix:
        # Negative controls only (the CLI refuses a prefix without --expect-fail): one scan covers several
        # mutated copies of the tree; keep this copy's results and express their targets as the gate expects.
        results = [dict(r, Target=str(r.get("Target"))[len(report_prefix):]) for r in results
                   if str(r.get("Target", "")).startswith(report_prefix)]
    _require(len(results) >= 1, "misconfiguration report has no results at all — nothing was analysed")
    seen_types = {r.get("Type") for r in results}
    for t in expect_types:
        _require(t in seen_types, f"no result of type {t!r} — that configuration class was not analysed")
    checks = 0
    for r in results:
        s = r.get("MisconfSummary") or {}
        checks += int(s.get("Successes") or 0) + int(s.get("Failures") or 0)
    _require(checks >= 1, "misconfiguration report records zero evaluated checks")
    lines.append(f"population: {len(results)} configuration targets, types {sorted(t for t in seen_types if t)}, {checks} checks evaluated")
    gating: list[tuple[dict, str]] = []
    lower: list[str] = []
    for r in results:
        target = str(r.get("Target"))
        for m in r.get("Misconfigurations") or []:
            if m.get("Status", "FAIL") != "FAIL":
                continue
            if m.get("Severity", "UNKNOWN") in fail_sev:
                gating.append((m, target))
            else:
                lower.append(_finding_desc(m, target))
    excepted: list[str] = []
    if exceptions is None:
        violations = [_finding_desc(m, t) for m, t in gating]
    else:
        exc_lines, excepted, violations = apply_exceptions(gating, exceptions, source_root or pathlib.Path("."),
                                                           report_prefix, today or _dt.datetime.now(_dt.timezone.utc).date())
        lines += exc_lines
    lines.append(f"failed checks: {len(gating)} at gating severity {sorted(fail_sev)}: {len(violations)} violation line(s), "
                 f"{len(excepted)} excepted; {len(lower)} at lower severity")
    lines += [f"  VIOLATION {d}" for d in violations]
    lines += [f"  EXCEPTED  {d}" for d in excepted]
    lines += [f"  lower     {d}" for d in lower]
    return lines, violations


# --------------------------------------------------------------------------- SAST
def evaluate_sast(report: object, policy: dict, *, expect_prefixes: list[str],
                  expect_extensions: list[str]) -> tuple[list[str], list[str]]:
    sp = policy["sast"]
    fail_sev = set(sp["fail_severities"])
    lines: list[str] = []
    _require(isinstance(report, dict), "semgrep report: top level is not an object", ReportError)
    for key in ("results", "errors"):
        _require(isinstance(report.get(key), list), f"semgrep report: '{key}' missing or not a list", ReportError)
    paths = report.get("paths") or {}
    scanned = paths.get("scanned")
    _require(isinstance(scanned, list), "semgrep report: 'paths.scanned' missing", ReportError)
    lines.append(f"semgrep {report.get('version')}: {len(scanned)} files scanned, {len(paths.get('skipped') or [])} skipped")
    errors = report["errors"]
    if sp.get("scanner_errors", "fail") == "fail" and errors:
        detail = "; ".join(f"SCANNER ERROR {e.get('type')} {e.get('level')} rule={e.get('rule_id')} path={e.get('path')}"
                           for e in errors[:20])
        raise IncompleteError(f"semgrep recorded {len(errors)} scanner error(s) (timeouts or parse failures) — "
                              f"the analysis is incomplete: {detail}")
    for prefix in expect_prefixes:
        _require(any(str(p).startswith(prefix) for p in scanned), f"no scanned file under {prefix!r} — that tree was not analysed")
    for ext in expect_extensions:
        _require(any(str(p).endswith(ext) for p in scanned), f"no scanned file with extension {ext!r} — that language was not analysed")
    lines.append("coverage: " + ", ".join(f"{p} ({sum(1 for s in scanned if str(s).startswith(p))} files)" for p in expect_prefixes))
    violations: list[str] = []
    lower: list[str] = []
    for r in report["results"]:
        extra = r.get("extra") or {}
        sev = extra.get("severity", "UNKNOWN")
        conf = (extra.get("metadata") or {}).get("confidence")
        desc = f"{r.get('check_id')} [{sev}/{conf}] {r.get('path')}:{(r.get('start') or {}).get('line')}"
        (violations if sev in fail_sev else lower).append(desc)
    lines.append(f"findings: {len(violations)} at gating severity {sorted(fail_sev)}, {len(lower)} reported at lower severity")
    lines += [f"  VIOLATION {d}" for d in violations]
    lines += [f"  reported  {d}" for d in lower]
    return lines, violations


# --------------------------------------------------------------------------- SBOM
def evaluate_sbom(bom: object, policy: dict, *, kind: str, artifact: str, min_components: int,
                  commit: str, bom_path: pathlib.Path, vuln_report: object | None,
                  checkout: str | None = None) -> tuple[list[str], dict]:
    sp = policy["sbom"]
    lines: list[str] = []
    _require(isinstance(bom, dict), "sbom: top level is not an object", ReportError)
    _require(bom.get("bomFormat") == "CycloneDX", f"sbom: bomFormat is {bom.get('bomFormat')!r}, expected CycloneDX", ReportError)
    _require(str(bom.get("specVersion")) in set(sp["spec_versions"]),
             f"sbom: specVersion {bom.get('specVersion')!r} not in {sp['spec_versions']}", ReportError)
    serial = bom.get("serialNumber")
    _require(isinstance(serial, str) and serial.startswith("urn:uuid:"), "sbom: serialNumber missing or not a urn:uuid", ReportError)
    _require(isinstance(bom.get("components"), list), "sbom: 'components' missing or not a list", ReportError)
    component = (bom.get("metadata") or {}).get("component") or {}
    _require(isinstance(component, dict) and component, "sbom: metadata.component missing", ReportError)
    _require(component.get("type") == SBOM_COMPONENT_TYPES[kind],
             f"sbom describes a {component.get('type')!r} component, expected {SBOM_COMPONENT_TYPES[kind]!r}")
    _require(component.get("name") == artifact,
             f"sbom describes {component.get('name')!r}, expected {artifact!r} — not the artifact this job built")
    components = bom["components"]
    packages = [c for c in components if isinstance(c, dict) and c.get("type") in PACKAGE_COMPONENT_TYPES]
    _require(len(packages) >= min_components,
             f"sbom lists {len(packages)} package components (library/application/framework) among {len(components)} entries, "
             f"expected at least {min_components} — an inventory without package data is not an SBOM (review finding M1)")
    props = _props(component)
    image_id = props.get(IMAGE_ID_PROPERTY)
    if kind == "image":
        _require(isinstance(image_id, str) and image_id.startswith("sha256:"), "image sbom carries no ImageID property — not generated from the built image")
        _require(any(c.get("type") == "operating-system" for c in components), "image sbom lists no operating-system component")
        if vuln_report is not None:
            report_id = ((vuln_report.get("Metadata") or {}).get("ImageID") if isinstance(vuln_report, dict) else None)
            _require(report_id == image_id,
                     f"sbom ImageID {image_id} differs from the vulnerability report's ImageID {report_id} — the two reports describe different images")
    kinds: dict = {}
    for c in components:
        kinds[c.get("type")] = kinds.get(c.get("type"), 0) + 1
    tools = ((bom.get("metadata") or {}).get("tools") or {})
    tool_names = [f"{t.get('name')} {t.get('version')}" for t in (tools.get("components") or []) if isinstance(t, dict)]
    identity = {
        "commit": commit, "checkout_commit": checkout or commit, "kind": kind, "artifact": artifact, "image_id": image_id,
        "bom_path": str(bom_path), "bom_sha256": _sha256(bom_path), "bom_serial": serial,
        "bom_spec_version": str(bom.get("specVersion")), "components": len(components), "package_components": len(packages),
        "components_by_type": kinds, "generator": tool_names, "recorded_at": _utc_now(),
    }
    lines.append(f"sbom: CycloneDX {bom.get('specVersion')} serial {serial}; generator {tool_names}")
    lines.append(f"describes: {component.get('type')} {component.get('name')} image_id={image_id}; commit {commit}"
                 + (f" (built from checkout {checkout})" if checkout and checkout != commit else ""))
    lines.append(f"components: {len(components)} of which {len(packages)} package components (floor {min_components}) by type {kinds}; sha256 {identity['bom_sha256']}")
    return lines, identity


# --------------------------------------------------------------------------- completeness
def check_complete(reports_dir: pathlib.Path, expect_pass: list[str], expect_control: list[str]) -> list[str]:
    lines: list[str] = []
    _require(reports_dir.is_dir(), f"reports directory is missing: {reports_dir}", ReportError)
    for name, wanted in [(n, RESULT_PASS) for n in expect_pass] + [(n, RESULT_CONTROL_OK) for n in expect_control]:
        path = reports_dir / f"{name}.summary.txt"
        _require(path.is_file(), f"required analysis {name!r} left no summary — it did not run", ReportError)
        text = path.read_text(encoding="utf-8")
        results = [l for l in text.splitlines() if l.startswith("RESULT:")]
        _require(len(results) == 1, f"required analysis {name!r} carries {len(results)} RESULT lines, expected exactly one (review finding N1)")
        _require(results[0] == wanted, f"required analysis {name!r} did not end in {wanted!r} (found {results[0]!r})")
        lines.append(f"  ok {name}: {wanted}")
    manifest = reports_dir / "MANIFEST.txt"
    entries = []
    for p in sorted(reports_dir.rglob("*")):
        if p.is_file() and p != manifest:
            entries.append(f"{_sha256(p)}  {p.relative_to(reports_dir)}")
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
    lines.append(f"manifest: {len(entries)} files digested into {manifest}")
    return lines


# --------------------------------------------------------------------------- CLI
def _write_summary(summary_path: pathlib.Path, header: list[str], lines: list[str], result: str) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(header + lines + [result]) + "\n"
    summary_path.write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def run(args: argparse.Namespace) -> int:
    policy = load_policy(pathlib.Path(args.policy))
    reports_dir = pathlib.Path(args.reports_dir)
    summary_path = reports_dir / f"{args.name}.summary.txt"
    header = [f"SECURITY GATE {args.name} ({args.command}) at {_utc_now()}", f"policy: {args.policy} sha256 {_sha256(pathlib.Path(args.policy))}"]
    lines: list[str] = []
    outcome: tuple[str, int]
    violations_seen: list[str] = []
    baseline = getattr(args, "control_baseline", False)
    if (args.expect_violation and not args.expect_fail) or (getattr(args, "report_prefix", None) and not (args.expect_fail or baseline)) \
            or (baseline and (args.expect_fail or not getattr(args, "report_prefix", None))):
        # Control-only options. Refusing them elsewhere means the real gate can never be pointed at a
        # subset of a report: --report-prefix needs --expect-fail (a negative control) or
        # --control-baseline (the unmutated copy of a negative-control set, which must PASS).
        sys.stderr.write("--report-prefix requires --expect-fail or --control-baseline; --expect-violation requires --expect-fail; "
                         "--control-baseline requires --report-prefix and excludes --expect-fail\n")
        return EXIT_USAGE
    try:
        if args.command == "complete":
            lines = check_complete(reports_dir, args.expect_pass or [], args.expect_control or [])
            outcome = (RESULT_PASS, EXIT_OK)
        else:
            report_path = pathlib.Path(args.report)
            header.append(f"report: {report_path}" + (f" sha256 {_sha256(report_path)}" if report_path.is_file() else " (missing)"))
            violations: list[str] = []
            if args.command == "vuln":
                report = load_json(report_path, "trivy vulnerability report")
                lines, violations = evaluate_vuln(report, policy, kind=args.kind, artifact=args.artifact,
                                                  expect_targets=args.expect_target or [],
                                                  min_lang_packages=args.min_lang_packages)
            elif args.command == "misconfig":
                report = load_json(report_path, "trivy misconfiguration report")
                exceptions = None
                if args.exceptions:
                    exc_path = pathlib.Path(args.exceptions)
                    header.append(f"exceptions: {exc_path}" + (f" sha256 {_sha256(exc_path)}" if exc_path.is_file() else " (missing)")
                                  + f"; source root {args.source_root}" + (f"; report prefix {args.report_prefix!r} (negative control)" if args.report_prefix else ""))
                    exceptions = load_exceptions(exc_path)
                lines, violations = evaluate_misconfig(report, policy, expect_types=args.expect_type or [],
                                                       exceptions=exceptions, source_root=pathlib.Path(args.source_root),
                                                       report_prefix=args.report_prefix or "")
            elif args.command == "sast":
                report = load_json(report_path, "semgrep report")
                lines, violations = evaluate_sast(report, policy, expect_prefixes=args.expect_prefix or [],
                                                  expect_extensions=args.expect_extension or [])
            elif args.command == "sbom":
                bom = load_json(report_path, "sbom")
                vuln_report = load_json(pathlib.Path(args.image_id_from), "trivy vulnerability report") if args.image_id_from else None
                lines, identity = evaluate_sbom(bom, policy, kind=args.kind, artifact=args.artifact,
                                                min_components=args.min_components, commit=args.commit,
                                                bom_path=report_path, vuln_report=vuln_report, checkout=args.checkout)
                if args.identity_out:
                    out = pathlib.Path(args.identity_out)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps(identity, indent=1, sort_keys=True) + "\n", encoding="utf-8")
                    lines.append(f"identity written: {out}")
            else:  # pragma: no cover - argparse restricts the choices
                return EXIT_USAGE
            if violations:
                raise PolicyViolation(violations)
            outcome = (RESULT_PASS, EXIT_OK)
    except PolicyViolation as exc:
        lines.append(f"policy: {exc}")
        violations_seen = exc.violations
        outcome = (RESULT_POLICY, EXIT_POLICY)
    except IncompleteError as exc:
        lines.append(f"incomplete: {exc}")
        outcome = (RESULT_INCOMPLETE, EXIT_INCOMPLETE)
    except ReportError as exc:
        lines.append(f"report: {exc}")
        outcome = (RESULT_REPORT, EXIT_REPORT)

    result, code = outcome
    if args.expect_fail:
        # A negative control: the ONLY acceptable outcome is a policy violation. A clean result,
        # a malformed report or an incomplete analysis all mean the control did not prove what
        # it exists to prove, and that is a failure of the control. With --expect-violation the
        # violation must also be the INTENDED one (a token in a violation line), so an unrelated
        # violation cannot make the control pass.
        wanted = args.expect_violation
        found = [v for v in violations_seen if wanted in v] if wanted else None
        if code == EXIT_POLICY and (wanted is None or found):
            lines.append(f"negative control: the underlying verdict was {result!r}"
                         + (f"; the intended violation {wanted!r} is present ({len(found)} line(s))" if wanted else ""))
            result, code = RESULT_CONTROL_OK, EXIT_OK
        elif code == EXIT_POLICY:
            lines.append(f"negative control: the underlying verdict was {result!r}, but no violation line carries {wanted!r}")
            result, code = RESULT_CONTROL_BAD, EXIT_CONTROL
        else:
            lines.append(f"negative control: the underlying verdict was {result!r}, not a policy violation")
            result, code = RESULT_CONTROL_BAD, EXIT_CONTROL
    _write_summary(summary_path, header, lines, result)
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy", default="security/policy.json")
    p.add_argument("--reports-dir", default="security-reports")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, report: bool = True) -> None:
        sp.add_argument("--name", required=True, help="summary name: <reports-dir>/<name>.summary.txt")
        if report:
            sp.add_argument("report")
        sp.add_argument("--expect-fail", action="store_true", help="negative control: succeed only on a policy violation")
        sp.add_argument("--expect-violation", help="negative control: the policy violation must include a line carrying this token")

    v = sub.add_parser("vuln"); common(v)
    v.add_argument("--kind", choices=sorted(ARTIFACT_TYPES), required=True)
    v.add_argument("--artifact", required=True, help="expected ArtifactName (image reference or scan root)")
    v.add_argument("--expect-target", action="append", help="filesystem: a lock target that must be present and populated")
    v.add_argument("--min-lang-packages", type=int, default=1, help="image: minimum language packages")

    m = sub.add_parser("misconfig"); common(m)
    m.add_argument("--expect-type", action="append", help="a configuration type that must appear (dockerfile, terraform)")
    m.add_argument("--exceptions", help="the tracked misconfiguration exceptions file (security/misconfig-exceptions.json)")
    m.add_argument("--source-root", default=".", help="the directory the report's targets are relative to (the scanned checkout)")
    m.add_argument("--report-prefix", help="controls only (requires --expect-fail or --control-baseline): keep results whose target starts with this prefix and strip it")
    m.add_argument("--control-baseline", action="store_true", help="the unmutated copy of a negative-control set: with --report-prefix, must PASS like the real gate")

    s = sub.add_parser("sast"); common(s)
    s.add_argument("--expect-prefix", action="append", help="a path prefix that must have scanned files")
    s.add_argument("--expect-extension", action="append", help="a file extension that must have scanned files")

    b = sub.add_parser("sbom"); common(b)
    b.add_argument("--kind", choices=sorted(SBOM_COMPONENT_TYPES), required=True)
    b.add_argument("--artifact", required=True, help="expected metadata.component.name")
    b.add_argument("--min-components", type=int, required=True)
    b.add_argument("--commit", required=True, help="the reviewed commit (the PR head on a pull_request event)")
    b.add_argument("--checkout", help="the commit actually checked out when it differs (the pull_request merge ref)")
    b.add_argument("--image-id-from", help="image: the vulnerability report whose ImageID the SBOM must match")
    b.add_argument("--identity-out", help="write the identity record (commit, image id, digests) here")

    c = sub.add_parser("complete"); common(c, report=False)
    c.add_argument("--expect-pass", action="append", help="a summary that must end in PASS")
    c.add_argument("--expect-control", action="append", help="a summary that must end in NEGATIVE CONTROL DETECTED")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ReportError as exc:  # the policy itself could not be loaded
        sys.stderr.write(f"{exc}\n{RESULT_REPORT}\n")
        return EXIT_REPORT


if __name__ == "__main__":
    sys.exit(main())
