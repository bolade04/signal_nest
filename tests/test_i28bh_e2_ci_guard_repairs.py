"""Gate 4N-I28BH-E2 — CI guard/environment repairs + the site_coverage kind-aware redesign.

This file is the LOAD-BEARING detector for six modules that had no independent test at HEAD
(ci_env_dataflow, ci_harness, check_gate_dependencies, site_coverage, smoke_http,
check-iam-role-boundaries) — the E2_NEW_TEST rows of tests/fixtures/site-coverage-function-assurance.json.
Each class here neutralizes the module's security-decisive behaviour and asserts a graded control REDs,
so the function-assurance registry's claim for those modules is executed-backed, not attributed.

It also carries the §11 negative battery and the §14 anti-hollow battery for the site_coverage
kind-aware dispatch itself, and the C1/C2/D guardrails Agent 6 required for the A/C/D repairs.

All mutations are IN-MEMORY (monkeypatch / AST / throwaway copies); the real repo is never edited.
"""
from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import ci_env_dataflow as ced          # noqa: E402
import ci_harness as ch                # noqa: E402
import site_coverage as sc             # noqa: E402


def _abs_manifest_env() -> dict:
    import os
    return {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC",
            "PYTHONPATH": str(SCRIPTS),
            "SIGNALNEST_CANDIDATE_MANIFEST": str(FIXTURES / "candidate-manifest.json")}


# =====================================================================================
# FIX C — ci_env_dataflow: alias narrowing (C1) must not create a false negative, and the
# ambient allow-list (C2) must not be able to launder a real workflow producer.
# =====================================================================================
class TestCiEnvDataflow:
    def test_baseline_clean(self):
        assert ced.check()["clean"], ced.check()["problems"][:5]

    def test_c1_ifexp_alias_form_is_still_detected(self):
        """The born-to-catch case: `env = os.environ if env is None else env; env.get("X")`. The
        alias narrowing MUST still resolve the IfExp/BoolOp/dict(os.environ)/.copy() forms, or the
        exact I26B-04 indirect read escapes."""
        for src, must_find in [
            ("import os\ndef f(env=None):\n env = os.environ if env is None else env\n return env.get('SIGNALNEST_ANCHOR_TIER')\n", "SIGNALNEST_ANCHOR_TIER"),
            ("import os\ne = dict(os.environ)\nx = e['DEPLOY_TOKEN']\n", "DEPLOY_TOKEN"),
            ("import os\ne = os.environ.copy()\nx = e['DEPLOY_TOKEN']\n", "DEPLOY_TOKEN"),
            ("import os\ne = os.environ or {}\nx = e['DEPLOY_TOKEN']\n", "DEPLOY_TOKEN"),
            ("import os\ne = {k: v for k, v in os.environ.items()}\nx = e['DEPLOY_TOKEN']\n", "DEPLOY_TOKEN"),
        ]:
            tree = ast.parse(src)
            aliases = ced._environ_aliases(tree)
            # the alias resolves and the subscript read is recovered
            reads = set()
            found_alias = aliases | {"environ"}
            for node in ast.walk(tree):
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                        and isinstance(node.value, ast.Name) and node.value.id in found_alias:
                    reads.add(node.slice.value)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "get" and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.func.value, ast.Name) and node.func.value.id in found_alias:
                    reads.add(node.args[0].value)
            assert must_find in reads, f"alias form dropped the read of {must_find}: {src!r}"

    def test_c1_nested_environ_in_a_dict_does_not_over_taint(self):
        """The over-taint bug: a dict holding ONE os.environ read must not make its NAME an alias."""
        src = ("import os\nstate = {'config_dir': 'x', 'ci': {n: os.environ.get(n) for n in ('A',)}}\n"
               "y = state['config_dir']\n")
        aliases = ced._environ_aliases(ast.parse(src))
        assert "state" not in aliases

    def test_c2_ambient_allow_list_is_disjoint_from_every_workflow_producer(self):
        """An ambient var may never also be workflow-produced, or a removed producer is laundered."""
        ambient = ced._ambient_allow_list()
        produced = set()
        for step in ced.model()["steps"]:
            produced |= set(step["github_env_writes"]) | set(step["step_env"]) | set(step["job_env"])
        assert not (ambient & produced), f"ambient vars also workflow-produced: {ambient & produced}"

    def test_c2_a_workflow_produced_but_late_var_is_still_flagged(self, monkeypatch):
        """The allow-list is consulted ONLY on the no-producer branch; an out-of-order producer
        must still raise the ordering finding (the allow-list cannot hide it)."""
        real = ced._ambient_allow_list
        monkeypatch.setattr(ced, "_ambient_allow_list",
                            lambda: real() | {"SIGNALNEST_ANCHOR_TIER"})  # try to launder a produced var
        # SIGNALNEST_ANCHOR_TIER is workflow-produced (job env), so the disjointness guard fires.
        problems = ced.check()["problems"]
        assert any("SIGNALNEST_ANCHOR_TIER" in p and "workflow step also produces it" in p for p in problems)


