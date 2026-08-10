#!/usr/bin/env python3
"""Docker execution boundary (Gate 4N-I28AT).

THE DEFECT THIS CLOSES. Gate 4N-I28AP finding ADV-I28AP-01. `external_executable_trust` binds the
docker CLI BINARY — path, content, mode — and nothing else. Which daemon that binary talks to is
chosen by state the repository never looked at. Measured on this tree before the fix, every one of

    DOCKER_HOST  DOCKER_CONTEXT  DOCKER_CONFIG  DOCKER_TLS_VERIFY  DOCKER_CERT_PATH
    DOCKER_API_VERSION  DOCKER_CONTENT_TRUST  BUILDKIT_HOST  DOCKER_BUILDKIT
    DOCKER_DEFAULT_PLATFORM

was accepted SILENTLY, with executed-code provenance, executed-state provenance, startup policy,
registry authority, executable trust, the executable inventory, cache authority, npm authority AND
session-finish reverification all reporting clean. A hostile `config.json` carrying `currentContext`,
`credsStore`, `credHelpers`, `auths`, `cliPluginsExtraDirs` and a proxy, plus a context store
pointing at `tcp://attacker.example:2375` with `SkipTLSVerify`, was accepted too. A repository-wide
search for any of those variable names returned NOTHING: they were not merely unbound, they were
unmentioned.

`scripts/docker-security-check.sh` reads its entire verdict — "runs as uid 10001", "no secrets baked
into the image" — from `docker run` output. Binding the CLI while leaving the daemon selectable means
that verdict can be produced by a daemon of the attacker's choosing.

THE MODEL: EXTERNAL CI INFRASTRUCTURE ASSUMPTION (Model B), chosen by MEASUREMENT. This host has no
docker CLI, no `~/.docker`, no daemon socket, no context store and no Docker Desktop, and the gate
forbids installing Docker. Model A ("repository-verified Docker") requires binding a daemon-reported
identity, which is impossible without a Docker-capable environment; claiming it here would be
inventing evidence. Model B claims LESS and enforces what it claims:

    the DAEMON is provisioned by the CI runner and its integrity is OUTSIDE repository verification
    ... but NO repository-controlled mechanism may redirect execution away from it.

So this module does not pretend to identify the daemon. It prohibits or binds every mechanism by
which the repository, the environment or the filesystem could point Docker somewhere else, and it
fails closed on anything it cannot adjudicate.

WHAT IS EXPLICITLY NOT CLAIMED. Binding the CLI does not bind the daemon, the context, the host, TLS
trust, configuration, plugins, credential helpers, registry identity, BuildKit workers, SSH transport
or remote endpoints. Model B asserts none of those. It asserts that repository-controlled redirection
is refused before Docker runs, and that the assumption itself is machine-checked rather than prose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "tests" / "fixtures" / "docker-boundary-policy.json"

# Trust boundaries a call site may occupy.
LOCAL_DAEMON_BOUND = "LOCAL_DAEMON_BOUND"
EXPLICIT_REMOTE_DAEMON_BOUND = "EXPLICIT_REMOTE_DAEMON_BOUND"
CONTEXT_BOUND = "CONTEXT_BOUND"
EXTERNAL_CI_DAEMON_ASSUMPTION = "EXTERNAL_CI_DAEMON_ASSUMPTION"
BINARY_ONLY_DIAGNOSTIC = "BINARY_ONLY_DIAGNOSTIC"
PROHIBITED_WHEN_STEERED = "PROHIBITED_WHEN_STEERED"
NOT_LOAD_BEARING = "NOT_LOAD_BEARING"

TRUST_BOUNDARIES = frozenset({
    LOCAL_DAEMON_BOUND, EXPLICIT_REMOTE_DAEMON_BOUND, CONTEXT_BOUND,
    EXTERNAL_CI_DAEMON_ASSUMPTION, BINARY_ONLY_DIAGNOSTIC, PROHIBITED_WHEN_STEERED,
    NOT_LOAD_BEARING,
})

# Steering dispositions.
FATAL_IF_PRESENT = "FATAL_IF_PRESENT"
REQUIRED_EXACT_VALUE = "REQUIRED_EXACT_VALUE"
ALLOWED_VALUE_SET = "ALLOWED_VALUE_SET"
CONTENT_BOUND = "CONTENT_BOUND"
NORMALIZED_AND_BOUND = "NORMALIZED_AND_BOUND"
NEUTRALIZED_BY_EXPLICIT_ARGV = "NEUTRALIZED_BY_EXPLICIT_ARGV"
EXTERNAL_INFRASTRUCTURE_ASSUMPTION = "EXTERNAL_INFRASTRUCTURE_ASSUMPTION"
IRRELEVANT_TO_ACTUAL_CALLS = "IRRELEVANT_TO_ACTUAL_CALLS"

DISPOSITIONS = frozenset({
    FATAL_IF_PRESENT, REQUIRED_EXACT_VALUE, ALLOWED_VALUE_SET, CONTENT_BOUND,
    NORMALIZED_AND_BOUND, NEUTRALIZED_BY_EXPLICIT_ARGV, EXTERNAL_INFRASTRUCTURE_ASSUMPTION,
    IRRELEVANT_TO_ACTUAL_CALLS,
})

# Steering FLAGS. Any of these on a graded docker argv redirects the client, so a call site is only
# as bound as its argv is free of them.
STEERING_FLAGS = ("--host", "-H", "--context", "--config", "--tls", "--tlsverify",
                  "--tlscacert", "--tlscert", "--tlskey")

DOCKER_WORDS = ("docker", "docker-compose", "dockerd")

# ---------------------------------------------------------------- GATE 4N-I28BE
# ADV-I28AX-ARCH-01 part A. Every call site carried `permitted_steering`,
# `prohibited_steering`, `required_verification` and `authoritative_inputs`, and NO decision path
# read any of them. Measured through final-decision behaviour, not code search: removing every
# field, blanking them, corrupting them, moving a site's source position, duplicating a site
# identity and adding an unknown field ALL left the Docker verdict clean — 8 of 10 mutations
# produced no failure at all. The records were evidence; enforcement is what follows.
#
# Two mutations WERE already caught, and it matters to say which: deleting a site is caught by the
# superset reconciliation, and adding a site with no `trust_boundary` is caught by the existing
# classification check. Those are not re-implemented here.

# Every field a call-site record must carry. Derived from the authored records rather than
# hand-listed twice: a record missing any of these is unadjudicable, and one carrying an unknown
# field is a schema change nobody reviewed.
PER_SITE_REQUIRED_FIELDS = frozenset({
    "id", "workflow", "job", "source", "step_name", "subcommand", "shell", "line_in_block",
    "permitted_steering", "prohibited_steering", "required_verification", "authoritative_inputs",
    "session_finish_obligation", "failure_behaviour", "continue_on_error", "trust_boundary", "why",
})

# The four decisions a site may receive. Only PASS satisfies a load-bearing site.
SITE_PASS = "PASS"
SITE_FAIL = "FAIL"
SITE_UNRESOLVED = "UNRESOLVED"
SITE_UNSUPPORTED = "UNSUPPORTED"
SITE_DECISIONS = (SITE_PASS, SITE_FAIL, SITE_UNRESOLVED, SITE_UNSUPPORTED)

# Site classification. Evidence-based, never filename alone: a site is graded because a workflow
# and job own it and the subcommand acts on a release artifact, not because of where it lives.
GRADED_RELEASE_BLOCKING = "GRADED_RELEASE_BLOCKING"
GRADED_NON_RELEASE = "GRADED_NON_RELEASE"
CI_INFRASTRUCTURE_ONLY = "CI_INFRASTRUCTURE_ONLY"
LOCAL_DEVELOPMENT_ONLY = "LOCAL_DEVELOPMENT_ONLY"
TEST_FIXTURE_ONLY = "TEST_FIXTURE_ONLY"
DOCUMENTATION_OR_INERT = "DOCUMENTATION_OR_INERT"
DYNAMIC_OR_UNRESOLVED = "DYNAMIC_OR_UNRESOLVED"
UNSUPPORTED_AND_FAIL_CLOSED = "UNSUPPORTED_AND_FAIL_CLOSED"
SITE_CLASSIFICATIONS = (GRADED_RELEASE_BLOCKING, GRADED_NON_RELEASE, CI_INFRASTRUCTURE_ONLY,
                        LOCAL_DEVELOPMENT_ONLY, TEST_FIXTURE_ONLY, DOCUMENTATION_OR_INERT,
                        DYNAMIC_OR_UNRESOLVED, UNSUPPORTED_AND_FAIL_CLOSED)

# Subcommands that act on a release artifact. `push` publishes; `build`/`buildx` produce the thing
# that gets published; `run` executes it.
_RELEASE_SUBCOMMANDS = frozenset({"push", "build", "buildx", "run", "create", "tag"})

# Classifications for which PASS is mandatory.
LOAD_BEARING_CLASSIFICATIONS = frozenset({GRADED_RELEASE_BLOCKING, GRADED_NON_RELEASE})


# GATE 4N-I28BF-A, closing I28BE-CAT-01. The previous helper lowercased the entry and tested
# `"context" in lowered`, `"config" in lowered`, `"flag" in lowered`, `"compose" in lowered`.
# Measured before this fix, FIFTEEN inputs appearing in no authored policy entry still resolved to
# real mechanisms — "contextual analysis" to DOCKER_CONTEXT, "misconfiguration" to DOCKER_CONFIG and
# XDG_CONFIG_HOME, "flagrant nonsense" to all nine steering flags, "subcontext", "tls-variables",
# "TLS variables." and so on. Not exploitable through the authored policy, which uses only exact
# names and three categories, but a fail-open path for any future entry.
#
# Substring matching is the wrong shape for a trust boundary. This is an EXACT table.
CATEGORY_TABLE_VERSION = "i28bf-a.1"

# Normalization is deliberately minimal and documented. Outer whitespace only: nothing that could
# change meaning. No case folding, no punctuation stripping, no hyphen/underscore equivalence, no
# pluralisation — every one of those was a measured widening path, and a category whose meaning
# depends on normalisation is a category that should be spelled correctly instead.
NORMALIZATION_VERSION = "i28bf-a.1-outer-whitespace-only"

# The three intended prose categories, DERIVED from the authored policy (every entry that is not a
# concrete steering name), each mapped to an exact mechanism set.
DOCKER_STEERING_CATEGORIES = {
    "TLS variables": ("DOCKER_CERT_PATH", "DOCKER_TLS", "DOCKER_TLS_VERIFY"),
    "steering flags": STEERING_FLAGS,
    "a defined Docker context": ("DOCKER_CONTEXT",),
}

CATEGORY_CONCRETE = "CONCRETE_NAME"
CATEGORY_CANONICAL = "CANONICAL_CATEGORY"
CATEGORY_INVALID = "INVALID"


def _normalize_category(entry):
    """Outer whitespace only. Returns None for anything that is not a string."""
    if not isinstance(entry, str):
        return None
    return entry.strip()


def category_table_digest() -> str:
    """Canonical digest over the table, its version and its normalization contract.

    Bound into session baseline and re-derived at session finish, so a widened or narrowed mapping
    is a detectable change rather than a silent one.
    """
    payload = {
        "table_version": CATEGORY_TABLE_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "categories": {name: sorted(mechs) for name, mechs in DOCKER_STEERING_CATEGORIES.items()},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def category_table_problems() -> list:
    """Structural defects in the table itself, checked rather than assumed."""
    problems = []
    seen = {}
    for name, mechs in DOCKER_STEERING_CATEGORIES.items():
        if not isinstance(name, str) or name != name.strip() or not name:
            problems.append(f"category key {name!r} is not a clean canonical string")
        if not mechs:
            problems.append(f"category {name!r} maps to ZERO mechanisms, which can never enforce")
        key = tuple(sorted(mechs))
        if key in seen:
            problems.append(f"categories {seen[key]!r} and {name!r} map to an identical mechanism "
                            "set, which makes the distinction ambiguous")
        seen[key] = name
    return problems


def resolve_steering_entry(entry, steering_table: dict) -> tuple:
    """(classification, mechanisms). EXACT lookup only — no substring, prefix or fuzzy matching.

    A concrete steering name resolves to itself; one of the three canonical categories resolves to
    its exact mechanism set; everything else is INVALID and fails closed.
    """
    normalized = _normalize_category(entry)
    if normalized is None or not normalized:
        return CATEGORY_INVALID, ()
    if normalized in steering_table:
        return CATEGORY_CONCRETE, (normalized,)
    if normalized in DOCKER_STEERING_CATEGORIES:
        return CATEGORY_CANONICAL, tuple(DOCKER_STEERING_CATEGORIES[normalized])
    return CATEGORY_INVALID, ()


def _resolve_steering_category(entry, steering_table: dict) -> list:
    """Backwards-compatible shim over the exact resolver."""
    _classification, mechanisms = resolve_steering_entry(entry, steering_table)
    return list(mechanisms)


def classify_site(site: dict) -> tuple:
    """(classification, evidence). Evidence-based; filename alone is never the classifier."""
    workflow = site.get("workflow")
    source = site.get("source") or ""
    subcommand = site.get("subcommand")
    if site.get("trust_boundary") is None:
        return UNSUPPORTED_AND_FAIL_CLOSED, "no trust_boundary; an unclassified site fails closed"
    if subcommand is None:
        return (DYNAMIC_OR_UNRESOLVED,
                "no statically resolvable subcommand, so what this call does is unresolved")
    if workflow:
        job = site.get("job") or ""
        if subcommand in _RELEASE_SUBCOMMANDS:
            return (GRADED_RELEASE_BLOCKING,
                    f"owned by workflow {workflow} job {job}; subcommand {subcommand!r} acts on a "
                    "release artifact")
        return (GRADED_NON_RELEASE,
                f"owned by workflow {workflow} job {job}; subcommand {subcommand!r} does not act "
                "on a release artifact")
    if source.endswith(".sh"):
        return (CI_INFRASTRUCTURE_ONLY,
                f"invoked from {source}, which no workflow step owns as a graded call")
    return UNSUPPORTED_AND_FAIL_CLOSED, "no workflow and no recognised script source"


def adjudicate_site(site: dict, state: dict, policy: dict) -> dict:
    """One deterministic decision for one site, CONSUMING every required field.

    This is the enforcement consumer ADV-I28AX-ARCH-01 said was missing. Every field named in
    PER_SITE_REQUIRED_FIELDS is read here and can change the outcome; a field that is only
    serialised into evidence is exactly the defect this closes.
    """
    problems = []
    present = set(site)
    missing = sorted(PER_SITE_REQUIRED_FIELDS - present)
    unknown = sorted(present - PER_SITE_REQUIRED_FIELDS)
    if missing:
        problems.append(f"missing required field(s) {missing}; an unadjudicable record fails closed")
    if unknown:
        problems.append(f"unknown field(s) {unknown}; a schema change nobody reviewed fails closed")
    classification, evidence = classify_site(site)
    if classification == UNSUPPORTED_AND_FAIL_CLOSED:
        return {"id": site.get("id"), "decision": SITE_UNSUPPORTED, "classification": classification,
                "evidence": evidence, "problems": problems + [evidence], "consumed_fields": []}
    if classification == DYNAMIC_OR_UNRESOLVED:
        return {"id": site.get("id"), "decision": SITE_UNRESOLVED, "classification": classification,
                "evidence": evidence, "problems": problems + [evidence], "consumed_fields": []}
    consumed = []
    # prohibited_steering mixes real variable NAMES with prose CATEGORIES ("TLS variables",
    # "steering flags", "a defined Docker context"). My first version compared observed environment
    # names against these lists directly and failed all 47 load-bearing sites over SSH_AUTH_SOCK —
    # a variable the steering layer already adjudicates and which no site names. String-matching a
    # prose category is a category error, so each entry is RESOLVED to the mechanism that enforces
    # it, and an entry that resolves to nothing is an undeclared category that fails closed.
    prohibited = site.get("prohibited_steering")
    if not isinstance(prohibited, list):
        problems.append("prohibited_steering is not a list")
    else:
        consumed.append("prohibited_steering")
        if classification in LOAD_BEARING_CLASSIFICATIONS and not prohibited:
            problems.append("a load-bearing site declares NO prohibited steering, which would make "
                            "the record vacuous")
        steering_table = policy.get("steering") or {}
        for entry in prohibited:
            names = _resolve_steering_category(entry, steering_table)
            if not names:
                problems.append(f"prohibited_steering entry {entry!r} resolves to no enforced "
                                "mechanism; an undeclared category cannot be adjudicated")
                continue
            for name in names:
                if name in (state.get("environment") or {}):
                    rule = steering_table.get(name) or {}
                    if rule.get("disposition") == FATAL_IF_PRESENT:
                        problems.append(f"prohibited steering {name!r} is present and FATAL for "
                                        "this site")
    # permitted_steering is the site's allow-list. Empty means "nothing extra"; any name it lists
    # must be a real steering variable, never invented.
    permitted = site.get("permitted_steering")
    if not isinstance(permitted, list):
        problems.append("permitted_steering is not a list")
    else:
        consumed.append("permitted_steering")
        for name in permitted:
            if name not in (policy.get("steering") or {}):
                problems.append(f"permitted_steering names {name!r}, which is not a steering "
                                "variable this policy adjudicates")
    # required_verification and authoritative_inputs must be non-empty statements.
    for field in ("required_verification", "authoritative_inputs"):
        value = site.get(field)
        if value is None or (isinstance(value, (list, str)) and not value):
            problems.append(f"{field} is empty; a load-bearing site must state it")
        else:
            consumed.append(field)
    # failure_behaviour and continue_on_error decide whether a refusal actually stops the run.
    if site.get("continue_on_error") is not False:
        problems.append("continue_on_error is not False, so a refusal would not stop the call")
    else:
        consumed.append("continue_on_error")
    if not site.get("failure_behaviour"):
        problems.append("failure_behaviour is empty")
    else:
        consumed.append("failure_behaviour")
    if not site.get("session_finish_obligation"):
        problems.append("session_finish_obligation is empty")
    else:
        consumed.append("session_finish_obligation")
    # `workflow`, `job` and `step_name` are legitimately null for a site that lives in a shell
    # script rather than a workflow step. The KEY must be present on every record — that is the
    # schema — but demanding a non-empty value everywhere failed all three script sites for having
    # no step name, which is a property of shell scripts, not a defect in the record.
    always_valued = ["id", "source", "shell", "why", "trust_boundary", "line_in_block"]
    if site.get("workflow"):
        always_valued += ["job", "step_name"]
    for field in always_valued:
        if site.get(field) in (None, ""):
            problems.append(f"{field} is empty")
        else:
            consumed.append(field)
    for field in ("workflow", "job", "step_name", "subcommand"):
        consumed.append(field)          # present-ness enforced above; subcommand decides the class
    position_problems = source_position_problems(site)
    problems.extend(position_problems)
    # `source_position` is deliberately NOT added to consumed_fields: that set is the AUTHORED
    # schema, and position is DERIVED from the record. Position is enforced by its own
    # fail-closed check above and compared through the snapshot, so adding it here would only
    # blur what consumed_fields means.
    decision = SITE_FAIL if problems else SITE_PASS
    return {"id": site.get("id"), "decision": decision, "classification": classification,
            "evidence": evidence, "problems": problems, "consumed_fields": sorted(set(consumed)),
            "position": canonical_source_position(site)}


SOURCE_POSITION_VERSION = "i28bf-a3.1"


def canonical_source_position(site: dict) -> str:
    """One deterministic string identifying WHERE a Docker call site lives.

    GATE 4N-I28BF-A3, closing A2-FIND-01. `per_site_state` carried id, decision, classification and
    the consumed-field set — and no position. Measured before this fix: moving a load-bearing call
    from line 1 to line 41, with identical command, classification, steering fields and decision,
    produced NO difference at session finish and left enforcement clean. Late attack 12 was
    therefore uncovered, and had been miscounted as complete.

    Composed from the OWNING SOURCE plus the containing context, never a basename and never the
    site id split on '#' — that split was the probe defect at I28BD which collapsed every
    per-workflow count to zero.
    """
    parts = [
        str(site.get("source") or ""),
        str(site.get("workflow") or ""),
        str(site.get("job") or ""),
        str(site.get("step_name") or ""),
        str(site.get("line_in_block")),
    ]
    return "|".join(parts)


def source_position_problems(site: dict) -> list:
    """A load-bearing site with no derivable position cannot be compared, so it fails closed."""
    problems = []
    if not site.get("source"):
        problems.append("source path is missing, so no canonical position can be derived")
    line = site.get("line_in_block")
    if line is None:
        problems.append("line_in_block is missing")
    elif not isinstance(line, int) or line < 0:
        problems.append(f"line_in_block {line!r} is not a non-negative integer")
    return problems


def per_site_state(policy: dict | None = None, state: dict | None = None) -> dict:
    """A canonical, comparable snapshot of Docker per-site enforcement.

    GATE 4N-I28BF-A, closing I28BE-SESSION-01. `reverify()` had no docker_per_site layer, so a late
    mutation after a clean baseline was never re-derived and never compared. This is the value the
    baseline binds and the session finish re-derives FRESHLY — never the initial result object.
    """
    result = enforce_per_site(policy, state)
    return {
        "category_table_version": CATEGORY_TABLE_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "category_table_digest": category_table_digest(),
        "policy_digest": (hashlib.sha256(POLICY.read_bytes()).hexdigest() if POLICY.is_file() else ""),
        "sites": result["sites"],
        "clean": result["clean"],
        "decision_counts": result["decision_counts"],
        "classification_counts": result["classification_counts"],
        "load_bearing": result["load_bearing"],
        "workflow_assurance_coverage": result["workflow_assurance_coverage"],
        "source_position_version": SOURCE_POSITION_VERSION,
        "per_site": sorted(
            ({"id": d["id"], "decision": d["decision"], "classification": d["classification"],
              "consumed": ",".join(sorted(d["consumed_fields"])),
              # GATE 4N-I28BF-A3: WHERE the call lives, so a moved site is a difference.
              "position": d.get("position", "")} for d in result["decisions"]),
            key=lambda d: d["id"]),
    }


def per_site_differences(before: dict, after: dict) -> list:
    """Every load-bearing difference between the bound and the freshly re-derived state."""
    problems = []
    for field in ("category_table_version", "normalization_version", "category_table_digest",
                  "policy_digest", "sites", "load_bearing", "workflow_assurance_coverage",
                  "source_position_version"):
        if before.get(field) != after.get(field):
            problems.append(f"{field} changed after establishment: "
                            f"{before.get(field)!r} -> {after.get(field)!r}")
    if after.get("clean") is not True:
        problems.append("the freshly re-derived per-site enforcement is not clean")
    before_sites = {d["id"]: d for d in before.get("per_site", [])}
    after_sites = {d["id"]: d for d in after.get("per_site", [])}
    for gone in sorted(set(before_sites) - set(after_sites)):
        problems.append(f"Docker site {gone!r} present at establishment is GONE at session finish")
    for new in sorted(set(after_sites) - set(before_sites)):
        problems.append(f"Docker site {new!r} appeared after establishment")
    for identity in sorted(set(before_sites) & set(after_sites)):
        for key in ("decision", "classification", "consumed", "position"):
            if before_sites[identity][key] != after_sites[identity][key]:
                problems.append(f"Docker site {identity!r} {key} changed: "
                                f"{before_sites[identity][key]!r} -> {after_sites[identity][key]!r}")
    return problems


def enforce_per_site(policy: dict | None = None, state: dict | None = None) -> dict:
    """The authoritative per-site consumer. Its result feeds the Docker aggregate.

    No fixed expected count anywhere: the universe is whatever the policy declares, and the
    invariants are structural — one decision per site, no decision without a site, no duplicates.
    """
    doc = policy if policy is not None else load_policy()
    observed = state if state is not None else steering_state()
    sites = doc.get("call_sites")
    problems = []
    if not isinstance(sites, list) or not sites:
        return {"clean": False, "decisions": [], "sites": 0,
                "problems": ["the Docker call-site universe is missing or empty; an empty universe "
                             "would make per-site enforcement vacuous"]}
    seen = {}
    decisions = []
    for site in sites:
        if not isinstance(site, dict):
            problems.append(f"call-site record is not an object: {site!r}")
            continue
        identity = site.get("id")
        if identity in seen:
            problems.append(f"duplicate site identity {identity!r}; two records for one site make "
                            "the decision ambiguous")
        seen[identity] = True
        decisions.append(adjudicate_site(site, observed, doc))
    # Structural invariants: exactly one decision per universe site, none for anything else.
    if len(decisions) != len([s for s in sites if isinstance(s, dict)]):
        problems.append("the number of decisions does not equal the number of universe sites")
    decided = {d["id"] for d in decisions}
    universe = {s.get("id") for s in sites if isinstance(s, dict)}
    for extra in sorted(decided - universe):
        problems.append(f"a decision exists for {extra!r}, which is not in the universe")
    for undecided in sorted(universe - decided):
        problems.append(f"universe site {undecided!r} has no decision")
    for d in decisions:
        if d["classification"] in LOAD_BEARING_CLASSIFICATIONS and d["decision"] != SITE_PASS:
            problems.append(f"load-bearing site {d['id']!r} is {d['decision']}: "
                            + "; ".join(d["problems"][:2]))
        # UNSUPPORTED and UNRESOLVED always fail, whatever the class. The first version only failed
        # LOAD_BEARING classes, so a site that could not be classified at all fell out of the
        # load-bearing set and escaped: adding a record with no trust_boundary left the aggregate
        # clean. A site that cannot be classified cannot be shown to be non-load-bearing.
        if d["decision"] in (SITE_UNSUPPORTED, SITE_UNRESOLVED):
            problems.append(f"site {d['id']!r} is {d['decision']} and therefore cannot be shown "
                            f"safe: {'; '.join(d['problems'][:2])}")
        if d["decision"] not in SITE_DECISIONS:
            problems.append(f"site {d['id']!r} produced an unknown decision {d['decision']!r}")

    # Reconcile the AUTHORED universe against the DERIVED one, both directions. Without this a
    # deleted policy record simply shrinks the universe and every remaining site still passes.
    try:
        derived = derive_call_sites()
    except Exception as exc:                       # fail closed; never silently skip
        problems.append(f"the derived Docker universe could not be built ({type(exc).__name__}), "
                        "so the authored universe cannot be reconciled")
        derived = None
    if derived is not None:
        derived_ids = {s["id"] for s in derived.get("sites", [])} if isinstance(derived, dict) else set()
        if not derived_ids:
            problems.append("the derived Docker universe is EMPTY; an empty derivation would make "
                            "reconciliation vacuously agree")
        else:
            for gone in sorted(derived_ids - universe):
                problems.append(f"derived Docker site {gone!r} has no authored record")
            for extra in sorted(universe - derived_ids):
                problems.append(f"authored Docker site {extra!r} is not in the derived universe")
            # env_keys lives on the DERIVED records, not the authored ones. Adjudicate it here or
            # it stays exactly what ADV-I28AX-ARCH-01 called it: produced and never read.
            for site in (derived.get("sites") or []):
                if "env_keys" not in site:
                    problems.append(f"derived site {site.get('id')!r} carries no env_keys, so "
                                    "workflow-declared Docker steering is unadjudicated")
                    continue
                for key in site.get("env_keys") or []:
                    # `doc`, not `policy`: the parameter may be None when the caller relies on the
                    # default load. Every test passed the policy explicitly, so this crashed only
                    # when enforce_per_site() was invoked with no arguments — which is how the
                    # session baseline and evidence generation call it.
                    if key in (doc.get("steering") or {}):
                        rule = (doc.get("steering") or {})[key]
                        if rule.get("disposition") == FATAL_IF_PRESENT:
                            problems.append(f"site {site.get('id')!r} declares steering variable "
                                            f"{key!r} in its own step env, which is FATAL")
    counts = {k: sum(1 for d in decisions if d["decision"] == k) for k in SITE_DECISIONS}
    by_class = {c: sum(1 for d in decisions if d["classification"] == c)
                for c in SITE_CLASSIFICATIONS}
    return {"clean": not problems, "problems": problems, "sites": len(sites),
            "decisions": decisions, "decision_counts": counts, "classification_counts": by_class,
            "load_bearing": sum(1 for d in decisions
                                if d["classification"] in LOAD_BEARING_CLASSIFICATIONS),
            # GATE 4N-I28BE, §18. Workflow assurance coverage is NOT adjudicated here and is NOT
            # claimed closed. It is recorded as explicitly deferred so no reader can mistake a
            # clean per-site result for coverage.
            "workflow_assurance_coverage": "NOT_ADJUDICATED — deferred to Gate 4N-I28BG "
                                           "(ADV-I28AX-ARCH-01 part B)"}



class DockerBoundaryError(RuntimeError):
    """Fail closed. A Docker call whose execution boundary is unestablished never runs."""


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_policy(path: Path | None = None) -> dict:
    p = path or POLICY
    if not p.is_file():
        raise DockerBoundaryError(
            f"the Docker boundary policy is missing at {p}. Without it this control would have to "
            "assume Docker steering is harmless, which is exactly the assumption ADV-I28AP-01 "
            "exploited.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("model") not in ("MODEL_A_REPOSITORY_VERIFIED", "MODEL_B_EXTERNAL_CI_ASSUMPTION"):
        raise DockerBoundaryError(f"unknown execution-boundary model {doc.get('model')!r}")
    sites = doc.get("call_sites")
    if not isinstance(sites, list) or not sites:
        raise DockerBoundaryError("the policy declares no Docker call site; an empty inventory "
                                  "would adjudicate nothing")
    for site in sites:
        if site.get("trust_boundary") not in TRUST_BOUNDARIES:
            raise DockerBoundaryError(
                f"{site.get('id')}: trust boundary {site.get('trust_boundary')!r} is not one of "
                f"{sorted(TRUST_BOUNDARIES)}. An unclassified call site fails closed.")
    for name, entry in (doc.get("steering") or {}).items():
        if entry.get("disposition") not in DISPOSITIONS:
            raise DockerBoundaryError(
                f"{name}: disposition {entry.get('disposition')!r} is not one of "
                f"{sorted(DISPOSITIONS)}. An unclassified steering mechanism fails closed.")
    if doc["model"] == "MODEL_B_EXTERNAL_CI_ASSUMPTION":
        assumption = doc.get("ci_assumption") or {}
        for required in ("version", "statement", "runner", "marker_env", "workflows",
                         "daemon_provisioned_by", "not_claimed"):
            if not assumption.get(required):
                raise DockerBoundaryError(
                    f"the external-CI assumption is missing '{required}'. Gate 4N-I28AT: a "
                    "prose-only assumption is insufficient — it must be machine-enforced.")
    return doc


# --------------------------------------------------------------------------- call-site derivation
def _strip_comment(line: str) -> str:
    """Remove a trailing comment, honouring quotes so a `#` inside a string survives."""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (not out or out[-1].isspace()):
            break
        out.append(ch)
    return "".join(out)


def _command_words(line: str):
    """Every word in a COMMAND POSITION on one logical line.

    A word is in command position at the start of the line, or immediately after a separator
    (`|`, `||`, `&&`, `;`, `&`), or at the start of a command substitution, or after a keyword that
    introduces a command (`if`, `then`, `else`, `do`, `elif`, `!`, `until`, `while`).

    THIS EXISTS BECAUSE THE SHARED DERIVER IS INCOMPLETE. Gate 4N-I28AT finding ADV-I28AT-01:
    `shell_positions` silently stops recognising commands after a `case ... esac`, with zero
    unresolved and zero unsupported reported — 39 command lines are swallowed repository-wide, TWO
    of which are graded `docker run` call sites. Fixing the shared grammar is outside this gate's
    authorized scope, but a Docker inventory that inherits the same blind spot would be an
    execution boundary with a hole in it, so the Docker call-site derivation is done here and
    RECONCILED against the shared deriver rather than trusting it.
    """
    line = _strip_comment(line)
    if not line.strip():
        return []
    # Normalise separators and substitution openers to a single delimiter, honouring quotes.
    tokens, buf, quote, i = [], [], None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            # A command substitution OPENS A NEW COMMAND even inside double quotes. Missing this
            # was the first defect in this scanner: `entry="$(docker inspect ...)"` hid every
            # `docker inspect` call site, and the superset reconciliation against the shared
            # deriver caught it immediately — 29 sites found where the shared deriver saw 49.
            # Single quotes suppress substitution, so the distinction is real, not cosmetic.
            if quote == '"' and line[i:i + 2] == "$(":
                tokens.append("".join(buf)); buf = []; i += 2; continue
            if quote == '"' and ch == "`":
                tokens.append("".join(buf)); buf = []; i += 1; continue
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = line[i:i + 2]
        if two in ("||", "&&"):
            tokens.append("".join(buf)); buf = []; i += 2; continue
        if two == "$(":
            tokens.append("".join(buf)); buf = []; i += 2; continue
        if ch in "|;&(){}":
            tokens.append("".join(buf)); buf = []; i += 1; continue
        if ch == "`":
            tokens.append("".join(buf)); buf = []; i += 1; continue
        buf.append(ch)
        i += 1
    tokens.append("".join(buf))

    KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "time", "case", "esac",
                "in", "for", "done", "fi", "select", "function"}
    words = []
    for token in tokens:
        stripped = token.strip()
        if not stripped:
            continue
        parts = stripped.split()
        idx = 0
        # Skip leading VAR=value assignments and command keywords.
        while idx < len(parts) and (re.match(r"^[A-Za-z_]\w*=", parts[idx])
                                    or parts[idx] in KEYWORDS):
            idx += 1
        # GATE 4N-I28BB, ADV-I28AX-01. `exec` REPLACES the shell with its operand, so the operand is
        # still a command position and `exec docker run --privileged` is a real Docker call site.
        # This scanner shared the shared deriver's blind spot exactly, which is why Gate 4N-I28AV
        # recorded a reconciliation difference of 0 and read it as confirmation: two derivations
        # agreeing because BOTH omit the same command is correlated error, not independence.
        #
        # The option table is explicit and arity-bearing, mirroring shell_positions.EXEC_OPTIONS.
        # A generic hyphen-skip would make `exec -a docker kubectl` report `docker` — the VALUE of
        # -a — as the call site.
        if idx < len(parts) and parts[idx].strip("\"'") == "exec":
            idx += 1
            while idx < len(parts):
                opt = parts[idx]
                if opt == "--":
                    idx += 1
                    break
                if re.match(r"^[0-9]*(>>?|<<<|<)&?[0-9-]*$", opt):
                    idx += 1
                    if not opt.endswith(("&1", "&2")) and idx < len(parts):
                        idx += 1
                    continue
                if not opt.startswith("-") or opt == "-":
                    break
                if opt == "-a":
                    idx += 2                       # the NAME is never the child
                    continue
                if len(opt) > 1 and all(c in "cl" for c in opt[1:]):
                    idx += 1
                    continue
                idx = len(parts)                   # unknown option: no child may be claimed
                break
        if idx < len(parts):
            word = parts[idx].strip("\"'")
            if word:
                words.append((word, parts[idx:]))
    return words


def _scan_source(text: str):
    """Docker command positions in one shell source, with heredoc BODIES treated as inert."""
    found = []
    pending = []
    for n, raw in enumerate(text.splitlines(), 1):
        if pending:
            if raw.strip() == pending[0]:
                pending.pop(0)
            continue
        for m in re.finditer(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", raw):
            pending.append(m.group(2))
        for word, argv in _command_words(raw):
            if word in DOCKER_WORDS:
                found.append({"line": n, "word": word, "argv": " ".join(argv)[:400],
                              "subcommand": next((a for a in argv[1:] if not a.startswith("-")),
                                                 None)})
    return found


def _parsed_workflows():
    """Every workflow, parsed once.

    A separate helper with a SINGLE local import, and that is the point rather than tidiness. When
    `reconcile_with_shared_deriver` imported both `yaml` and `shell_positions`, the executed-state
    deriver bound `LOCALCALLABLE:yaml.scan` — it attributed `sp.scan()` to the wrong module because
    two local imports in one function make the bytecode attribution ambiguous. The Gate 4N-I28AG
    independent oracle caught the misattribution, and the right response was to make the code
    unambiguous rather than to tune either derivation to match the other.
    """
    try:
        import yaml
    except ImportError as exc:                                   # pragma: no cover - see below
        # A CRASH IS NOT A VERDICT. Gate 4N-I28AR settled this: an unrelated exception is not an
        # acceptable substitute for the intended refusal. Without a YAML parser the Docker
        # call-site inventory cannot be derived at all, so the boundary is UNESTABLISHED — which is
        # a refusal with a nameable cause, not a stack trace. Observed with the user site disabled.
        raise DockerBoundaryError(
            f"the Docker call-site inventory cannot be derived because PyYAML is unavailable "
            f"({exc}). The execution boundary is therefore unestablished and every graded Docker "
            "call site is unadjudicated; this refuses rather than proceeding with an empty "
            "inventory, which would silently classify nothing.") from exc

    return [(wf, yaml.safe_load(wf.read_text(encoding="utf-8")))
            for wf in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))]


def derive_call_sites() -> dict:
    """Every graded Docker call site, derived from the workflows and tracked shell scripts."""
    sites, problems = [], []
    for wf, doc in _parsed_workflows():
        wf_shell = (((doc.get("defaults") or {}).get("run") or {}).get("shell"))
        for job_name, job in (doc.get("jobs") or {}).items():
            job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell")
            for index, step in enumerate(job.get("steps") or []):
                run = step.get("run")
                if not run:
                    continue
                for hit in _scan_source(run):
                    sites.append({
                        "id": f"{wf.name}#{job_name}#{index}#{hit['line']}",
                        "source": str(wf.relative_to(REPO_ROOT)),
                        "workflow": wf.name, "job": job_name,
                        "step_index": index, "step_name": step.get("name"),
                        "step_id": step.get("id"),
                        "line_in_block": hit["line"], "command": hit["word"],
                        "subcommand": hit["subcommand"], "argv": hit["argv"],
                        "shell": step.get("shell") or job_shell or wf_shell,
                        "working_directory": step.get("working-directory"),
                        "continue_on_error": bool(step.get("continue-on-error", False)),
                        "condition": step.get("if"),
                        "env_keys": sorted((step.get("env") or {})),
                    })
    for script in sorted((REPO_ROOT / "scripts").rglob("*.sh")):
        text = script.read_text(encoding="utf-8")
        for hit in _scan_source(text):
            sites.append({
                "id": f"{script.name}#{hit['line']}",
                "source": str(script.relative_to(REPO_ROOT)),
                "workflow": None, "job": None, "step_index": None, "step_name": None,
                "step_id": None, "line_in_block": hit["line"], "command": hit["word"],
                "subcommand": hit["subcommand"], "argv": hit["argv"], "shell": "bash",
                "working_directory": None, "continue_on_error": False, "condition": None,
                "env_keys": [],
            })
    return {"sites": sites, "problems": problems, "count": len(sites)}


def reconcile_with_shared_deriver() -> dict:
    """This module's derivation must be a SUPERSET of the shared one, and the gap is REPORTED.

    Not a courtesy check. Gate 4N-I28AT finding ADV-I28AT-01 means the two derivations legitimately
    disagree today, and a silent disagreement between two components that both claim to know where
    Docker runs is worse than either being wrong alone. If this module ever finds FEWER sites than
    `shell_positions`, that is a defect HERE and it fails closed.
    """
    import shell_positions as sp

    shared = 0
    for _wf, doc in _parsed_workflows():
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                if step.get("run"):
                    shared += sum(1 for c in sp.scan(step["run"]).commands
                                  if c.word in DOCKER_WORDS)
    for script in sorted((REPO_ROOT / "scripts").rglob("*.sh")):
        text = script.read_text(encoding="utf-8")
        res = sp.scan_script(text) if hasattr(sp, "scan_script") else sp.scan(text)
        shared += sum(1 for c in res.commands if c.word in DOCKER_WORDS)

    mine = derive_call_sites()["count"]
    problems = []
    if mine < shared:
        problems.append(
            f"the Docker call-site derivation found {mine} sites but the shared command-position "
            f"deriver found {shared}. This module must be a SUPERSET: finding fewer means the "
            "Docker inventory has a blind spot the rest of the system does not.")
    return {"docker_boundary_sites": mine, "shell_positions_sites": shared,
            "difference": mine - shared, "problems": problems}


# --------------------------------------------------------------------------- steering state
def steering_state() -> dict:
    """Every Docker-relevant input actually present, read rather than assumed."""
    doc = load_policy()
    # Collect by PREFIX, not from the policy's own key list.
    #
    # The first version iterated the policy's declared names, so a variable nobody had classified
    # was never even collected — and the "unclassified fails closed" branch below was unreachable
    # code. `DOCKER_MYSTERY_KNOB` sailed through. A control that can only see what it already knows
    # about cannot fail closed on the unknown, which is the entire point of section 8's rule that no
    # relevant steering mechanism may remain unclassified.
    prefixes = ("DOCKER_", "BUILDKIT_", "COMPOSE_")
    env = {name: value for name, value in os.environ.items()
           if name.startswith(prefixes) or name in (doc.get("steering") or {})}
    env = dict(sorted(env.items()))
    config_dir = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
    config_file = config_dir / "config.json"
    state = {
        "environment": env,
        "docker_on_path": shutil.which("docker"),
        "config_dir": str(config_dir),
        "config_dir_exists": config_dir.is_dir(),
        "config_file_exists": config_file.is_file(),
        "config_sha256": _digest_bytes(config_file.read_bytes()) if config_file.is_file() else None,
        "config_is_symlink": config_file.is_symlink(),
        "contexts_dir_exists": (config_dir / "contexts").is_dir(),
        "contexts": [],
        "ci_marker": {name: os.environ.get(name)
                      for name in (doc.get("ci_assumption") or {}).get("marker_env", [])},
    }
    contexts_meta = config_dir / "contexts" / "meta"
    if contexts_meta.is_dir():
        for meta in sorted(contexts_meta.rglob("meta.json")):
            try:
                parsed = json.loads(meta.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                state["contexts"].append({"path": str(meta), "unparseable": True})
                continue
            endpoints = parsed.get("Endpoints") or {}
            state["contexts"].append({
                "path": str(meta), "name": parsed.get("Name"),
                "host": (endpoints.get("docker") or {}).get("Host"),
                "skip_tls_verify": (endpoints.get("docker") or {}).get("SkipTLSVerify"),
                "sha256": _digest_bytes(meta.read_bytes()),
            })
    return state


def config_fields(state: dict) -> dict:
    """The config.json fields that can redirect execution, inject helpers or change auth."""
    path = Path(state["config_dir"]) / "config.json"
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"__unparseable__": True}
    # EVERY top-level key, not a known-key list.
    #
    # Same fail-open as the steering collector had: extracting only the fields already classified
    # meant an unrecognised field was never seen, and `config_problems`'s "unclassified field fails
    # closed" branch was unreachable code. Docker adds configuration keys over time, and a key this
    # gate has never heard of is exactly the one worth refusing.
    return dict(doc) if isinstance(doc, dict) else {"__unparseable__": True}


# --------------------------------------------------------------------------- adjudication
def _in_ci(doc: dict) -> bool:
    marker = (doc.get("ci_assumption") or {}).get("marker_env") or []
    return any(os.environ.get(name) for name in marker)


def steering_problems(state: dict, doc: dict) -> list:
    """Adjudicate the observed steering state against its declared disposition."""
    problems = []
    declared = doc.get("steering") or {}
    for name, value in sorted(state["environment"].items()):
        entry = declared.get(name)
        if entry is None:
            problems.append(f"{name} is set and no disposition classifies it; an unclassified "
                            "Docker steering mechanism fails closed")
            continue
        disposition = entry["disposition"]
        if disposition == FATAL_IF_PRESENT:
            problems.append(
                f"{name}={value!r} is set, and it is FATAL_IF_PRESENT under the "
                f"{doc['model']} boundary: {entry['why']}")
        elif disposition == REQUIRED_EXACT_VALUE and value != entry.get("value"):
            problems.append(f"{name}={value!r} but this boundary requires {entry.get('value')!r}")
        elif disposition == ALLOWED_VALUE_SET and value not in (entry.get("values") or []):
            problems.append(f"{name}={value!r} is outside the allowed set {entry.get('values')}")
    # A variable declared FATAL but absent from the environment is the normal, healthy case.
    return problems


def config_problems(state: dict, doc: dict) -> list:
    """Docker configuration and context state, adjudicated field by field."""
    problems = []
    rules = doc.get("config_fields") or {}
    fields = config_fields(state)
    if fields.get("__unparseable__"):
        return ["the Docker config.json exists but cannot be parsed, so its steering content is "
                "unknown; an unreadable configuration fails closed rather than being ignored"]
    for field, value in sorted(fields.items()):
        rule = rules.get(field)
        if rule is None:
            problems.append(f"config.json carries '{field}' and no rule classifies it; an "
                            "unclassified configuration field fails closed")
            continue
        if rule["disposition"] == FATAL_IF_PRESENT:
            problems.append(f"config.json sets '{field}', which is FATAL_IF_PRESENT: {rule['why']}")
    if state["config_is_symlink"] and doc.get("config_symlink_prohibited", True):
        problems.append(
            "the Docker config.json is a SYMLINK, so the file adjudicated and the file Docker reads "
            "can differ; a symlinked configuration is refused")
    for context in state["contexts"]:
        if context.get("unparseable"):
            problems.append(f"context metadata at {context['path']} cannot be parsed")
            continue
        problems.append(
            f"a Docker CONTEXT is defined ({context.get('name')!r} -> {context.get('host')!r}); "
            "under the external-CI assumption the repository environment must define no context, "
            "because a context selects the daemon endpoint independently of DOCKER_HOST")
    return problems


def flag_problems(sites: list, doc: dict) -> list:
    """Steering flags on a graded argv, and unresolved dynamic argv, both fail closed."""
    problems = []
    for site in sites:
        argv = site.get("argv") or ""
        for flag in STEERING_FLAGS:
            if re.search(rf"(?:^|\s){re.escape(flag)}(?:[=\s]|$)", argv):
                problems.append(
                    f"{site['id']}: the argv carries the steering flag {flag}, which redirects the "
                    "client independently of every environment control")
        # A variable expanded in the command word position, or a wrapper variable that could expand
        # into flags, cannot be adjudicated statically.
        for m in re.finditer(r"\$\{?(\w+)", argv):
            name = m.group(1)
            if name in (doc.get("flag_expanding_variables") or []):
                problems.append(
                    f"{site['id']}: argv expands ${name}, which the policy declares capable of "
                    "carrying steering flags; a dynamic steering flag is a blocker")
    return problems


def call_site_problems(sites: list, doc: dict) -> list:
    """Every derived call site must carry a declared trust boundary; extras and gaps both fail."""
    problems = []
    declared = {s["id"]: s for s in doc["call_sites"]}
    derived_ids = {s["id"] for s in sites}
    for site_id in sorted(derived_ids - set(declared)):
        problems.append(
            f"{site_id}: a graded Docker call site with NO trust-boundary classification. A call "
            "site may not inherit another's classification merely because both invoke Docker.")
    for site_id in sorted(set(declared) - derived_ids):
        problems.append(
            f"{site_id}: classified in the policy but no longer derived from the workflows, so the "
            "classification describes a call site that does not exist")
    return problems


def availability_problems(state: dict, doc: dict) -> list:
    """Docker absence is permitted only where no graded Docker call path is active."""
    problems = []
    in_ci = _in_ci(doc)
    if in_ci and not state["docker_on_path"]:
        problems.append(
            "the CI marker is set, so the graded Docker call path is ACTIVE, but no docker "
            "executable is resolvable. Absence must fail BEFORE the first Docker call rather than "
            "being discovered by it.")
    if not in_ci and state["docker_on_path"] and doc.get("prohibit_local_docker_execution", True):
        # Not a refusal: a developer having docker installed is normal. It is recorded so that the
        # session-finish comparison can see it appear or disappear.
        pass
    return problems


def ci_assumption_problems(state: dict, doc: dict) -> list:
    """The external assumption, machine-checked rather than asserted."""
    if doc["model"] != "MODEL_B_EXTERNAL_CI_ASSUMPTION":
        return []
    assumption = doc["ci_assumption"]
    problems = []
    if not _in_ci(doc):
        # Outside CI the assumption is not in force, and that is exactly when the local host must
        # not be able to activate a graded Docker path. Nothing to check about the runner.
        return problems
    for name in assumption["marker_env"]:
        if not os.environ.get(name):
            problems.append(
                f"the CI marker {name} is absent while other markers are set; a partially forged "
                "CI environment does not satisfy the assumption")
    expected = assumption.get("expected_marker_values") or {}
    for name, value in expected.items():
        if os.environ.get(name) != value:
            problems.append(
                f"{name}={os.environ.get(name)!r} but the assumption requires {value!r}")
    return problems


def verify(policy: dict | None = None) -> dict:
    """Establish the Docker execution boundary BEFORE any graded Docker command runs."""
    doc = policy if policy is not None else load_policy()
    state = steering_state()
    derived = derive_call_sites()
    reconciliation = reconcile_with_shared_deriver()

    problems = list(derived["problems"])
    problems += reconciliation["problems"]
    problems += call_site_problems(derived["sites"], doc)
    problems += steering_problems(state, doc)
    problems += config_problems(state, doc)
    problems += flag_problems(derived["sites"], doc)
    problems += availability_problems(state, doc)
    problems += ci_assumption_problems(state, doc)

    # GATE 4N-I28BE. The per-site enforcement consumer. Its problems join the Docker aggregate, so
    # the final assurance decision depends on every site's decision — which is precisely what
    # ADV-I28AX-ARCH-01 said was missing.
    per_site = enforce_per_site(doc, state)
    problems.extend(f"per-site: {p}" for p in per_site["problems"])

    return {
        "per_site": per_site,
        "clean": not problems,
        "problems": problems,
        "model": doc["model"],
        "assumption_version": (doc.get("ci_assumption") or {}).get("version"),
        "call_site_count": derived["count"],
        "reconciliation": reconciliation,
        "state": state,
        "policy_sha256": _digest_bytes(POLICY.read_bytes()),
    }


def snapshot() -> dict:
    """The Docker state session-finish compares against."""
    doc = load_policy()
    state = steering_state()
    return {
        "model": doc["model"],
        "assumption_version": (doc.get("ci_assumption") or {}).get("version"),
        "call_site_count": derive_call_sites()["count"],
        "docker_on_path": state["docker_on_path"],
        "docker_sha256": (_digest_bytes(Path(state["docker_on_path"]).read_bytes())
                          if state["docker_on_path"] else None),
        "path_env_sha256": hashlib.sha256(os.environ.get("PATH", "").encode()).hexdigest(),
        "steering_environment": state["environment"],
        "config_dir": state["config_dir"],
        "config_sha256": state["config_sha256"],
        "contexts": [c.get("sha256") for c in state["contexts"]],
        "ci_marker": state["ci_marker"],
        "policy_sha256": _digest_bytes(POLICY.read_bytes()),
    }


def compare(before: dict, after: dict) -> list:
    """Every Docker field that moved between configure and session finish, named individually."""
    labels = {
        "model": "the execution-boundary model",
        "assumption_version": "the external-CI assumption version",
        "call_site_count": "the Docker call-site inventory",
        "docker_on_path": "the docker executable selected from PATH",
        "docker_sha256": "the docker binary CONTENT",
        "path_env_sha256": "PATH",
        "steering_environment": "Docker steering environment",
        "config_dir": "the Docker config directory",
        "config_sha256": "the Docker config.json CONTENT",
        "contexts": "the Docker context store",
        "ci_marker": "the CI environment marker",
        "policy_sha256": "the Docker boundary policy",
    }
    return [f"{label} changed after verification: {before.get(key)!r} -> {after.get(key)!r}"
            for key, label in labels.items() if before.get(key) != after.get(key)]


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Verify the Docker execution boundary.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = verify()
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
    else:
        for problem in result["problems"]:
            print(f"  {problem}")
        print(f"  model {result['model']} | call sites {result['call_site_count']}")
        print("DOCKER EXECUTION BOUNDARY: " + ("verified" if result["clean"] else "refused"))
    return 0 if result["clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
