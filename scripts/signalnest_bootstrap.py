#!/usr/bin/env python3
"""Bounded interpreter bootstrap for the protected Gate 4N modules (Gate 4N-I28AE).

THE DEFECT THIS CLOSES. Gate 4N-I28AC finding ACC-I28AC-01: `sitecustomize.py` staged into the
repository's own `scripts/` directory is auto-imported at interpreter startup by the graded
command's own PYTHONPATH, before pytest exists. Pre-seeding `sys.modules["pytest_session_guard"]`
there makes the guard and every verifier resolve the same substituted module, and every
disk-reading check — `inspect.getsource`, the provenance hash, package coherence — reports the
legitimate staged file while different code runs.

WHAT THIS DOES. Loaded as the FIRST plugin on the graded command (`-p signalnest_bootstrap`,
ahead of `-p pytest_session_guard`), it establishes a trust point before any protected module is
imported:

  1. Every protected module resident in sys.modules has its EXECUTED code verified against the
     pinned bytes. Presence alone is not the signal — pytest imports `-p` plugins before
     pytest_configure fires, so the guard is legitimately resident by then; what is refused is a
     resident module whose executing code is not the code on the pinned path.
  2. It records the interpreter-startup surface it observed — sitecustomize, usercustomize, .pth
     hooks, PYTHONPATH, cwd on sys.path, meta_path finders — as evidence, not as a verdict.
  3. It loads each protected module ITSELF from the pinned path, from bytes it reads, with a plain
     compile, and installs the result. The bytes it executed are therefore the bytes it hashed.
  4. It records an executed-code identity for what it loaded.

BOOTSTRAP SEEDING IS NOT THE ATTACK IT PREVENTS. Step 3 does put modules into sys.modules, which
is superficially the same act the attacker performs. The difference is attested: the bootstrap
reads the pinned path, hashes exactly those bytes, compiles them itself, and records the identity;
the attacker's seeding happens before any of that and is caught by step 1.

GATE 4N-I28AG ADDS TWO LAYERS. Gate 4N-I28AF proved that binding executed CODE is not sufficient:
exec'ing the real pinned bytes and then rebinding one module-level global (REGISTRY) cut the guard's
mandatory set from twelve nodes to one while executed-code provenance reported clean, and the graded
session exited 0 with eleven of twelve assurance nodes absent. So this bootstrap now also runs:

  * executed_state_provenance — binds the load-bearing STATE the protected code reads: module-level
    values, the callables it reaches, defaults and keyword defaults, and the CONTENT of every file
    those values name.
  * startup_policy — adjudicates the interpreter startup surface against an authored policy instead
    of merely recording it, which is what ADV-I28AF-02 found missing.

Both re-run at session finish, so a mutation applied after configure is caught before the run is
reported as satisfactory.

HONEST RESIDUAL. A party who pre-seeds THIS module has moved the problem up one level. That is
bounded rather than dismissed: `test_i28ae_executed_provenance.py` verifies this module's own
executing code against its pinned bytes using an independently implemented oracle, so a substituted
bootstrap whose behaviour differs has different bytecode and is caught. A substituted bootstrap
that is byte-identical cannot behave differently. What remains outside the claim is a party able to
rewrite every independently pinned source at once.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_SET = REPO_ROOT / "tests" / "fixtures" / "protected-module-set.json"
BOOTSTRAP_ATTESTATION = "_signalnest_bootstrap_attestation"


class BootstrapError(RuntimeError):
    """Fail closed. A bootstrap that cannot establish trust never reports that it did."""


def _protected_entries() -> list[dict]:
    if not PROTECTED_SET.is_file():
        raise BootstrapError(
            f"the protected-module set is missing at {PROTECTED_SET}; refusing to bootstrap "
            "without an authored statement of what must be protected")
    doc = json.loads(PROTECTED_SET.read_text(encoding="utf-8"))
    entries = doc.get("protected_modules") or []
    if not entries:
        raise BootstrapError("the protected-module set is empty; an empty protected set would "
                             "make this bootstrap vacuous")
    return entries


def startup_surface() -> dict:
    """What the interpreter had already done before this ran. Evidence, not a verdict."""
    cwd = str(Path.cwd())
    return {
        "sitecustomize_resident": "sitecustomize" in sys.modules,
        "sitecustomize_origin": getattr(sys.modules.get("sitecustomize"), "__file__", None),
        "usercustomize_resident": "usercustomize" in sys.modules,
        "usercustomize_origin": getattr(sys.modules.get("usercustomize"), "__file__", None),
        "pythonpath": os.environ.get("PYTHONPATH", ""),
        "pythonhome": os.environ.get("PYTHONHOME", ""),
        "cwd": cwd,
        "cwd_on_sys_path": cwd in sys.path or "" in sys.path,
        "meta_path": [f"{type(f).__module__}.{type(f).__name__}" for f in sys.meta_path],
        "path_hooks": [getattr(h, "__name__", type(h).__name__) for h in sys.path_hooks],
        "no_site": sys.flags.no_site == 1,
        "no_user_site": sys.flags.no_user_site == 1,
        "isolated": sys.flags.isolated == 1,
        "pth_derived_entries": [p for p in sys.path if p.endswith(".pth")],
    }


def _load_protected(entry: dict) -> dict:
    """Read, hash, compile and execute one protected module from its pinned path."""
    name, rel = entry["module"], entry["relative_path"]
    path = (REPO_ROOT / rel).resolve()
    if not path.is_file():
        raise BootstrapError(f"{name}: pinned path {rel} is not in the tree")
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    code = compile(source, str(path), "exec")
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(path)
    sys.modules[name] = module
    try:
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return {"module": name, "relative_path": rel, "resolved_path": str(path),
            "source_sha256": digest, "loaded_by": "signalnest_bootstrap",
            "compile_mode": "plain compile of the pinned bytes (no assertion rewriting)"}


def _exec_transfer_state() -> dict:
    """Derive exec transfers TWICE, independently, and require them to agree AND be present.

    GATE 4N-I28BB. Returned as data so `establish` binds it into the session baseline and
    `reverify` re-derives and compares it at session finish — a transfer child removed, relabelled
    or moved mid-session is then drift, not a quietly smaller inventory.

    Both modules are imported HERE rather than passed in. The first version took them as
    parameters, and `test_the_oracle_independently_agrees_on_the_reachable_name_set` caught the
    consequence: the bytecode derivation resolved `oracle.compare` to
    `LOCALCALLABLE:exec_transfer_oracle.compare` while an independent AST walk could not see that a
    parameter named `oracle` was that module, so production bound a name the oracle called
    unreachable. A function-local import makes the binding derivable BOTH ways, which is the
    property the state contract depends on.
    """
    import exec_transfer_oracle as oracle
    import shell_positions
    production, problems = [], []
    for origin, text in sorted(oracle.tracked_sources().items()):
        scanned = (shell_positions.scan_script(text, origin=origin)
                   if origin.endswith((".sh", ".bash"))
                   else shell_positions.scan(text, origin=origin))
        if not scanned.is_trustworthy():
            problems.append(f"{origin}: {scanned.status} is not a permitted trust input")
            continue
        for site in scanned.transfer_sites:
            production.append({"origin": origin, "line": site.line, "word": site.word,
                               "child": site.child, "classification": site.classification,
                               "options": list(site.options)})
    independent = oracle.derive_tracked()
    # expect_present is FALSE for the tracked tree only if the tree genuinely has no transfer; it
    # is derived, never assumed, so a tree that loses its last transfer does not silently start
    # passing a weaker check.
    comparison = oracle.compare(production, independent, expect_present=bool(independent))
    problems.extend(comparison["problems"])
    contract = shell_positions.exec_grammar_contract()
    # A positive control that does not depend on the tree: a fixture known to contain a transfer
    # must be non-empty in BOTH derivations. If this ever passes while empty, the comparison
    # itself has stopped working.
    fixture = "exec kubectl apply -f x"
    fixture_production = [{"origin": "<positive-control>", "line": t.line, "word": t.word,
                           "child": t.child, "classification": t.classification,
                           "options": list(t.options)}
                          for t in shell_positions.scan(fixture).transfer_sites]
    fixture_check = oracle.compare(fixture_production,
                                   oracle.derive(fixture, origin="<positive-control>"),
                                   expect_present=True)
    if not fixture_check["clean"]:
        problems.append("the exec positive control failed: " + "; ".join(fixture_check["problems"]))
    return {"clean": not problems, "problems": problems,
            "grammar_version": contract["version"],
            "grammar": contract,
            "production_sites": len(production), "independent_sites": len(independent),
            "agree": comparison["agree"],
            "sites": sorted(production, key=lambda s: (s["origin"], s["line"], s["word"])),
            "positive_control_clean": fixture_check["clean"]}


def establish(*, strict: bool = True) -> dict:
    """Run the bootstrap. Returns an attestation; raises BootstrapError when strict and unsafe."""
    entries = _protected_entries()
    surface = startup_surface()
    preseeded = [e["module"] for e in entries if e["module"] in sys.modules]
    attestation = {
        "bootstrap_module": __name__,
        "bootstrap_path": str(Path(__file__).resolve()),
        "bootstrap_source_sha256": hashlib.sha256(
            Path(__file__).resolve().read_bytes()).hexdigest(),
        "protected_set_sha256": hashlib.sha256(PROTECTED_SET.read_bytes()).hexdigest(),
        "startup_surface": surface,
        "preseeded_protected_modules": preseeded,
        "loaded": [],
        "problems": [],
    }
    # A protected module being RESIDENT is not by itself the attack: pytest imports `-p` plugins
    # before pytest_configure fires, so the guard and this module are legitimately resident by the
    # time this runs. Treating presence as fatal would refuse every honest session, which is how a
    # control gets disabled for being unusable. What is fatal is a resident module whose EXECUTED
    # code cannot be shown to be the pinned bytes — the actual ACC-I28AC-01 condition.
    import executed_code_provenance as _ecp
    for entry in entries:
        if entry["module"] not in sys.modules:
            attestation["loaded"].append(_load_protected(entry))

    verdict = _ecp.verify({"protected_modules": entries})
    attestation["provenance"] = {
        "clean": verdict["clean"], "problems": verdict["problems"],
        "results": [{k: r.get(k) for k in
                     ("module", "runtime_origin", "disk_code_digest", "runtime_code_digest",
                      "shared_code_objects", "mismatched", "missing_critical")}
                    for r in verdict["results"]],
    }

    # GATE 4N-I28AG. Code identity is necessary and not sufficient — see ADV-I28AF-01. Bind the
    # state that code reads, and adjudicate the startup surface rather than recording it.
    import executed_state_provenance as _esp
    import startup_policy as _sp

    # GATE 4N-I28AI. Two further layers, closing the Gate 4N-I28AH blockers: the registry the
    # guard ACTUALLY consumes is path-, content- and parsed-set-bound (ADV-I28AH-01), and every
    # required external executable is resolved against an approved path set and content-identified
    # rather than merely PATH-discovered (ADV-I28AH-02).
    import registry_authority as _ra
    import external_executable_trust as _eet

    registry = _ra.verify()
    attestation["registry_authority"] = {
        "clean": registry["clean"], "problems": registry["problems"], "record": registry["record"]}
    # GATE 4N-I28AK: the inventory is derived, not assumed (ADV-I28AJ-01). A reachable executable
    # with no classification fails the session here, before anything runs it.
    import executable_inventory as _ei
    inventory = _ei.check()
    attestation["executable_inventory"] = {
        "clean": inventory["clean"], "problems": inventory["problems"],
        "static": inventory["static"], "reconciliation": inventory["reconciliation"],
        "unreachable_policy_entries": inventory["unreachable_policy_entries"]}

    # GATE 4N-I28AR: the seventh layer, closing ADV-I28AP-03. Every preceding layer binds code, or
    # state that code READS from disk. None of them binds a value the process DERIVED and then
    # memoised — and poisoning that memo takes release roots 41 -> 0 and production sites 492 -> 0
    # with all six layers reporting clean. This one recomputes the authoritative answer from staged
    # source without consulting any cache, and refuses when the cache disagrees.
    import cache_authority as _ca
    caches = _ca.verify()
    attestation["cache_authority"] = {
        "clean": caches["clean"], "problems": caches["problems"],
        "policy_sha256": caches["policy_sha256"],
        "fresh": caches["records"].get("fresh"), "served": caches["records"].get("served"),
        "classifications": caches["records"].get("classifications")}

    # GATE 4N-I28AS: the eighth layer, closing ADV-I28AP-02. External executable trust binds a
    # FILE. npm is not a file — it is a wrapper for a JavaScript entrypoint executed by a
    # separately-resolved Node interpreter, and binding only the wrapper let an attacker script
    # inside an approved installation root be accepted with its own digest recorded as the bound
    # identity. This binds the whole chain, before npm can run.
    import npm_authority as _na
    npm = _na.verify()
    attestation["npm_authority"] = {
        "clean": npm["clean"], "problems": npm["problems"],
        "family": npm.get("family"), "authority_model": npm.get("authority_model"),
        "policy_sha256": npm["policy_sha256"],
        "canonical_npm": npm["chain"].get("canonical_npm"),
        "canonical_node": npm["chain"].get("canonical_node")}
    # The identity session-finish compares against: an npm file, symlink, Node binary or npmrc
    # replaced AFTER this point is invisible to a fresh check and visible only as a DIFFERENCE.
    attestation["npm_snapshot"] = _na.snapshot()

    # GATE 4N-I28AT: the ninth layer, closing ADV-I28AP-01. External executable trust binds the
    # docker CLI BINARY and nothing else, so which DAEMON that binary talks to was chosen by state
    # nothing looked at: DOCKER_HOST, DOCKER_CONTEXT, DOCKER_CONFIG, the TLS variables, a hostile
    # config.json and a context store were all accepted silently with every other layer clean.
    # This binds the execution BOUNDARY — the model, the call-site inventory, the steering
    # environment, the configuration, the context store and the CI assumption — before Docker runs.
    # GATE 4N-I28AV: parser completeness is session state. ADV-I28AT-01 returned a partial parse
    # as COMPLETE, so binding the command inventory alone would never have caught it — what is
    # bound is the completeness EVIDENCE.
    import shell_positions as _shp
    shell_completeness = _shp.completeness_digest()
    attestation["shell_completeness"] = shell_completeness
    if shell_completeness["untrustworthy"]:
        attestation["problems"].append(
            "shell parse completeness failed during bootstrap: "
            f"{shell_completeness['untrustworthy']} could not be parsed to a trustworthy result. "
            "A partial parse must never be treated as coverage.")
        if strict:
            raise BootstrapError(attestation["problems"][-1])

    # GATE 4N-I28BB, closing the load-bearing half of ADV-I28AX-01. The exec command-position
    # transfer model, its option table, and the reconciliation of the production parser against an
    # INDEPENDENT direct-source oracle. The independence is the point: Gate 4N-I28AV recorded a
    # Docker reconciliation difference of 0 as confirmation while both derivations shared the same
    # exec blind spot, so agreement proved nothing. Every comparison here carries a positive
    # expected-presence condition, because two empty results are also equal.
    exec_transfer = _exec_transfer_state()
    attestation["exec_transfer"] = exec_transfer
    if not exec_transfer["clean"]:
        attestation["problems"].append(
            "exec command-position transfer verification failed during bootstrap: "
            + "; ".join(exec_transfer["problems"][:4])
            + ". A transfer the two derivations disagree about, or a transfer neither found on a "
            "source known to contain one, fails closed.")
        if strict:
            raise BootstrapError(attestation["problems"][-1])

    import docker_boundary as _db
    docker = _db.verify()
    # GATE 4N-I28BF-A, closing I28BE-SESSION-01. Bind the per-site enforcement state so session
    # finish has something to compare a FRESH rederivation against. Before this, per-site
    # enforcement ran once at establishment and was never re-derived.
    attestation["docker_per_site"] = _db.per_site_state()
    if not attestation["docker_per_site"]["clean"]:
        attestation["problems"].append(
            "Docker per-site enforcement failed during bootstrap; the baseline may not establish "
            "while any load-bearing site lacks a PASS decision")
        if strict:
            raise BootstrapError(attestation["problems"][-1])
    attestation["docker_boundary"] = {
        "clean": docker["clean"], "problems": docker["problems"],
        "model": docker["model"], "assumption_version": docker["assumption_version"],
        "call_site_count": docker["call_site_count"],
        "reconciliation": docker["reconciliation"],
        "policy_sha256": docker["policy_sha256"]}
    attestation["docker_snapshot"] = _db.snapshot()

    executables = _eet.check()
    attestation["executable_trust"] = {
        "clean": executables["clean"], "problems": executables["problems"],
        "executables": executables["executables"],
        "steering_variables_fatal": executables["steering_variables_fatal"],
        "steering_variables_neutralized": executables["steering_variables_neutralized"],
        "path_env_sha256": executables["path_env_sha256"]}
    # The snapshot is what session-finish compares against, so a binary or PATH swapped mid-run is
    # caught even though no digest is pinned in the repository.
    attestation["executable_snapshot"] = _eet.snapshot()

    state = _esp.verify()
    attestation["state_provenance"] = {
        "clean": state["clean"], "problems": state["problems"],
        "contract_sha256": state["contract_sha256"],
        "results": [{k: r.get(k) for k in
                     ("module", "reachable", "pinned", "uncovered", "stale", "drifted",
                      "state_digest")} for r in state["results"]],
    }
    startup = _sp.check()
    attestation["startup_policy"] = {
        "clean": startup["clean"], "problems": startup["problems"],
        "policy_sha256": startup["policy_sha256"],
        "inert_observations": startup.get("inert_observations", []),
        "bound": startup["bound"], "surface": startup["surface"],
    }
    for layer, result in (("registry authority", registry),
                          ("executable inventory", inventory),
                          ("external executable trust", executables),
                          ("executed-state provenance", state), ("startup policy", startup),
                          ("cache authority", caches), ("npm toolchain identity", npm),
                          ("docker execution boundary", docker)):
        if not result["clean"]:
            message = (f"{layer} failed during bootstrap: " + "; ".join(result["problems"][:3])
                       + ". Refusing to run rather than trusting the state or startup surface "
                         "actually in force.")
            attestation["problems"].append(message)
            if strict:
                raise BootstrapError(message)

    if preseeded:
        attestation["problems"].append(
            f"protected module(s) {preseeded} were resident before the bootstrap ran; their "
            "provenance was verified against the pinned bytes rather than assumed")
    if not verdict["clean"]:
        msg = ("executed-code provenance failed during bootstrap: "
               + "; ".join(verdict["problems"][:3])
               + ". This is the Gate 4N-I28AC condition (ACC-I28AC-01): the staged file is not "
                 "the code that ran. Refusing to run rather than trusting resident code.")
        attestation["problems"].append(msg)
        if strict:
            raise BootstrapError(msg)
    # GATE 4N-I28BF-B1. Bind the AUTHORITATIVE Docker assurance state LAST, after every component
    # layer, since it aggregates authorization + repository + policy + parser + universe + per-site
    # + aggregate identities and populates its governed cache cold. Unlike docker_per_site it binds
    # the authorization pair and both universe digests, so a cross-tree or retired-authorization warm
    # cache is refusable. Session finish re-derives it FRESHLY and compares; the cache is never the
    # answer. Placed after the component-layer refusals so a more specific layer (e.g. an environment
    # tamper caught by external_executable_trust) surfaces its own message first.
    import docker_assurance_state as _das
    _das_clean = True
    try:
        attestation["docker_assurance"] = _das.establish_state()
    except _das.DockerAssuranceError as _exc:
        _das_clean = False
        attestation["problems"].append(f"Docker assurance baseline refused: {_exc}")
        if strict:
            raise BootstrapError(attestation["problems"][-1])
    attestation["established"] = (verdict["clean"] and state["clean"] and startup["clean"]
                                  and registry["clean"] and executables["clean"]
                                  and inventory["clean"] and caches["clean"]
                                  and npm["clean"] and docker["clean"] and _das_clean)
    return attestation


def reverify(config=None) -> dict:
    """Re-run every binding at session finish.

    GATE 4N-I28AG, closing ADV-I28AF-03. Both provenance modules used to state in their own
    residual-limitations sections that verification "is re-run at session finish". It was not:
    the only call site was establish(), reached once from pytest_configure. A mitigation that is
    documented and absent is worse than a gap, because it stops the next auditor looking. This is
    that re-run, and it exists so the claim and the code agree.

    Verifying at configure and consuming for the rest of the session leaves a window in which
    state can change. State tokens bind file CONTENT rather than file names, so a swap of the
    material itself is caught here even when the path never changed.
    """
    import executed_code_provenance as _ecp
    import executed_state_provenance as _esp
    import startup_policy as _sp

    import registry_authority as _ra
    import external_executable_trust as _eet
    import executable_inventory as _ei
    import cache_authority as _ca
    import npm_authority as _na
    import docker_boundary as _db
    import shell_positions as _shp

    entries = _protected_entries()
    layers = {"executed_code": _ecp.verify({"protected_modules": entries}),
              "executed_state": _esp.verify(),
              "startup_policy": _sp.check(),
              "registry_authority": _ra.verify(),
              "external_executable_trust": _eet.check(),
              "executable_inventory": _ei.check(),
              # A cache is verified at configure and read for the rest of the session, so the
              # window this closes is exactly the one reverify() exists for: a value poisoned
              # AFTER the bootstrap passed is only visible if something looks again at the end.
              "cache_authority": _ca.verify(),
              # GATE 4N-I28AS. Re-derived, not merely compared: a chain that became invalid after
              # configure must fail the session even if it drifted in a way the snapshot does not
              # cover.
              "npm_authority": _na.verify(),
              # GATE 4N-I28AT. Re-derived AND compared: steering introduced after configure, a
              # config file replaced, a context defined late or the policy itself swapped are only
              # ever visible as a difference from what was validated.
              "docker_boundary": _db.verify(),
              # GATE 4N-I28AV: re-derived at session finish, because a parser that begins
              # terminating early mid-session must fail the session rather than quietly shrink the
              # inventory.
              "shell_completeness": {
                  "clean": not _shp.completeness_digest()["untrustworthy"],
                  "problems": [f"{m}: shell parse is not trustworthy"
                               for m in _shp.completeness_digest()["untrustworthy"]]}}
    problems = [f"{name}: {p}" for name, result in layers.items() for p in result["problems"]]

    # GATE 4N-I28AI: compare the executables against the snapshot taken at configure. A binary
    # replaced at the same path, a changed symlink target, or a changed PATH are all invisible to
    # a fresh check() — they are only visible as a DIFFERENCE from what was validated.
    prior = getattr(config, BOOTSTRAP_ATTESTATION, None) if config is not None else None
    baseline_snapshot = (prior or {}).get("executable_snapshot")
    if baseline_snapshot:
        drift = _eet.compare(baseline_snapshot, _eet.snapshot())
        problems.extend(f"external_executable_trust: {d}" for d in drift)
        layers["executable_snapshot"] = {"clean": not drift}

    # GATE 4N-I28AS. The npm chain compared field by field against what was validated at configure:
    # npm content, mode, symlink chain, Node content, package root, package metadata, PATH, NVM_DIR
    # and npm configuration. A late substitution is only ever visible as a difference.
    npm_baseline = (prior or {}).get("npm_snapshot")
    if npm_baseline:
        npm_drift = _na.compare(npm_baseline, _na.snapshot())
        problems.extend(f"npm_authority: {d}" for d in npm_drift)
        layers["npm_snapshot"] = {"clean": not npm_drift}

    shell_baseline = (prior or {}).get("shell_completeness")
    if shell_baseline:
        fresh = _shp.completeness_digest()
        if fresh["digest"] != shell_baseline["digest"]:
            problems.append(
                "shell_positions: the parse-completeness digest changed after verification "
                f"({shell_baseline['digest'][:12]} -> {fresh['digest'][:12]}); a parser that stops "
                "consuming source mid-session shrinks every downstream inventory silently")
        if fresh["grammar_version"] != shell_baseline["grammar_version"]:
            problems.append("shell_positions: the grammar version changed mid-session")
        # GATE 4N-I28BB. The exec model is versioned and digested SEPARATELY, so a widened option
        # arity or a silently added option is attributable rather than lost inside one big digest.
        if fresh.get("exec_grammar_version") != shell_baseline.get("exec_grammar_version"):
            problems.append("shell_positions: the exec grammar version changed mid-session")
        if fresh.get("exec_grammar_digest") != shell_baseline.get("exec_grammar_digest"):
            problems.append(
                "shell_positions: the exec grammar contract changed mid-session; the option table "
                "and its arities are bound because a widened option is how a child stops being "
                "recognised without any inventory appearing to shrink")
        layers["shell_completeness_snapshot"] = {"clean": fresh["digest"] == shell_baseline["digest"]}

    # GATE 4N-I28BB. Freshly re-derive the exec transfers BOTH ways and compare against what was
    # bound at configure. A child removed, relabelled static-versus-dynamic, moved to another line,
    # or an oracle quietly disabled, is only ever visible as a difference from the baseline.
    exec_baseline = (prior or {}).get("exec_transfer")
    if exec_baseline:
        fresh_exec = _exec_transfer_state()
        if not fresh_exec["clean"]:
            problems.extend(f"exec_transfer: {p}" for p in fresh_exec["problems"])
        if fresh_exec["sites"] != exec_baseline.get("sites"):
            before = {(s["origin"], s["line"], s["word"], s["child"], s["classification"])
                      for s in exec_baseline.get("sites", [])}
            after = {(s["origin"], s["line"], s["word"], s["child"], s["classification"])
                     for s in fresh_exec["sites"]}
            for gone in sorted(before - after):
                problems.append(f"exec_transfer: a transfer site present at configure is GONE at "
                                f"session finish: {gone}")
            for new in sorted(after - before):
                problems.append(f"exec_transfer: a transfer site appeared after configure: {new}")
        if fresh_exec["grammar_version"] != exec_baseline.get("grammar_version"):
            problems.append("exec_transfer: the exec grammar version changed mid-session")
        if fresh_exec["grammar"] != exec_baseline.get("grammar"):
            problems.append("exec_transfer: the exec grammar contract changed mid-session")
        if not fresh_exec["positive_control_clean"]:
            problems.append("exec_transfer: the positive control stopped holding, so an empty "
                            "comparison can no longer be distinguished from a working one")
        layers["exec_transfer_snapshot"] = {"clean": fresh_exec["clean"]
                                            and fresh_exec["sites"] == exec_baseline.get("sites")}

    # GATE 4N-I28BF-A. Freshly RE-DERIVE the per-site state — never reuse the establishment object —
    # and compare it field by field against what was bound. I28BE ran per-site enforcement only at
    # establishment, so a late mutation after a clean baseline went undetected.
    per_site_baseline = (prior or {}).get("docker_per_site")
    if per_site_baseline:
        fresh_per_site = _db.per_site_state()
        drift = _db.per_site_differences(per_site_baseline, fresh_per_site)
        problems.extend(f"docker_per_site: {d}" for d in drift)
        layers["docker_per_site"] = {"clean": not drift and fresh_per_site["clean"]}

    # GATE 4N-I28BF-B1. Freshly RE-DERIVE the authoritative Docker assurance state and compare it
    # field by field against the bound baseline. reverify_state never consults the governed cache, so
    # a cache poisoned after establishment cannot mask a late tree, policy, universe, or authorization
    # change: the finish derivation is always fresh.
    das_baseline = (prior or {}).get("docker_assurance")
    if das_baseline:
        import docker_assurance_state as _das
        das_outcome = _das.reverify_state(das_baseline)
        problems.extend(das_outcome["problems"])
        layers["docker_assurance"] = {"clean": das_outcome["clean"]}

    docker_baseline = (prior or {}).get("docker_snapshot")
    if docker_baseline:
        docker_drift = _db.compare(docker_baseline, _db.snapshot())
        problems.extend(f"docker_boundary: {d}" for d in docker_drift)
        layers["docker_snapshot"] = {"clean": not docker_drift}
    outcome = {"clean": not problems, "problems": problems,
               "layers": {name: result["clean"] for name, result in layers.items()}}
    if config is not None:
        config._signalnest_bootstrap_reverified = outcome
    return outcome


def pytest_sessionfinish(session, exitstatus):
    """Fail the session when a binding that held at configure no longer holds."""
    config = getattr(session, "config", None)
    if config is None or getattr(config, BOOTSTRAP_ATTESTATION, None) is None:
        return                                    # this bootstrap never established anything
    outcome = reverify(config)
    if outcome["clean"]:
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    header = "SIGNALNEST BOOTSTRAP RE-VERIFICATION: FAILED"
    if reporter is not None:
        reporter.write_sep("=", header, red=True)
        for problem in outcome["problems"]:
            reporter.write_line(f"  {problem}")
    else:                                          # never stay silent
        print(header, file=sys.stderr)
        for problem in outcome["problems"]:
            print(f"  {problem}", file=sys.stderr)
    if exitstatus == 0:
        session.exitstatus = 3


def pytest_configure(config):
    """First plugin on the graded command. Must run before `-p pytest_session_guard`."""
    if getattr(config, BOOTSTRAP_ATTESTATION, None) is not None:
        return
    attestation = establish(strict=True)
    setattr(config, BOOTSTRAP_ATTESTATION, attestation)
    marker = os.environ.get("SIGNALNEST_BOOTSTRAP_ATTESTATION")
    if marker:
        try:
            Path(marker).write_text(json.dumps(attestation, indent=1, sort_keys=True))
        except OSError:
            pass


def main(argv=None) -> int:
    try:
        att = establish(strict=True)
    except BootstrapError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("SIGNALNEST BOOTSTRAP: refused")
        return 2
    print(f"  bootstrapped {len(att['loaded'])} protected module(s)")
    print("SIGNALNEST BOOTSTRAP: established")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