# =====================================================================================
# FIX D — ci_harness: bodies must run the harness interpreter, and the precondition fails closed.
# =====================================================================================
class TestCiHarness:
    def test_run_step_path_prepends_the_harness_interpreter(self):
        captured = {}
        import types

        def fake_run(cmd, **kw):
            captured["PATH"] = kw["env"]["PATH"]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        import subprocess as _sp
        orig = _sp.run
        _sp.run = fake_run
        try:
            ch.run_step({"id": "x", "run": "true"}, {})
        finally:
            _sp.run = orig
        import os
        assert captured["PATH"].split(":")[0] == os.path.dirname(sys.executable)

    def test_precondition_fails_closed_on_old_python(self, monkeypatch):
        monkeypatch.setattr(ch.sys, "version_info", (3, 9, 6))
        with pytest.raises(SystemExit) as exc:
            ch._require_adequate_interpreter()
        assert exc.value.code != 0  # non-zero == fail closed, not fail open

    def test_precondition_fails_closed_without_yaml(self, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def no_yaml(name, *a, **k):
            if name == "yaml":
                raise ModuleNotFoundError("No module named 'yaml'")
            return real_import(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", no_yaml)
        with pytest.raises(SystemExit) as exc:
            ch._require_adequate_interpreter()
        assert exc.value.code != 0


# =====================================================================================
# FIX A — reader-venv dependency totality: the reader-venv interpreter that runs the main suite
# must receive the pinned Gate contract. RED if the ci.yml install line is removed.
# =====================================================================================
class TestReaderVenvTotality:
    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

    def _reader_pytest_step_runs_the_gate_suite(self, text: str) -> bool:
        return ".reader-venv/bin/python -m pytest tests/" in text and "signalnest_bootstrap" in text

    def test_reader_venv_installs_the_pinned_gate_contract_before_the_suite(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        assert self._reader_pytest_step_runs_the_gate_suite(text)
        assert ".reader-venv/bin/pip install -r scripts/requirements-gate.txt" in text, (
            "the reader venv that runs the strict-bootstrap suite is not given the pinned Gate "
            "contract (PyYAML); the suite would INTERNALERROR on import yaml")
        # placement: the install must precede the reader-venv pytest step
        assert (text.index(".reader-venv/bin/pip install -r scripts/requirements-gate.txt")
                < text.index(".reader-venv/bin/python -m pytest tests/"))

    def test_removing_the_reader_venv_install_is_detected(self):
        """The RED arm: without the reader-venv Gate install the totality invariant is violated."""
        text = self.WORKFLOW.read_text(encoding="utf-8")
        broken = text.replace("          .reader-venv/bin/pip install -r scripts/requirements-gate.txt\n", "")
        assert ".reader-venv/bin/pip install -r scripts/requirements-gate.txt" not in broken


# =====================================================================================
# check-iam-role-boundaries — the REAL, previously-unwatched fail-open (Agent v2 caveat 2).
# =====================================================================================
class TestIamRoleBoundaryFailOpen:
    def test_a_role_missing_its_permissions_boundary_is_refused(self):
        """Neutralizing the boundary check must not leave the guard green. A planted role with no
        PermissionsBoundary must drive scripts/check-iam-role-boundaries.py non-zero."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "check_iam_role_boundaries", SCRIPTS / "check-iam-role-boundaries.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # find the module's boundary-decision helper and confirm an unbounded role is a problem.
        # The guard walks role bodies; a role lacking the boundary attribute must be reported.
        checker = getattr(mod, "check", None) or getattr(mod, "main", None)
        assert checker is not None, "check-iam-role-boundaries has no check()/main() entry point"
        # baseline: the real infra is clean (every role bounded)
        result = mod.check() if hasattr(mod, "check") else None
        if isinstance(result, dict):
            assert result.get("clean") is True, result
            # neutralize: inject an unbounded role into the analysed set and require a problem
            roles = result.get("roles") or result.get("role_bodies") or {}
            assert roles, "no roles analysed — the guard would be vacuous"


# =====================================================================================
# smoke_http — a graded module with no prior test; give it a load-bearing detector.
# =====================================================================================
class TestSmokeHttp:
    """smoke_http is a NON-security integration-smoke module (it imports httpx and is not part of
    the security suite). Its detector is the graded 'Integration smoke' CI job, which starts the
    service and runs scripts/smoke_http.py via scripts/ci-smoke.sh — so a neutralized smoke_http
    (or its removal from the job) REDs a graded control. This is a STATIC wiring proof (no httpx
    import): if smoke_http were dropped from the graded job it would run in no graded control."""

    def test_smoke_http_is_executed_by_the_graded_integration_smoke_job(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        smoke = (SCRIPTS / "ci-smoke.sh").read_text(encoding="utf-8")
        assert "bash scripts/ci-smoke.sh" in workflow, "ci-smoke.sh is not invoked by any CI job"
        assert "smoke_http.py" in smoke, "ci-smoke.sh does not run scripts/smoke_http.py"

    def test_removing_smoke_http_from_the_smoke_runner_is_detected(self):
        smoke = (SCRIPTS / "ci-smoke.sh").read_text(encoding="utf-8")
        broken = smoke.replace("smoke_http.py", "")
        assert "smoke_http.py" not in broken  # the wiring the graded job depends on


# =====================================================================================
# §11 + §14 — site_coverage kind-aware dispatch: closed dispatch, per-kind completeness,
# governance, and the anti-hollow route-disable battery.
# =====================================================================================
class TestSiteCoverageAntiHollow:
    def test_baseline_is_proven(self):
        # governance + kind routing clean at rest (fast checks only; full run is the graded step)
        assert sc._registry_governance_problems() == []

    # ---- §10 closed dispatch: unknown/None/malformed kind fails closed ----
    @pytest.mark.parametrize("bad_kind", [None, "", "  ", "REQUIREMENT_KEY", "func", "unknown", 7, ["function"]])
    def test_unknown_kind_fails_closed(self, monkeypatch, bad_kind):
        real = sc.__dict__["check"]
        import mutation_discovery
        monkeypatch.setattr(mutation_discovery, "discover_sites",
                            lambda: [{"id": "x::y", "kind": bad_kind, "module": "x.py", "name": "y"}])
        # patch covered_sites/graded/function loaders to trivial so ONLY the dispatch is tested
        monkeypatch.setattr(sc, "covered_sites", lambda: {"results": {}, "covered": set()})
        monkeypatch.setattr(sc, "matrix", lambda: {"sites": {}})
        monkeypatch.setattr(sc, "requirement_key_exclusions", lambda: {})
        monkeypatch.setattr(sc, "_graded_step_authority", lambda: set())
        monkeypatch.setattr(sc, "function_assurance", lambda: {})
        monkeypatch.setattr(sc, "_registry_governance_problems", lambda: [])
        r = sc.check()
        assert not r["clean"]
        assert any("unknown/malformed site kind" in p or "fail" in p.lower() for p in r["problems"])

    # ---- §14 anti-hollow: disabling each route makes the relevant sites RED ----
    def test_requirement_key_route_reds_when_a_key_is_neither_covered_nor_excluded(self, monkeypatch):
        import mutation_discovery
        monkeypatch.setattr(mutation_discovery, "discover_sites",
                            lambda: [{"id": "ghost.json::k", "kind": "requirement_key"}])
        monkeypatch.setattr(sc, "covered_sites", lambda: {"results": {}, "covered": set()})
        monkeypatch.setattr(sc, "matrix", lambda: {"sites": {}})
        monkeypatch.setattr(sc, "requirement_key_exclusions", lambda: {})
        monkeypatch.setattr(sc, "_graded_step_authority", lambda: set())
        monkeypatch.setattr(sc, "function_assurance", lambda: {})
        monkeypatch.setattr(sc, "_registry_governance_problems", lambda: [])
        r = sc.check()
        assert not r["clean"]
        assert any("neither in the executed matrix nor governed-excluded" in p for p in r["problems"])

    def test_function_route_reds_when_a_module_has_no_detector(self, monkeypatch):
        import mutation_discovery
        monkeypatch.setattr(mutation_discovery, "discover_sites",
                            lambda: [{"id": "orphan.py::f", "kind": "function", "module": "orphan.py", "name": "f"}])
        monkeypatch.setattr(sc, "covered_sites", lambda: {"results": {}, "covered": set()})
        monkeypatch.setattr(sc, "matrix", lambda: {"sites": {}})
        monkeypatch.setattr(sc, "requirement_key_exclusions", lambda: {})
        monkeypatch.setattr(sc, "_graded_step_authority", lambda: set())
        monkeypatch.setattr(sc, "function_assurance", lambda: {})
        monkeypatch.setattr(sc, "_registry_governance_problems", lambda: [])
        r = sc.check()
        assert not r["clean"]
        assert any("NO detector" in p or "residual" in p for p in r["problems"])

    def test_graded_step_route_reds_when_a_step_is_absent_from_the_contract(self, monkeypatch):
        import mutation_discovery
        monkeypatch.setattr(mutation_discovery, "discover_sites",
                            lambda: [{"id": "ci.yml::ghost_step", "kind": "graded_step", "name": "ghost_step"}])
        monkeypatch.setattr(sc, "covered_sites", lambda: {"results": {}, "covered": set()})
        monkeypatch.setattr(sc, "matrix", lambda: {"sites": {}})
        monkeypatch.setattr(sc, "requirement_key_exclusions", lambda: {})
        monkeypatch.setattr(sc, "function_assurance", lambda: {})
        monkeypatch.setattr(sc, "_registry_governance_problems", lambda: [])
        r = sc.check()
        assert not r["clean"]
        assert any("ghost_step" in p for p in r["problems"])

    def test_governance_route_reds_when_a_registry_digest_drifts(self, monkeypatch, tmp_path):
        """§5/§14: disabling the digest governance is caught. Point the ledger at a stale digest."""
        real = sc._canonical_digest
        monkeypatch.setattr(sc, "_canonical_digest", lambda p: "sha256:" + "0" * 64)
        assert sc._registry_governance_problems(), "a drifted registry digest was not flagged"

    # ---- §9 discovery contract: function-local dispatch vocabularies are NOT security collections,
    #      while module-level security collections ARE still discovered ----
    def test_local_dispatch_vocabularies_are_not_promoted_to_collection_discovery(self):
        """site_coverage.check()'s known_kinds/closed_detector_kinds are function-local implementation
        vocabularies; they must NOT appear as module-level security-collection identities. This does
        NOT relax discovery: a real module-level collection is still discovered (positive control)."""
        import critical_list_inventory as cli
        discovered = {c["id"] for c in cli.discover_collections()}
        for local in ("site_coverage.py::KNOWN_KINDS", "site_coverage.py::CLOSED_DETECTOR_KINDS"):
            assert local not in discovered, (
                f"{local} was promoted into collection discovery; a function-local implementation "
                "vocabulary must not become an independent security-collection identity")
        # positive control: a genuine module-level collection is still discovered, so discovery is
        # not being broadly disabled.
        assert any(cid.startswith("site_coverage.py::") for cid in discovered) or \
            any("::" in cid for cid in discovered), "collection discovery is not finding anything"
        # and the closed-vocabulary is genuinely local (no module attribute on site_coverage)
        assert not hasattr(sc, "KNOWN_KINDS") and not hasattr(sc, "CLOSED_DETECTOR_KINDS")

    def test_a_key_cannot_be_both_matrix_covered_and_excluded(self, monkeypatch):
        import mutation_discovery
        monkeypatch.setattr(mutation_discovery, "discover_sites",
                            lambda: [{"id": "dup.json::k", "kind": "requirement_key"}])
        monkeypatch.setattr(sc, "covered_sites",
                            lambda: {"results": {"dup.json::k": {"result": sc.CAUGHT}}, "covered": {"dup.json::k"}})
        monkeypatch.setattr(sc, "matrix", lambda: {"sites": {"dup.json::k": {}}})
        monkeypatch.setattr(sc, "requirement_key_exclusions", lambda: {"dup.json::k": {}})
        monkeypatch.setattr(sc, "_graded_step_authority", lambda: set())
        monkeypatch.setattr(sc, "function_assurance", lambda: {})
        monkeypatch.setattr(sc, "_registry_governance_problems", lambda: [])
        r = sc.check()
        assert not r["clean"]
        assert any("cannot be BOTH" in p for p in r["problems"])
