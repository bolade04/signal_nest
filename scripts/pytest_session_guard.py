#!/usr/bin/env python3
"""Authoritative pytest SESSION guard for mandatory assurance controls (Gate 4N-I28Y).

THE DEFECT THIS CLOSES. Gate 4N-I28X proved, from a proven-green baseline on the exact final
I28W tree, that the entire assertion-control system could be removed from the graded run without
any control failing. Three one-line mutations each restored the historical dead-branch defect with
a completely green suite:

  * `collect_ignore = ["test_i28w_assertion_reachability.py",
                       "test_i28u_assertion_self_protection.py"]` in tests/conftest.py
    -> 2902 passed, 0 failed, 84 skipped;
  * module-level `pytestmark = pytest.mark.skip(...)` in both files -> 2902 passed, 0 failed;
  * `--deselect <node id>` on the graded command -> zero problems, because the contract for
    `policy_tests` required only `must_invoke: [PYTEST, tests/]`.

WHY THE OLD PIN DID NOT CLOSE IT. `test_every_mandatory_test_is_collected` did the right thing in
one respect — it asked pytest for real collected node ids rather than trusting a test to claim it
ran. But it lived INSIDE one of the two files it protected, and it interrogated a FRESH,
UNFILTERED subprocess instead of the session that was actually grading the package. So a
collected-but-skipped test satisfied it, and a de-collected pin never ran at all.

WHAT THIS DOES INSTEAD. This is a pytest plugin. It runs inside the authoritative graded process
and observes that process: the real command line, the real configuration, the real collected node
ids, the real collect/setup/call/teardown reports, the real deselections, the real skips and
xfails. At session finish it requires every node in the AUTHORED registry to have been collected,
not deselected, not skipped, not xfailed, entered in its required phase, and completed with its
contracted outcome. Anything else sets a non-zero exit status.

WHY THE REGISTRY IS AUTHORED. Deriving the mandatory set from whatever pytest happens to collect
is the self-authored oracle Gate 4N-I28U rejected for assertions: it would ratify whatever the
session currently runs, including a session that runs nothing. The requirement comes from
`tests/fixtures/mandatory-pytest-nodes.json`, an authored file, pinned by hash.

FAIL CLOSED. A missing registry, an unparseable registry, a node that resolves to no test, a
duplicated node, an unapproved configuration source, a selection-altering option, or an
unresolved outcome is a FAILURE, never a skip. Absence of evidence that a control ran is never
read as evidence that it ran.

TRUST BOUNDARY. This module is not inside either protected assertion-control test module, so
deleting, ignoring or skipping those modules does not remove it. Its own loading is protected
three ways: the graded command contract requires the `-p` flag; the configuration contract pins
the files that could unload it; and `tests/test_i28y_mandatory_session.py` fails when the plugin
is absent from the running session. What remains trusted is the authored registry itself and a
party able to change the graded command, the configuration baseline and the registry coherently
in one reviewed change — the same honestly stated limit as the assertion layers.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = REPO_ROOT / "tests" / "fixtures" / "mandatory-pytest-nodes.json"
MARKER_ENV = "SIGNALNEST_SESSION_MARKER"

# The self-protection tests need to drive this guard over synthetic sessions, so the registry
# location is overridable. That override is itself a bypass vector — point it at a registry with
# no nodes and every session passes vacuously — so two things close it: load_registry REFUSES an
# empty node list, and the authoritative session asserts (in a mandatory node of its own) that the
# registry actually in force is the repository one. An override is evidence of a sandbox, never a
# way to lower the bar for the graded run.
REGISTRY_ENV = "SIGNALNEST_MANDATORY_NODES"


def registry_path() -> Path:
    """The registry actually consumed, delegated to the authority model.

    GATE 4N-I28AI, closing ADV-I28AH-01. This used to return whatever SIGNALNEST_MANDATORY_NODES
    named. The executed-state contract bound the REGISTRY constant while the guard consumed THIS
    value, so an in-tree redirect cut the enforced mandatory set from twelve nodes to one with
    every provenance layer reporting clean and the pinned-baseline test passing against the file
    that was not read. The override is now prohibited outright: an in-tree path is not
    authorization, and the effective registry has exactly one legal location.
    """
    import registry_authority
    return registry_authority.effective_registry()

PLUGIN_NAME = "signalnest_mandatory_session_guard"
GUARD_VERSION = "4N-I28AB.1"

# Options that change WHICH tests run. Their presence is not automatically fatal — the final
# authority is the resulting collected-node set — but an option that removes a mandatory node
# is reported with the option that did it, so the failure names its cause.
SELECTION_OPTIONS = (
    "deselect", "keyword", "markexpr", "ignore", "ignore_glob", "confcutdir", "collectonly",
    "pyargs", "lf", "ff", "last_failed", "failed_first", "stepwise",
)


class GuardError(RuntimeError):
    """Fail closed. A session this guard cannot decide is never treated as satisfactory."""


def load_registry(path: Path | None = None) -> dict:
    """Parse the registry.

    GATE 4N-I28AI: when no explicit path is given, the document is parsed from the SAME bytes the
    authority model hashed, so there is no window in which one file is hashed and another parsed.
    """
    if path is None:
        # The authority model resolves and hashes; this function still applies EVERY structural
        # check below to the document it returns. Delegating resolution must not delegate away
        # validation — Gate 4N-I28AI kept the field, emptiness and duplicate checks intact and
        # simply moved the bytes they run on to the ones that were hashed.
        import registry_authority
        try:
            state = registry_authority.authoritative()
        except registry_authority.RegistryAuthorityError as exc:
            raise GuardError(f"{exc}") from None
        return _validate_registry_document(state["doc"], state["path"])
    p = path
    if not p.is_file():
        raise GuardError(
            f"the mandatory pytest node registry is missing at {p}. It is the INDEPENDENT "
            "statement of which assurance controls must actually run; without it this guard "
            "would have to accept whatever the session happened to collect, which is the "
            "self-authored oracle it exists to prevent.")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GuardError(f"the mandatory pytest node registry does not parse: {exc}") from exc
    return _validate_registry_document(doc, p)


def _validate_registry_document(doc: dict, p) -> dict:
    """Structural validation, applied identically however the document was obtained."""
    nodes = doc.get("mandatory_nodes")
    if not isinstance(nodes, list) or not nodes:
        raise GuardError("the mandatory pytest node registry declares no mandatory nodes; an "
                         "empty requirement would pass vacuously")
    seen = set()
    for n in nodes:
        for field in ("node_id", "protected_invariant", "owning_layer", "required_phase",
                      "acceptable_outcome", "prohibited_outcomes", "why_mandatory",
                      "proving_bypass_mutation", "category"):
            if field not in n:
                raise GuardError(f"{n.get('node_id', '<unnamed>')}: registry entry is missing "
                                 f"the required field {field!r}")
        if n["node_id"] in seen:
            raise GuardError(f"{n['node_id']}: duplicate mandatory node id")
        seen.add(n["node_id"])
    return doc


def registry_sha256(path: Path | None = None) -> str:
    return hashlib.sha256((path or registry_path()).read_bytes()).hexdigest()


class MandatorySessionGuard:
    """Observes the authoritative session and refuses to let it pass silently."""

    def __init__(self, config):
        self.config = config
        self.problems: list[str] = []
        self.registry = None
        self.nodes: dict[str, dict] = {}
        self.collected: set[str] = set()
        self.deselected: set[str] = set()
        self.collect_errors: list[str] = []
        self.phases: dict[str, dict] = {}
        self.enforced = True
        # GATE 4N-I28AB: genuine lifecycle-hook execution, recorded as it happens. An impersonator
        # can copy every public attribute; it cannot manufacture this log, because entries are only
        # ever appended from inside the genuine hooks below.
        self.hook_log: dict[str, int] = {}
        self.hook_sequence: list[str] = []
        self.registry_path = registry_path()
        try:
            self.registry = load_registry()
            self.nodes = {n["node_id"]: n for n in self.registry["mandatory_nodes"]}
        except GuardError as exc:
            self.problems.append(str(exc))

    # ---------------------------------------------------------------- observation
    def _record_hook(self, name: str) -> None:
        self.hook_log[name] = self.hook_log.get(name, 0) + 1
        if name not in self.hook_sequence:
            self.hook_sequence.append(name)

    def pytest_collectreport(self, report):
        self._record_hook("pytest_collectreport")
        if report.failed:
            self.collect_errors.append(str(report.nodeid or "<root>"))

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        """trylast so the observed set is the FINAL one, after every other hook has modified it."""
        self._record_hook("pytest_collection_modifyitems")
        for item in items:
            self.collected.add(item.nodeid)

    def pytest_deselected(self, items):
        self._record_hook("pytest_deselected")
        for item in items:
            self.deselected.add(item.nodeid)

    def pytest_runtest_logreport(self, report):
        self._record_hook("pytest_runtest_logreport")
        self._record_hook(f"phase:{report.when}")
        if report.nodeid not in self.nodes:
            return
        rec = self.phases.setdefault(report.nodeid, {})
        rec[report.when] = {
            "outcome": report.outcome,
            "skipped": bool(report.skipped),
            "xfail": bool(getattr(report, "wasxfail", None) is not None),
            "longrepr": str(report.longrepr)[:200] if report.failed else None,
        }
        if report.when == "call":
            rec["call_entered"] = True

    # ---------------------------------------------------------------- adjudication
    def _selection_options(self) -> dict:
        opts = {}
        for name in SELECTION_OPTIONS:
            value = getattr(self.config.option, name, None)
            if value not in (None, False, (), [], ""):
                opts[name] = value if isinstance(value, (str, int, bool)) else list(value)
        return opts

    def evaluate(self) -> dict:
        problems = list(self.problems)
        selection = self._selection_options()

        if self.collect_errors:
            problems.append(
                f"collection FAILED for {sorted(self.collect_errors)[:5]}; a session that could "
                "not collect its tests has not run them")

        outcomes = {}
        for node_id, spec in sorted(self.nodes.items()):
            rec = self.phases.get(node_id, {})
            state = {
                "collected": node_id in self.collected,
                "deselected": node_id in self.deselected,
                "setup_entered": "setup" in rec,
                "call_entered": bool(rec.get("call_entered")),
                "call_outcome": (rec.get("call") or {}).get("outcome"),
                "teardown_outcome": (rec.get("teardown") or {}).get("outcome"),
                "skipped": any(p.get("skipped") for k, p in rec.items()
                               if isinstance(p, dict)),
                "xfail": any(p.get("xfail") for k, p in rec.items() if isinstance(p, dict)),
                "skip_reason": None,
            }
            for phase in ("setup", "call", "teardown"):
                p = rec.get(phase)
                if isinstance(p, dict) and p.get("skipped"):
                    state["skip_reason"] = f"{phase} phase reported skipped"
                    break
            outcomes[node_id] = state

            if not state["collected"]:
                problems.append(
                    f"{node_id}: NOT COLLECTED in the authoritative session. Protected "
                    f"invariant: {spec['protected_invariant']}. A control that is not collected "
                    f"cannot protect anything. (selection options in effect: "
                    f"{selection or 'none'})")
                continue
            if state["deselected"]:
                problems.append(
                    f"{node_id}: DESELECTED in the authoritative session (options: "
                    f"{selection or 'none'}). Protected invariant: "
                    f"{spec['protected_invariant']}.")
                continue
            if state["skipped"]:
                problems.append(
                    f"{node_id}: SKIPPED ({state['skip_reason']}). "
                    f"{spec.get('skip_policy', 'MANDATORY_TEST_SKIP_FORBIDDEN')}. Protected "
                    f"invariant: {spec['protected_invariant']}.")
                continue
            if state["xfail"]:
                problems.append(
                    f"{node_id}: XFAILED or XPASSED under an allowance that hides failure. "
                    f"Protected invariant: {spec['protected_invariant']}.")
                continue
            if spec["required_phase"] == "call" and not state["call_entered"]:
                problems.append(
                    f"{node_id}: never entered its required '{spec['required_phase']}' phase, so "
                    f"its assertions did not execute. Protected invariant: "
                    f"{spec['protected_invariant']}.")
                continue
            if state["call_outcome"] != spec["acceptable_outcome"]:
                problems.append(
                    f"{node_id}: completed with outcome {state['call_outcome']!r}, not the "
                    f"contracted {spec['acceptable_outcome']!r}. Protected invariant: "
                    f"{spec['protected_invariant']}.")
                continue
            if state["call_outcome"] in spec["prohibited_outcomes"]:
                problems.append(f"{node_id}: outcome {state['call_outcome']!r} is prohibited")

        # duplicate representation (parametrisation must be explicitly contracted)
        for node_id, spec in sorted(self.nodes.items()):
            matches = [c for c in self.collected
                       if c == node_id or c.startswith(node_id + "[")]
            if len(matches) > 1 and not spec.get("parametrised_allowed"):
                problems.append(
                    f"{node_id}: represented {len(matches)} times in the session but "
                    "parametrisation is not contracted for it")

        return {
            "plugin": PLUGIN_NAME,
            "registry_path": str(self.registry_path),
            "registry_is_the_repository_registry": self.registry_path == REGISTRY,
            "registry_sha256": (registry_sha256(self.registry_path)
                                if self.registry_path.is_file() else None),
            "mandatory_nodes": len(self.nodes),
            "collected_nodes": len(self.collected),
            "selection_options": selection,
            "collect_errors": sorted(self.collect_errors),
            "outcomes": outcomes,
            "problems": problems,
            "clean": not problems,
        }

    # ---------------------------------------------------------------- session outcome
    def pytest_sessionfinish(self, session, exitstatus):
        self._record_hook("pytest_sessionfinish")
        result = self.evaluate()
        result["session"] = session_identity(self.config)
        # GATE 4N-I28AB: authenticated session evidence. Every field below is produced by genuine
        # hook execution in THIS process; the token binds them to the implementation, registry and
        # tree that produced them.
        prov = implementation_provenance()
        result["implementation_provenance"] = prov
        result["hook_log"] = dict(self.hook_log)
        result["hook_sequence"] = list(self.hook_sequence)
        result["genuine_object_id"] = id(self)
        result["genuine_type"] = f"{type(self).__module__}.{type(self).__qualname__}"
        result["registered_object_is_self"] = (
            self.config.pluginmanager.get_plugin(PLUGIN_NAME) is self)
        result["provenance_token"] = provenance_token(
            prov, result.get("registry_sha256") or "", result["session"].get("rootdir"))
        result["collected_node_set_sha256"] = _hash_set(self.collected)
        result["completed_node_set_sha256"] = _hash_set(
            {n for n, s in result["outcomes"].items() if s["call_outcome"] == "passed"})
        result["outcome_map_sha256"] = hashlib.sha256(
            json.dumps(result["outcomes"], sort_keys=True).encode()).hexdigest()

        marker = os.environ.get(MARKER_ENV)
        if marker:
            try:
                Path(marker).write_text(json.dumps(result, indent=1, sort_keys=True))
            except OSError:
                pass

        writer = getattr(self.config, "_signalnest_guard_result", None)
        self.config._signalnest_guard_result = result  # readable by the self-protection tests

        if not result["clean"]:
            reporter = self.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                reporter.write_sep("=", "MANDATORY SESSION GUARD: FAILED", red=True)
                for p in result["problems"]:
                    reporter.write_line(f"  {p}")
            else:                                  # never stay silent
                print("MANDATORY SESSION GUARD: FAILED", file=sys.stderr)
                for p in result["problems"]:
                    print(f"  {p}", file=sys.stderr)
            if exitstatus == 0:
                session.exitstatus = 3
            self.config._signalnest_guard_failed = True


def _hash_set(values) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode()).hexdigest()


def session_identity(config) -> dict:
    """Everything needed to prove WHICH session was observed."""
    inifile = getattr(config, "inifile", None) or getattr(config, "inipath", None)
    return {
        "pid": os.getpid(),
        "argv": list(sys.argv),
        "invocation_dir": str(getattr(config, "invocation_params", None)
                              and config.invocation_params.dir or ""),
        "invocation_args": list(getattr(config, "invocation_params", None)
                                and config.invocation_params.args or []),
        "rootdir": str(config.rootpath),
        "inifile": str(inifile) if inifile else None,
        "testpaths": list(getattr(config.option, "file_or_dir", []) or []),
        "plugins": sorted(name for name, _ in config.pluginmanager.list_name_plugin()
                          if isinstance(name, str)),
        "guard_loaded": config.pluginmanager.hasplugin(PLUGIN_NAME),
    }


# --------------------------------------------------------------------------- #
# GATE 4N-I28AB: identity, not name
# --------------------------------------------------------------------------- #
#
# THE DEFECT THIS CLOSES. Gate 4N-I28AA reviewer finding ADV-I28AA-01. This function used to begin
#
#     if config.pluginmanager.hasplugin(PLUGIN_NAME):
#         return
#
# which established plugin identity by NAME ALONE and then declined protection SILENTLY. Because
# tests/conftest.py is registered after the `-p pytest_session_guard` plugin, pluggy's LIFO hook
# ordering runs the conftest's pytest_configure FIRST, so a four-line decoy could claim the name and
# the genuine guard would return without ever registering: no collection observation, no report
# observation, no adjudication, no exit-status change. A faithful decoy produced 2943 passed / 0
# failed with 8 of 10 mandatory nodes absent, and a disarm-only variant produced a suite result
# byte-identical to the clean baseline.
#
# WHAT REPLACES IT. Three things, none of which a same-named object can satisfy:
#
#   1. A name collision is a hard failure, never a silent return. If the name is already taken by an
#      object that is not a genuine instance produced by THIS module, the session is failed.
#   2. Identity is checked against pinned implementation PROVENANCE — this module's resolved path,
#      its source hash, its spec origin and its loader class — not against a name, a version
#      attribute or a duck-typed shape.
#   3. The guard records genuine lifecycle-hook execution as it happens, so an object that merely
#      exposes the right attributes cannot pass: it has no hook log, and a hook log cannot be
#      produced without the genuine hooks running.
#
# WHAT IS HONESTLY NOT CLAIMED. The provenance digest is computed from files an attacker with repo
# write access can also read; it is not a secret and this module does not pretend otherwise. Its
# purpose is to bind the running object to a specific implementation state that the pinned baseline
# and the anti-circularity control independently attest, so that a substitution has to change
# several separately reviewed files rather than one conftest.


class GuardIdentityError(RuntimeError):
    """Fail closed. An unverifiable guard identity is never treated as satisfactory."""


def implementation_provenance() -> dict:
    """Where this module actually came from, computed at import time from the filesystem."""
    src = Path(__file__).resolve()
    spec = globals().get("__spec__")
    return {
        "canonical_module": "pytest_session_guard",
        "resolved_path": str(src),
        "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "spec_origin": getattr(spec, "origin", None),
        "loader_class": type(getattr(spec, "loader", None)).__name__ if spec else None,
        "guard_version": GUARD_VERSION,
        "class_qualname": MandatorySessionGuard.__qualname__,
    }


def provenance_token(prov: dict, registry_sha: str, tree: str | None = None) -> str:
    """A bounded integrity binding over independently pinned implementation state.

    NOT a secret-keyed MAC — nothing here is hidden from a party who can read the repository. It
    binds an emitted session record to the exact implementation, registry and tree that produced
    it, so a record from another process, tree, registry or implementation cannot be presented as
    this one's.
    """
    payload = json.dumps({"provenance": prov, "registry_sha256": registry_sha, "tree": tree},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def is_genuine(obj) -> bool:
    """Exact type identity produced by THIS module object. A subclass is not the genuine guard.

    `type(obj) is MandatorySessionGuard` is checked against the class object held by this module,
    so an identically-named class defined elsewhere, a proxy, or a subclass that overrides hooks
    all fail. Attribute shape is deliberately not consulted.
    """
    return type(obj) is MandatorySessionGuard


def pytest_configure(config):
    """Registered by `-p pytest_session_guard`, or by an approved conftest.

    A pre-claimed name is a hard failure. There is no silent early return.
    """
    existing = config.pluginmanager.get_plugin(PLUGIN_NAME)
    if existing is not None:
        if not is_genuine(existing):
            raise GuardIdentityError(
                f"the plugin name {PLUGIN_NAME!r} is already registered to "
                f"{type(existing).__module__}.{type(existing).__name__}, which is not a genuine "
                f"MandatorySessionGuard produced by {Path(__file__).resolve()}. This is the "
                "Gate 4N-I28AA finding ADV-I28AA-01: a same-named object claiming the guard's name "
                "before it registers. Refusing to run rather than declining protection silently.")
        return                                          # genuine guard already registered
    guard = MandatorySessionGuard(config)
    config.pluginmanager.register(guard, PLUGIN_NAME)
    config._signalnest_guard = guard
