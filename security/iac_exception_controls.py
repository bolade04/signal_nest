#!/usr/bin/env python3
"""Negative controls for the IaC exception mechanism (P6-CI-6, operator order 2026-10-18).

Each CASE copies the checked-out Terraform tree (every .tf/.tf.json/.tofu/.tofu.json file under the
exceptions file's tree_scope) and the tracked security/misconfig-exceptions.json into its own
directory, applies ONE mutation that must defeat an exception, and records the violation token the
gate must raise for it. CI then scans every case directory with the real Trivy in ONE config scan and
runs the real gate (security/gate.py, the same CLI the IaC gate step uses) once per case:

    python3 security/iac_exception_controls.py build <cases-root>
    python3 security/iac_exception_controls.py check <trivy-report> <cases-root> [<reports-dir>]

A mutation case passes only when the gate reports a policy violation AND a violation line carries the
case's token (gate --expect-fail --expect-violation), so an unrelated violation cannot make a control
pass. The unmutated BASELINE copy must PASS (gate --control-baseline): it proves that copying the tree
is not itself what fails the cases. Every mutation is an exact-text edit whose anchor must occur exactly
once; a drifted anchor stops `build` (exit 2) rather than silently testing nothing. The case list is
pinned against the completeness step of .github/workflows/ci.yml by security/test_iac_exception_controls.py.

Stdlib only; no network; nothing here runs a scanner.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
EXCEPTIONS = "security/misconfig-exceptions.json"
SUMMARY_PREFIX = "control-iac-exc-"

ECS = "infra/aws/modules/ecs/main.tf"
ALB = "infra/aws/modules/alb/main.tf"
ROOT_MAIN = "infra/aws/main.tf"

HTTP_LISTENER = '''
resource "aws_lb_listener" "http_probe" {
  count             = var.enabled ? 1 : 0
  load_balancer_arn = aws_lb.this[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }
}
'''
ROOT_HTTP_LISTENER = '''
# Negative-control mutation: a listener declared OUTSIDE the alb module, attached through the
# module's exported alb_arn (the bypass found by review round 3 of the disposition packet).
resource "aws_lb_listener" "http_probe" {
  load_balancer_arn = module.alb.alb_arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      status_code  = "200"
    }
  }
}
'''
ALIAS_LISTENER = '''
# Negative-control mutation: the provider's legacy alias resource type.
resource "aws_alb_listener" "http_probe" {
  load_balancer_arn = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/probe/0123456789abcdef"
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      status_code  = "200"
    }
  }
}
'''
EGRESS_BLOCK_TAIL = '  cidr_ipv4         = "0.0.0.0/0"\n\n  tags = {\n    Name = "${var.name_prefix}-${each.key}-https-egress"'


def _stale_entry(doc: dict) -> dict:
    probe = dict(doc["exceptions"][0], id="Z9-stale-probe", resource="aws_vpc_security_group_egress_rule.does_not_exist",
                 expected_instances=1, resource_conditions=[], tree_conditions=[])
    return dict(doc, exceptions=doc["exceptions"] + [probe])


def _expired_entry(doc: dict) -> dict:
    entries = [dict(e, expires="2020-01-01") if e["id"].startswith("A1-") else e for e in doc["exceptions"]]
    return dict(doc, exceptions=entries)


# name -> (what it proves, expected violation token or None for the baseline, edits, exceptions transform)
# An edit is ("replace", file, old, new) | ("append", file, text) | ("create", file, text) | ("copy_mutated", src, dst, old, new)
# | ("symlink", link, target) — target is written verbatim as the link's relative target.
CASES: dict = {
    "baseline": ("the unmutated copy passes (copying is not what fails the cases)", None, [], None),
    "egress-port-range": ("A1 condition: a widened port range on the excepted egress rule", "EXCEPTION_CONDITION_FAILED",
                          [("replace", ECS, '  to_port           = 443\n' + EGRESS_BLOCK_TAIL.split("\n")[0], '  to_port           = 444\n' + EGRESS_BLOCK_TAIL.split("\n")[0])], None),
    "egress-ipv6": ("A1 condition: the excepted rule moved to IPv6 (::/0)", "EXCEPTION_CONDITION_FAILED",
                    [("replace", ECS, EGRESS_BLOCK_TAIL, EGRESS_BLOCK_TAIL.replace('cidr_ipv4         = "0.0.0.0/0"', 'cidr_ipv6         = "::/0"'))], None),
    "egress-fourth-workload": ("A1 count: a fourth workload makes the matched count differ from expected_instances", "EXCEPTION_COUNT_MISMATCH",
                               [("replace", ECS, 'workloads    = toset(["api", "worker", "migration"])', 'workloads    = toset(["api", "worker", "migration", "batch"])')], None),
    "egress-override-file": ("A1 integrity: an override file widening the rule (Trivy still reports the original block)", "EXCEPTION_MODULE_OVERRIDE",
                             [("create", "infra/aws/modules/ecs/egress_override.tf",
                               'resource "aws_vpc_security_group_egress_rule" "task_https" {\n  to_port = 65535\n}\n')], None),
    "egress-tofu-shadow": ("A1 integrity: a .tofu file that OpenTofu would load instead of main.tf", "EXCEPTION_MODULE_OVERRIDE",
                           [("copy_mutated", ECS, "infra/aws/modules/ecs/main.tofu", '  to_port           = 443\n' + EGRESS_BLOCK_TAIL.split("\n")[0], '  to_port           = 65535\n' + EGRESS_BLOCK_TAIL.split("\n")[0])], None),
    "entry-expired": ("an entry past its expiry date", "EXCEPTION_EXPIRED", [], _expired_entry),
    "entry-stale": ("an entry that matches no finding", "EXCEPTION_STALE", [], _stale_entry),
    "listener-http-module-file": ("A2 tree condition: an HTTP listener in the alb module file", "EXCEPTION_CONDITION_FAILED",
                                  [("append", ALB, HTTP_LISTENER)], None),
    "listener-http-new-module-file": ("A2 tree condition: an HTTP listener in a NEW file of the alb module", "EXCEPTION_CONDITION_FAILED",
                                      [("create", "infra/aws/modules/alb/listener_http.tf", HTTP_LISTENER)], None),
    "listener-http-root-module": ("A2 tree condition: an HTTP listener in the ROOT module via module.alb.alb_arn", "EXCEPTION_CONDITION_FAILED",
                                  [("create", "infra/aws/listener_http_probe.tf", ROOT_HTTP_LISTENER)], None),
    "listener-alias-type": ("A2 tree condition: an HTTP listener of the alias type aws_alb_listener in another module", "EXCEPTION_CONDITION_FAILED",
                            [("create", "infra/aws/modules/edge/listener_alias_probe.tf", ALIAS_LISTENER)], None),
    "listener-nonliteral": ("A2 tree condition: the listener protocol set by a variable fails closed", "EXCEPTION_CONDITION_NONLITERAL",
                            [("replace", ALB, '  protocol          = "HTTPS"\n  ssl_policy', '  protocol          = var.listener_protocol\n  ssl_policy'),
                             ("append", "infra/aws/modules/alb/variables.tf", '\nvariable "listener_protocol" {\n  type    = string\n  default = "HTTPS"\n}\n')], None),
    "tree-nonlocal-module": ("A2 tree integrity: a module with a non-local source (the review-round-4 residual)", "EXCEPTION_TREE_OPAQUE",
                             [("append", ROOT_MAIN, '\nmodule "remote_probe" {\n  source = "git::https://example.invalid/signalnest-negative-control.git"\n}\n')], None),
    "tree-escaping-local-module": ("A2 tree integrity: a LOCAL module source that escapes the scope (../) declaring an HTTP listener on module.alb.alb_arn (review FREEZE-1, adversarial BLOCKER-1)", "EXCEPTION_TREE_OPAQUE",
                                   [("create", "infra/outside-scope-probe/main.tf", 'variable "alb_arn" {\n  type = string\n}\n' + ROOT_HTTP_LISTENER.replace("module.alb.alb_arn", "var.alb_arn")),
                                    ("append", ROOT_MAIN, '\nmodule "outside_scope_probe" {\n  source  = "../outside-scope-probe"\n  alb_arn = module.alb.alb_arn\n}\n')], None),
    "tree-symlink-dotdot-escape": ("A2 tree integrity: a symlink followed by .. (\"./link/../../modules\") — Path.resolve() follows the link first, OpenTofu cleans the path lexically and loads a directory outside the scope (review FREEZE-2, permissions lane R2-1)", "EXCEPTION_TREE_OPAQUE",
                                   [("symlink", "infra/aws/link", "modules/alb"),
                                    ("create", "infra/modules/main.tf", 'variable "alb_arn" {\n  type = string\n}\n' + ROOT_HTTP_LISTENER.replace("module.alb.alb_arn", "var.alb_arn")),
                                    ("append", ROOT_MAIN, '\nmodule "listener_extra" {\n  source  = "./link/../../modules"\n  alb_arn = module.alb.alb_arn\n}\n')], None),
    "tree-tofu-file": ("A2 tree integrity: an HTTP listener in a .tofu file", "EXCEPTION_TREE_OPAQUE",
                       [("create", "infra/aws/modules/alb/listener_http.tofu", HTTP_LISTENER)], None),
    "tree-provisioner": ("A2 tree integrity: a provisioner, which could create a listener out of band", "EXCEPTION_TREE_OPAQUE",
                         [("create", "infra/aws/provisioner_probe.tf", 'resource "terraform_data" "provisioner_probe" {\n  provisioner "local-exec" {\n    command = "true"\n  }\n}\n')], None),
    "alb-log-bucket-not-aes256": ("C1 condition: the ALB log bucket no longer SSE-S3", "EXCEPTION_CONDITION_FAILED",
                                  [("replace", ALB, 'sse_algorithm = "AES256"', 'sse_algorithm = "aws:kms"')], None),
}


class ControlError(Exception):
    pass


def _replace_once(text: str, old: str, new: str, where: str) -> str:
    n = text.count(old)
    if n != 1:
        raise ControlError(f"{where}: the mutation anchor occurs {n} times, expected exactly 1 — the control would test nothing; update the case")
    return text.replace(old, new)


def build(cases_root: pathlib.Path, repo: pathlib.Path = REPO) -> list[str]:
    if cases_root.exists() and any(cases_root.iterdir()):
        raise ControlError(f"{cases_root} exists and is not empty; the cases must be built fresh")
    doc = json.loads((repo / EXCEPTIONS).read_text(encoding="utf-8"))
    scope = repo / doc["tree_scope"]
    sources = sorted(p for p in scope.rglob("*") if p.is_file() and p.name.endswith((".tf", ".tf.json", ".tofu", ".tofu.json")))
    if not sources:
        raise ControlError(f"no configuration files under {scope}")
    built = []
    for name, (_, _, edits, transform) in CASES.items():
        case = cases_root / name
        for src in sources:
            dst = case / src.relative_to(repo); dst.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dst)
        exc_doc = transform(doc) if transform else doc
        (case / "security").mkdir(parents=True, exist_ok=True)
        (case / EXCEPTIONS).write_text(json.dumps(exc_doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        for edit in edits:
            kind, rel = edit[0], edit[1]
            path = case / rel
            if kind == "replace":
                path.write_text(_replace_once(path.read_text(encoding="utf-8"), edit[2], edit[3], f"{name}: {rel}"), encoding="utf-8")
            elif kind == "append":
                if not path.is_file():
                    raise ControlError(f"{name}: {rel} does not exist to append to")
                path.write_text(path.read_text(encoding="utf-8") + edit[2], encoding="utf-8")
            elif kind == "create":
                if path.exists():
                    raise ControlError(f"{name}: {rel} already exists; a created file must be new")
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text(edit[2], encoding="utf-8")
            elif kind == "symlink":
                if path.exists() or path.is_symlink():
                    raise ControlError(f"{name}: {rel} already exists")
                path.parent.mkdir(parents=True, exist_ok=True); path.symlink_to(edit[2])
            elif kind == "copy_mutated":
                dst = case / edit[2]
                if dst.exists():
                    raise ControlError(f"{name}: {edit[2]} already exists")
                dst.write_text(_replace_once((case / rel).read_text(encoding="utf-8"), edit[3], edit[4], f"{name}: {rel}"), encoding="utf-8")
            else:
                raise ControlError(f"{name}: unknown edit kind {kind!r}")
        built.append(name)
    return built


def check(report: pathlib.Path, cases_root: pathlib.Path, reports_dir: pathlib.Path) -> list[tuple[str, int]]:
    sys.path.insert(0, str(HERE))
    import gate  # noqa: E402 — the same module and CLI the IaC gate step runs
    results = []
    for name, (_, token, _, _) in CASES.items():
        case = cases_root / name
        argv = ["--reports-dir", str(reports_dir), "misconfig", str(report), "--name", SUMMARY_PREFIX + name,
                "--expect-type", "terraform", "--exceptions", str(case / EXCEPTIONS), "--source-root", str(case),
                "--report-prefix", name + "/"]
        argv += ["--control-baseline"] if token is None else ["--expect-fail", "--expect-violation", token]
        results.append((name, gate.main(argv)))
    return results


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "build":
            built = build(pathlib.Path(argv[1]))
            print(f"built {len(built)} IaC exception control cases under {argv[1]}: {', '.join(built)}")
            return 0
        if len(argv) in (3, 4) and argv[0] == "check":
            reports_dir = pathlib.Path(argv[3] if len(argv) == 4 else "security-reports")
            results = check(pathlib.Path(argv[1]), pathlib.Path(argv[2]), reports_dir)
            bad = [(n, c) for n, c in results if c != 0]
            for n, c in results:
                print(f"{'ok ' if c == 0 else 'BAD'} {SUMMARY_PREFIX}{n}: gate exit {c} — {CASES[n][0]}")
            print(f"RESULT: {'every IaC exception control behaved as intended' if not bad else f'{len(bad)} control(s) did NOT behave as intended'} ({len(results)} cases)")
            return 0 if not bad else 6
    except ControlError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stderr.write(__doc__ + "\n")
    return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
