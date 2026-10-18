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
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import sys

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


# --------------------------------------------------------------------------- misconfigurations
def evaluate_misconfig(report: object, policy: dict, *, expect_types: list[str]) -> tuple[list[str], list[str]]:
    mp = policy["misconfigurations"]
    fail_sev = set(mp["fail_severities"])
    lines: list[str] = []
    _require(isinstance(report, dict), "trivy misconfiguration report: top level is not an object", ReportError)
    _require(report.get("SchemaVersion") == 2, "trivy misconfiguration report: SchemaVersion is not 2", ReportError)
    _require(isinstance(report.get("Results"), list), "trivy misconfiguration report: 'Results' missing or not a list", ReportError)
    results = report["Results"]
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
    violations: list[str] = []
    lower: list[str] = []
    for r in results:
        for m in r.get("Misconfigurations") or []:
            if m.get("Status", "FAIL") != "FAIL":
                continue
            sev = m.get("Severity", "UNKNOWN")
            start = (m.get("CauseMetadata") or {}).get("StartLine")
            desc = f"{m.get('ID')} [{sev}] {r.get('Target')}:{start} {m.get('Title')}"
            (violations if sev in fail_sev else lower).append(desc)
    lines.append(f"failed checks: {len(violations)} at gating severity {sorted(fail_sev)}, {len(lower)} at lower severity")
    lines += [f"  VIOLATION {d}" for d in violations]
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
                lines, violations = evaluate_misconfig(report, policy, expect_types=args.expect_type or [])
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
        # it exists to prove, and that is a failure of the control.
        if code == EXIT_POLICY:
            lines.append(f"negative control: the underlying verdict was {result!r}")
            result, code = RESULT_CONTROL_OK, EXIT_OK
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

    v = sub.add_parser("vuln"); common(v)
    v.add_argument("--kind", choices=sorted(ARTIFACT_TYPES), required=True)
    v.add_argument("--artifact", required=True, help="expected ArtifactName (image reference or scan root)")
    v.add_argument("--expect-target", action="append", help="filesystem: a lock target that must be present and populated")
    v.add_argument("--min-lang-packages", type=int, default=1, help="image: minimum language packages")

    m = sub.add_parser("misconfig"); common(m)
    m.add_argument("--expect-type", action="append", help="a configuration type that must appear (dockerfile, terraform)")

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
