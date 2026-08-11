#!/usr/bin/env python3
"""Reusable publish-workflow assurance verifier and source/image binding (Gate 4N-I28BG-B1).

WHY THIS EXISTS. Gate 4N-I28BG-A established that the two publication workflows
(`reader-publish.yml`, `staging-publish.yml`) BUILD and PUSH container images and carry ZERO
authoritative assurance: the static Docker Part A model (docker_assurance_state) binds the source
universe but never the *built image* — a generated artefact the static suite cannot see. A commit
SHA binds the tree, but not the image digest, the runtime-resolved immutable tags, the build
metadata, or the build context that a publish job actually trusts. This module is the reusable
enforcement component that closes that gap. It is NOT integrated into either real workflow here;
integration is Gate 4N-I28BG-B2 (reader) and B3 (staging).

FOUR MODES, one per point in a publish job's lifecycle, each fail-closed and each emitting a
canonical, digest-bound, machine-readable record:

  A. establish            immediately after checkout: bind the checked-out source identity, the
                          workflow/job/intended-step identity, the authorization pair, and the
                          FRESHLY derived authoritative Docker-state digest into an establishment
                          record. Nothing downstream is trusted that does not descend from this.
  B. pre_build_verify     immediately before build: FRESHLY rederive the source/workflow/state
                          identity, compare to the establishment, refuse any drift, and bind the
                          build inputs (Dockerfile, context, build-args, cache, resolved tags).
  C. post_build_image_bind  immediately after build: consume STRUCTURED build metadata (buildx
                          metadata-file / equivalent — never scraped from human logs, never a live
                          Docker call), validate the built image digest, reject a missing / malformed
                          / ambiguous digest and a mutable-only tag identity, and emit the canonical
                          executed-image manifest.
  D. pre_push_verify      immediately before push: FRESHLY rederive source/workflow identity and
                          refuse source drift, workflow drift, image substitution, tag substitution,
                          authorization change, and manifest replay from another workflow / job /
                          tree / authorization window. Emits only PASS or a structured fail-closed
                          result.

WHAT IT NEVER DOES. It never invokes Docker, contacts a registry, or reaches the network — every
input is provided or read from the checked-out tree, so the whole verifier is deterministic and
offline-testable. It never treats a warning as success, never silently drops an unknown field, and
never returns zero on a load-bearing failure. It consumes a FRESHLY validated authoritative
Docker-state digest (docker_assurance_state) but never a stale cache as executed proof, and a
static state digest can never stand in for the built-image digest.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import stat as _stat

from pathlib import Path

import cache_authority as _ca
import docker_assurance_state as _das
import expiry_authorization as _ea

REPO_ROOT = Path(__file__).resolve().parents[1]

WORKFLOW_ASSURANCE_SCHEMA_VERSION = "i28bg-b1.1"
IMAGE_MANIFEST_SCHEMA_VERSION = "i28bg-b1.1"
SOURCE_MANIFEST_SCHEMA_VERSION = "i28bg-b1.1"
CANONICAL_SERIALIZATION_VERSION = "i28bg-b1.1"

# An image digest is a registry content address: the algorithm, a colon, and a lowercase hex body.
# sha256 is the only algorithm the publish path uses; a different one is refused rather than guessed.
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# A tag that names nothing durable. The publish workflows tag by the immutable commit SHA; a build
# that offers only a moving tag (latest / a branch name / an env like `staging`) is refused, because
# an image bound to a moving tag is not an image identity at all.
_MUTABLE_TAG_TOKENS = frozenset({"latest", "main", "master", "staging", "stable", "edge", "head"})

_VERIFY_PASS = "PASS"
_VERIFY_FAIL = "FAIL"

MODE_ESTABLISH = "establish"
MODE_PRE_BUILD = "pre_build_verify"
MODE_IMAGE_BIND = "post_build_image_bind"
MODE_PRE_PUSH = "pre_push_verify"

# Reader/staging are not integrated in B1. A static graph validator must not call them protected,
# and this verifier's records carry the same honest not-yet-integrated posture where relevant.
NOT_YET_INTEGRATED = "NOT_YET_INTEGRATED"

# Canonical field sets. A missing OR unknown field is a schema error, never a silent partial record.
_ESTABLISH_FIELDS = (
    "schema_version", "mode", "workflow", "authorization", "source", "docker_state",
    "expected_phase", "result", "problems", "establishment_digest",
)
_WORKFLOW_IDENTITY_FIELDS = (
    "workflow_path", "workflow_identity", "job_identity", "docker_step_identity",
)
_SOURCE_IDENTITY_FIELDS = (
    "commit_sha", "tree_identity", "source_content_digest",
)
_DOCKER_STATE_FIELDS = (
    "state_digest", "state_schema_version", "site_count", "load_bearing_count",
)
_PRE_BUILD_FIELDS = (
    "schema_version", "mode", "establishment_digest", "workflow", "source", "docker_state",
    "build_inputs", "authorization", "result", "problems", "pre_build_token",
)
_BUILD_INPUT_FIELDS = (
    "dockerfile_path", "dockerfile_digest", "context_path", "context_digest",
    "build_args", "build_secret_names", "cache_from", "cache_to", "platforms",
    "target_stage", "labels", "runtime_metadata", "resolved_tags",
)
_IMAGE_MANIFEST_FIELDS = (
    "schema_version", "canonical_serialization_version", "authorization", "source", "workflow",
    "build_inputs", "build_output", "pre_build_token", "creation_utc", "owner", "manifest_digest",
)
_BUILD_OUTPUT_FIELDS = (
    "image_digest", "image_digests", "build_metadata_digest", "builder_result_identity",
    "provenance_source_identity", "resolved_tags",
)
_PRE_PUSH_FIELDS = (
    "schema_version", "mode", "manifest_digest", "workflow", "source", "authorization",
    "intended_image_digest", "intended_tags", "result", "problems", "push_authorization_token",
)
_AUTHORIZATION_FIELDS = ("issuance", "expiry", "duration_seconds", "pair_digest")


class WorkflowAssuranceError(RuntimeError):
    """Fail closed. A record that cannot be trusted never authorizes a build or a push."""


# ============================================================ helpers
def _thaw(value):
    """A plain, comparable copy of a possibly deep-frozen structure (never mutates the original)."""
    from types import MappingProxyType
    if isinstance(value, (dict, MappingProxyType)):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(v) for v in value]
    if isinstance(value, frozenset):
        return sorted(_thaw(v) for v in value)
    return value


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(path: Path, root: Path) -> str:
    # Do NOT resolve `path`: it comes from os.walk of an already-resolved root, so it is a real
    # absolute path under `root`, and resolving it would follow a symlink to an escaping target and
    # raise. The symlink's own LOCATION is inside root; only its target escapes.
    return str(Path(path).relative_to(root.resolve()))


def _canonical(obj):
    """A deterministic, order-free canonical form; semantically equal inputs canonicalise equal."""
    return _ca.canonical(_thaw(obj))


def _digest(obj) -> str:
    return _ca.digest(_thaw(obj))


# ============================================================ authorization identity
def _authorization_identity() -> dict:
    """Bind the active pair identity from the reviewed constants, mirroring docker_assurance_state.

    active_pair() additionally runs the IAM date validation; binding identity does not need it and
    avoiding it keeps this verifier offline. The window's AUTHORISATION is enforced by the graded
    session and by expiry_authorization's own guard, not here.
    """
    iss, exp = _ea.ACTIVE_ISSUANCE_UTC, _ea.ACTIVE_EXPIRY_UTC
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    dur = int((_dt.datetime.strptime(exp, fmt).replace(tzinfo=_dt.timezone.utc)
               - _dt.datetime.strptime(iss, fmt).replace(tzinfo=_dt.timezone.utc)).total_seconds())
    ident = {"issuance": iss, "expiry": exp, "duration_seconds": dur}
    ident["pair_digest"] = _ca.digest(ident)
    return ident


def validate_authorization(auth: object) -> list:
    problems = []
    if not isinstance(auth, dict):
        return [f"authorization is {type(auth).__name__}, not a mapping"]
    if set(auth) != set(_AUTHORIZATION_FIELDS):
        problems.append("authorization identity has an unexpected field set")
        return problems
    if auth["pair_digest"] != _ca.digest({k: auth[k] for k in
                                          ("issuance", "expiry", "duration_seconds")}):
        problems.append("authorization pair_digest does not match its own fields")
    return problems


# ============================================================ authoritative Docker-state digest
def fresh_docker_state_identity() -> dict:
    """FRESHLY derive and validate the authoritative Docker-state digest. No cache substitution.

    A static state digest is bound so the establishment records WHICH Part A universe was in force;
    it can never stand in for the built-image digest (that is bound separately, post-build).
    """
    state = _das.fresh_state()
    problems = _das.validate_state(state)
    if problems:
        raise WorkflowAssuranceError(
            "the authoritative Docker state is invalid; a workflow cannot be established over an "
            "unvalidatable Part A baseline: " + "; ".join(problems))
    return {
        "state_digest": _das.state_digest(state),
        "state_schema_version": state["schema_version"],
        "site_count": state["universe"]["site_count"],
        "load_bearing_count": state["universe"]["load_bearing_count"],
    }


def validate_docker_state(ds: object) -> list:
    problems = []
    if not isinstance(ds, dict):
        return [f"docker_state is {type(ds).__name__}, not a mapping"]
    if set(ds) != set(_DOCKER_STATE_FIELDS):
        problems.append("docker_state identity has an unexpected field set")
        return problems
    if not _DIGEST_HEX.match(str(ds["state_digest"])):
        problems.append("docker_state.state_digest is not a hex digest")
    if int(ds.get("load_bearing_count") or 0) <= 0:
        problems.append("docker_state binds no load-bearing sites")
    return problems


_DIGEST_HEX = re.compile(r"^[0-9a-f]{64}$")


# ============================================================ source-content manifest
def _default_context_exclusions() -> tuple:
    # Declared exclusions, never implicit. .git is not build context; VCS and editor noise are not
    # content the image is built from. Anything else is INCLUDED, so an added file is detected.
    return (".git", "__pycache__", ".DS_Store")


def _posix_rel(path: Path, base: Path) -> str:
    return str(Path(path).relative_to(base)).replace(os.sep, "/")


def _load_dockerignore(context_dir: Path) -> list:
    """The .dockerignore patterns for a build context (empty if absent). Comments and blanks are
    dropped; a trailing slash is normalised away and recorded structurally."""
    p = context_dir / ".dockerignore"
    if not p.is_file():
        return []
    patterns = []
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        if neg:
            line = line[1:]
        line = line.lstrip("/").rstrip("/")
        if line:
            patterns.append({"pattern": line, "negate": neg})
    return patterns


def _dockerignored(relpath: str, patterns: list, *, is_dir: bool) -> bool:
    """Whether a context-relative POSIX path is excluded by .dockerignore. Docker uses
    filepath.Match semantics where `*` does not cross `/`; a directory pattern excludes everything
    beneath it. A later negation (`!pat`) re-includes. Conservative: an unmatched path is INCLUDED,
    so a file the image ships is never silently dropped from the binding."""
    import fnmatch
    ignored = False
    segments = relpath.split("/")
    for entry in patterns:
        pat = entry["pattern"]
        matched = (
            fnmatch.fnmatch(relpath, pat)
            or fnmatch.fnmatch(segments[-1], pat)                 # basename match (e.g. *.env)
            or relpath == pat
            or relpath.startswith(pat + "/")                      # directory-prefix exclusion
            or any(fnmatch.fnmatch(seg, pat) for seg in segments)  # a matched dir component
        )
        if matched:
            ignored = not entry["negate"]
    return ignored


def source_content_manifest(root: Path, spec: dict) -> dict:
    """Derive the exact source universe bound before a build, by CONTENT not by commit SHA.

    `spec` declares the load-bearing source set:
      workflow_path, dockerfile_path, context_path, script_paths, generated_input_paths,
      policy_paths, authorization_source, config_paths, actions_metadata (values only, no secrets),
      commit_sha (recorded, never the identity), context_exclusions (optional; defaults declared).

    A symlink whose target escapes `root` is refused. A missing declared file is refused. The
    context directory is walked recursively into a sorted (path, sha256, mode) list, so an added,
    removed, moved, replaced, or mode-changed file moves the digest even when the commit SHA does not.
    """
    root = root.resolve()
    problems: list = []
    files: dict = {}

    def bind_file(label: str, relpath: str):
        p = (root / relpath)
        if p.is_symlink():
            target = p.resolve()
            if not str(target).startswith(str(root) + os.sep) and target != root:
                problems.append(f"{label} {relpath!r} is a symlink escaping the source root")
                return
        if not p.is_file():
            problems.append(f"{label} {relpath!r} is missing or not a regular file")
            return
        files[label] = {"path": relpath, "sha256": _sha256_file(p)}

    if spec.get("workflow_path"):
        bind_file("workflow", spec["workflow_path"])
    if spec.get("dockerfile_path"):
        bind_file("dockerfile", spec["dockerfile_path"])
    if spec.get("authorization_source"):
        bind_file("authorization_source", spec["authorization_source"])
    for i, sp in enumerate(spec.get("script_paths") or []):
        bind_file(f"script:{sp}", sp)
    for gp in spec.get("generated_input_paths") or []:
        bind_file(f"generated:{gp}", gp)
    for pp in spec.get("policy_paths") or []:
        bind_file(f"policy:{pp}", pp)
    for cp in spec.get("config_paths") or []:
        bind_file(f"config:{cp}", cp)

    exclusions = tuple(spec.get("context_exclusions") or _default_context_exclusions())
    context = None
    ctx_rel = spec.get("context_path")
    if ctx_rel:
        ctx = (root / ctx_rel).resolve()
        if not str(ctx).startswith(str(root)):
            problems.append(f"context_path {ctx_rel!r} escapes the source root")
        elif not ctx.is_dir():
            problems.append(f"context_path {ctx_rel!r} is not a directory")
        else:
            # GATE 4N-I28BG-B3 finding BG-B3-FIND-01. Bind exactly the content Docker BUILDS FROM:
            # honour the context's .dockerignore. Without it the walk bound (and symlink-refused)
            # paths Docker excludes from the build context — e.g. a local `.venv/bin/python*` symlink
            # to the system interpreter, present in a working tree but absent from a CI checkout and
            # never shipped in a layer. Binding non-context paths is both a false symlink-escape and
            # a binding of content the image does not contain.
            dockerignore = _load_dockerignore(ctx)
            entries = []
            for dirpath, dirnames, filenames in os.walk(ctx):
                pruned = []
                for d in sorted(dirnames):
                    drel = _posix_rel(Path(dirpath) / d, ctx)
                    if d in exclusions or _dockerignored(drel, dockerignore, is_dir=True):
                        continue
                    pruned.append(d)
                dirnames[:] = pruned
                for name in sorted(filenames):
                    if name in exclusions:
                        continue
                    fp = Path(dirpath) / name
                    frel = _posix_rel(fp, ctx)
                    if _dockerignored(frel, dockerignore, is_dir=False):
                        continue
                    if fp.is_symlink():
                        tgt = fp.resolve()
                        if not str(tgt).startswith(str(root) + os.sep) and tgt != root:
                            problems.append(
                                f"context file {_rel(fp, root)!r} is a symlink escaping the root")
                            continue
                    if not fp.is_file():
                        continue
                    st = fp.lstat()
                    entries.append({"path": _rel(fp, root),
                                    "sha256": _sha256_file(fp),
                                    "mode": oct(_stat.S_IMODE(st.st_mode))})
            entries.sort(key=lambda e: e["path"])
            context = {"path": ctx_rel, "exclusions": list(exclusions),
                       "dockerignore_patterns": dockerignore,
                       "files": entries, "file_count": len(entries)}

    manifest = {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "commit_sha": spec.get("commit_sha"),
        "files": files,
        "context": context,
        "actions_metadata": dict(spec.get("actions_metadata") or {}),
        "problems": problems,
    }
    manifest["source_content_digest"] = _digest(
        {k: manifest[k] for k in ("schema_version", "files", "context", "actions_metadata")})
    return manifest


def validate_source_manifest(manifest: object) -> list:
    problems = []
    if not isinstance(manifest, dict):
        return [f"source manifest is {type(manifest).__name__}, not a mapping"]
    if manifest.get("schema_version") != SOURCE_MANIFEST_SCHEMA_VERSION:
        return [f"stale or missing source manifest schema {manifest.get('schema_version')!r}"]
    if manifest.get("problems"):
        problems.extend(f"source: {p}" for p in manifest["problems"])
    if not manifest.get("files"):
        problems.append("source manifest binds no files")
    if "workflow" not in (manifest.get("files") or {}):
        problems.append("source manifest does not bind the workflow file")
    recomputed = _digest({k: manifest.get(k) for k in
                          ("schema_version", "files", "context", "actions_metadata")})
    if manifest.get("source_content_digest") != recomputed:
        problems.append("source_content_digest does not match the manifest content")
    return problems


def source_content_digest(manifest: dict) -> str:
    return manifest["source_content_digest"]


# ============================================================ workflow identity
def validate_workflow_identity(wf: object) -> list:
    problems = []
    if not isinstance(wf, dict):
        return [f"workflow identity is {type(wf).__name__}, not a mapping"]
    if set(wf) != set(_WORKFLOW_IDENTITY_FIELDS):
        problems.append("workflow identity has an unexpected field set")
        return problems
    for f in _WORKFLOW_IDENTITY_FIELDS:
        if not wf.get(f):
            problems.append(f"workflow identity component {f!r} is empty")
    return problems


# ============================================================ MODE A — establish
def establish(*, workflow: dict, source_manifest: dict, authorization: dict | None = None,
              expected_phase: str, commit_sha: str, tree_identity: str,
              expected_docker_state_digest: str | None = None) -> dict:
    """Bind the checked-out source, workflow identity, authorization, and FRESH Docker-state digest.

    Returns a canonical establishment record with an establishment_digest. `result` is PASS only
    when every bound identity validates; otherwise FAIL with explicit problems and no usable digest.
    """
    problems: list = []
    auth = authorization or _authorization_identity()
    problems.extend(validate_authorization(auth))
    problems.extend(validate_workflow_identity(workflow))
    problems.extend(validate_source_manifest(source_manifest))

    try:
        ds = fresh_docker_state_identity()
    except WorkflowAssuranceError as exc:
        ds = None
        problems.append(str(exc))
    if ds is not None:
        problems.extend(validate_docker_state(ds))
        if expected_docker_state_digest is not None and \
                ds["state_digest"] != expected_docker_state_digest:
            problems.append("the freshly derived Docker-state digest does not match the expected "
                            "digest; the Part A baseline drifted since it was recorded")

    source = {
        "commit_sha": commit_sha,
        "tree_identity": tree_identity,
        "source_content_digest": source_manifest.get("source_content_digest"),
    }
    if set(source) != set(_SOURCE_IDENTITY_FIELDS) or not all(source.values()):
        problems.append("source identity is incomplete")

    record = {
        "schema_version": WORKFLOW_ASSURANCE_SCHEMA_VERSION,
        "mode": MODE_ESTABLISH,
        "workflow": dict(workflow),
        "authorization": dict(auth),
        "source": source,
        "docker_state": ds,
        "expected_phase": expected_phase,
        "result": _VERIFY_FAIL if problems else _VERIFY_PASS,
        "problems": problems,
    }
    record["establishment_digest"] = _digest(
        {k: record[k] for k in ("schema_version", "mode", "workflow", "authorization", "source",
                                "docker_state", "expected_phase")})
    return record


def validate_establishment(record: object) -> list:
    problems = []
    if not isinstance(record, dict):
        return [f"establishment record is {type(record).__name__}, not a mapping"]
    if record.get("schema_version") != WORKFLOW_ASSURANCE_SCHEMA_VERSION:
        return [f"stale establishment schema {record.get('schema_version')!r}"]
    if record.get("mode") != MODE_ESTABLISH:
        problems.append(f"record mode is {record.get('mode')!r}, not {MODE_ESTABLISH!r}")
    unknown = sorted(set(record) - set(_ESTABLISH_FIELDS))
    if unknown:
        problems.append(f"unknown establishment field(s) {unknown}")
    missing = sorted(set(_ESTABLISH_FIELDS) - set(record))
    if missing:
        problems.append(f"missing establishment field(s) {missing}")
    if problems:
        return problems
    if record["result"] != _VERIFY_PASS:
        problems.append(f"establishment result is {record['result']!r}, not PASS")
    recomputed = _digest({k: record[k] for k in
                          ("schema_version", "mode", "workflow", "authorization", "source",
                           "docker_state", "expected_phase")})
    if record["establishment_digest"] != recomputed:
        problems.append("establishment_digest does not match the record content")
    return problems


# ============================================================ MODE B — pre-build verify
def pre_build_verify(*, establishment: dict, fresh_source_manifest: dict, workflow: dict,
                     build_inputs: dict, authorization: dict | None = None,
                     fresh_commit_sha: str, fresh_tree_identity: str) -> dict:
    """FRESHLY rederive identity, refuse drift from the establishment, and bind the build inputs."""
    problems: list = []
    est_problems = validate_establishment(establishment)
    if est_problems:
        problems.extend(f"establishment: {p}" for p in est_problems)

    auth = authorization or _authorization_identity()
    problems.extend(validate_authorization(auth))
    problems.extend(f"fresh source: {p}" for p in validate_source_manifest(fresh_source_manifest))
    problems.extend(validate_workflow_identity(workflow))
    problems.extend(validate_build_inputs(build_inputs))

    # Drift refusals — the fresh identity must equal what was established.
    if not est_problems:
        if _canonical(workflow) != _canonical(establishment["workflow"]):
            problems.append("workflow identity drifted between establishment and pre-build")
        if auth["pair_digest"] != establishment["authorization"]["pair_digest"]:
            problems.append("authorization changed between establishment and pre-build")
        est_src = establishment["source"]
        if fresh_source_manifest.get("source_content_digest") != est_src.get("source_content_digest"):
            problems.append("source content changed after establishment (pre-build)")
        if fresh_commit_sha != est_src.get("commit_sha"):
            problems.append("commit SHA changed after establishment (pre-build)")
        if fresh_tree_identity != est_src.get("tree_identity"):
            problems.append("tree identity changed after establishment (pre-build)")
        # A static state digest must still validate freshly; the Part A baseline may not drift.
        try:
            ds_now = fresh_docker_state_identity()
            if establishment["docker_state"] and \
                    ds_now["state_digest"] != establishment["docker_state"]["state_digest"]:
                problems.append("authoritative Docker-state digest drifted after establishment")
        except WorkflowAssuranceError as exc:
            problems.append(str(exc))

    record = {
        "schema_version": WORKFLOW_ASSURANCE_SCHEMA_VERSION,
        "mode": MODE_PRE_BUILD,
        "establishment_digest": establishment.get("establishment_digest"),
        "workflow": dict(workflow),
        "source": {"commit_sha": fresh_commit_sha, "tree_identity": fresh_tree_identity,
                   "source_content_digest": fresh_source_manifest.get("source_content_digest")},
        "docker_state": establishment.get("docker_state"),
        "build_inputs": dict(build_inputs),
        "authorization": dict(auth),
        "result": _VERIFY_FAIL if problems else _VERIFY_PASS,
        "problems": problems,
    }
    record["pre_build_token"] = _digest(
        {k: record[k] for k in ("schema_version", "mode", "establishment_digest", "workflow",
                                "source", "build_inputs", "authorization")})
    return record


def validate_build_inputs(bi: object) -> list:
    problems = []
    if not isinstance(bi, dict):
        return [f"build_inputs is {type(bi).__name__}, not a mapping"]
    unknown = sorted(set(bi) - set(_BUILD_INPUT_FIELDS))
    if unknown:
        problems.append(f"unknown build-input field(s) {unknown}")
    missing = sorted(set(_BUILD_INPUT_FIELDS) - set(bi))
    if missing:
        problems.append(f"missing build-input field(s) {missing}")
    if problems:
        return problems
    if not bi.get("dockerfile_path") or not _DIGEST_HEX.match(str(bi.get("dockerfile_digest"))):
        problems.append("build inputs bind no valid Dockerfile identity")
    if not bi.get("resolved_tags"):
        problems.append("build inputs bind no resolved tags")
    else:
        problems.extend(_tag_problems(bi["resolved_tags"]))
    # Build secrets: names only, never values.
    for name in bi.get("build_secret_names") or []:
        if not isinstance(name, str):
            problems.append("build_secret_names must be names, not values")
            break
    return problems


def _tag_problems(tags) -> list:
    problems = []
    if not isinstance(tags, (list, tuple)) or not tags:
        return ["resolved_tags is empty or not a list"]
    for t in tags:
        base = str(t).rsplit(":", 1)[-1].strip().lower()
        if base in _MUTABLE_TAG_TOKENS:
            problems.append(f"tag {t!r} is a mutable-only tag; an immutable (commit-bound) tag is "
                            "required for a publishable image identity")
    return problems


def validate_pre_build(record: object) -> list:
    problems = []
    if not isinstance(record, dict):
        return [f"pre-build record is {type(record).__name__}, not a mapping"]
    if record.get("schema_version") != WORKFLOW_ASSURANCE_SCHEMA_VERSION:
        return [f"stale pre-build schema {record.get('schema_version')!r}"]
    if record.get("mode") != MODE_PRE_BUILD:
        problems.append(f"record mode is {record.get('mode')!r}, not {MODE_PRE_BUILD!r}")
    unknown = sorted(set(record) - set(_PRE_BUILD_FIELDS))
    if unknown:
        problems.append(f"unknown pre-build field(s) {unknown}")
    missing = sorted(set(_PRE_BUILD_FIELDS) - set(record))
    if missing:
        problems.append(f"missing pre-build field(s) {missing}")
    if problems:
        return problems
    if record["result"] != _VERIFY_PASS:
        problems.append(f"pre-build result is {record['result']!r}, not PASS")
    recomputed = _digest({k: record[k] for k in
                          ("schema_version", "mode", "establishment_digest", "workflow",
                           "source", "build_inputs", "authorization")})
    if record["pre_build_token"] != recomputed:
        problems.append("pre_build_token does not match the record content")
    return problems


# ============================================================ MODE C — post-build image bind
def _digests_from_metadata(metadata: dict) -> list:
    """Extract image digests from STRUCTURED buildx metadata, deterministically.

    Accepts the buildx `--metadata-file` shape ({"containerimage.digest": "sha256:..."} and/or
    {"containerimage.descriptor": {"digest": "sha256:..."}}) and an explicit {"image_digests": [...]}
    form. Never parses human logs. Returns the sorted unique digest list it found.
    """
    found = set()
    if not isinstance(metadata, dict):
        return []
    d = metadata.get("containerimage.digest")
    if isinstance(d, str):
        found.add(d)
    desc = metadata.get("containerimage.descriptor")
    if isinstance(desc, dict) and isinstance(desc.get("digest"), str):
        found.add(desc["digest"])
    for x in metadata.get("image_digests") or []:
        if isinstance(x, str):
            found.add(x)
    return sorted(found)


def post_build_image_bind(*, pre_build_record: dict, build_metadata: dict, resolved_tags: list,
                          allow_multiple_digests: bool = False, owner: str = "workflow_assurance",
                          build_context_digest: str | None = None,
                          dockerfile_digest: str | None = None) -> dict:
    """Validate the built image digest and emit the canonical executed-image manifest.

    Rejects a missing / malformed / ambiguous digest (unless multi-platform semantics are explicitly
    permitted) and a mutable-only tag. Binds the built digest to the pre-build record so a later
    push cannot substitute an image the build did not produce.
    """
    problems: list = []
    pb_problems = validate_pre_build(pre_build_record)
    if pb_problems:
        problems.extend(f"pre-build: {p}" for p in pb_problems)

    digests = _digests_from_metadata(build_metadata)
    for d in digests:
        if not _DIGEST_RE.match(d):
            problems.append(f"built image digest {d!r} is malformed")
    if not digests:
        problems.append("no image digest was produced by the build; a build without a bound digest "
                        "cannot be published")
    if len(digests) > 1 and not allow_multiple_digests:
        problems.append(f"the build produced multiple digests {digests}; a single-platform publish "
                        "must bind exactly one, and multi-platform semantics were not permitted")
    problems.extend(_tag_problems(resolved_tags))

    primary = digests[0] if digests else None
    build_output = {
        "image_digest": primary,
        "image_digests": digests,
        "build_metadata_digest": _digest(build_metadata),
        "builder_result_identity": build_metadata.get("buildx.build.ref"),
        "provenance_source_identity": build_metadata.get("containerimage.config.digest"),
        "resolved_tags": list(resolved_tags),
    }

    bi = pre_build_record.get("build_inputs") if isinstance(pre_build_record, dict) else {}
    if dockerfile_digest is not None and bi and dockerfile_digest != bi.get("dockerfile_digest"):
        problems.append("post-build Dockerfile digest does not match the pre-build binding")
    if build_context_digest is not None and bi and build_context_digest != bi.get("context_digest"):
        problems.append("post-build context digest does not match the pre-build binding")

    manifest = {
        "schema_version": IMAGE_MANIFEST_SCHEMA_VERSION,
        "canonical_serialization_version": CANONICAL_SERIALIZATION_VERSION,
        "authorization": _thaw(pre_build_record.get("authorization")) if isinstance(
            pre_build_record, dict) else None,
        "source": _thaw(pre_build_record.get("source")) if isinstance(pre_build_record, dict)
        else None,
        "workflow": _thaw(pre_build_record.get("workflow")) if isinstance(pre_build_record, dict)
        else None,
        "build_inputs": _thaw(bi),
        "build_output": build_output,
        "pre_build_token": pre_build_record.get("pre_build_token") if isinstance(
            pre_build_record, dict) else None,
        "creation_utc": _now_utc(),
        "owner": owner,
    }
    manifest["manifest_digest"] = _digest(
        {k: manifest[k] for k in ("schema_version", "canonical_serialization_version",
                                  "authorization", "source", "workflow", "build_inputs",
                                  "build_output", "pre_build_token")})
    manifest["_problems"] = problems
    return manifest


def validate_image_manifest(manifest: object) -> list:
    problems = []
    if not isinstance(manifest, dict):
        return [f"image manifest is {type(manifest).__name__}, not a mapping"]
    if manifest.get("schema_version") != IMAGE_MANIFEST_SCHEMA_VERSION:
        return [f"stale image manifest schema {manifest.get('schema_version')!r}"]
    known = set(_IMAGE_MANIFEST_FIELDS) | {"_problems"}
    unknown = sorted(set(manifest) - known)
    if unknown:
        problems.append(f"unknown image-manifest field(s) {unknown}")
    missing = sorted(set(_IMAGE_MANIFEST_FIELDS) - set(manifest))
    if missing:
        problems.append(f"missing image-manifest field(s) {missing}")
    if problems:
        return problems
    if manifest.get("_problems"):
        problems.extend(f"image bind: {p}" for p in manifest["_problems"])
    out = manifest["build_output"]
    if not isinstance(out, dict) or set(out) != set(_BUILD_OUTPUT_FIELDS):
        problems.append("build_output has an unexpected field set")
        return problems
    if not out.get("image_digest") or not _DIGEST_RE.match(str(out["image_digest"])):
        problems.append("image manifest binds no valid primary image digest")
    if not out.get("resolved_tags"):
        problems.append("image manifest binds no resolved tags")
    else:
        problems.extend(_tag_problems(out["resolved_tags"]))
    recomputed = _digest({k: manifest[k] for k in
                          ("schema_version", "canonical_serialization_version", "authorization",
                           "source", "workflow", "build_inputs", "build_output", "pre_build_token")})
    if manifest.get("manifest_digest") != recomputed:
        problems.append("manifest_digest does not match the manifest content")
    return problems


def image_manifest_digest(manifest: dict) -> str:
    return manifest["manifest_digest"]


# ============================================================ MODE D — pre-push verify
def pre_push_verify(*, image_manifest: dict, fresh_source_manifest: dict, workflow: dict,
                    intended_image_digest: str, intended_tags: list,
                    authorization: dict | None = None, fresh_commit_sha: str,
                    fresh_tree_identity: str) -> dict:
    """Refuse source drift, workflow drift, image substitution, tag substitution, auth change, and
    manifest replay from another workflow / job / tree / authorization window. PASS or fail-closed.
    """
    problems: list = []
    im_problems = validate_image_manifest(image_manifest)
    if im_problems:
        problems.extend(f"image manifest: {p}" for p in im_problems)

    auth = authorization or _authorization_identity()
    problems.extend(validate_authorization(auth))
    problems.extend(f"fresh source: {p}" for p in validate_source_manifest(fresh_source_manifest))
    problems.extend(validate_workflow_identity(workflow))

    if not im_problems:
        bound_out = image_manifest["build_output"]
        bound_src = image_manifest["source"] or {}
        bound_wf = image_manifest["workflow"] or {}
        bound_auth = image_manifest["authorization"] or {}
        # Replay across workflow / job / step / tree / authorization window.
        if _canonical(workflow) != _canonical(bound_wf):
            problems.append("manifest replay: the bound workflow/job/step identity is not this "
                            "workflow's identity")
        if auth.get("pair_digest") != bound_auth.get("pair_digest"):
            problems.append("manifest replay: the bound authorization window is not the current one")
        if fresh_source_manifest.get("source_content_digest") != \
                bound_src.get("source_content_digest"):
            problems.append("source content changed after the image was bound (pre-push)")
        if fresh_commit_sha != bound_src.get("commit_sha"):
            problems.append("commit SHA changed after the image was bound (pre-push)")
        if fresh_tree_identity != bound_src.get("tree_identity"):
            problems.append("tree identity changed after the image was bound (pre-push)")
        # Image / tag substitution.
        if intended_image_digest != bound_out.get("image_digest"):
            problems.append("image substitution: the digest intended for push is not the built, "
                            "bound image digest")
        if not _DIGEST_RE.match(str(intended_image_digest)):
            problems.append("the digest intended for push is malformed")
        if sorted(map(str, intended_tags)) != sorted(map(str, bound_out.get("resolved_tags") or [])):
            problems.append("tag substitution: the tags intended for push are not the bound tags")
        problems.extend(_tag_problems(intended_tags))

    record = {
        "schema_version": WORKFLOW_ASSURANCE_SCHEMA_VERSION,
        "mode": MODE_PRE_PUSH,
        "manifest_digest": image_manifest.get("manifest_digest"),
        "workflow": dict(workflow),
        "source": {"commit_sha": fresh_commit_sha, "tree_identity": fresh_tree_identity,
                   "source_content_digest": fresh_source_manifest.get("source_content_digest")},
        "authorization": dict(auth),
        "intended_image_digest": intended_image_digest,
        "intended_tags": list(intended_tags),
        "result": _VERIFY_FAIL if problems else _VERIFY_PASS,
        "problems": problems,
    }
    record["push_authorization_token"] = _digest(
        {k: record[k] for k in ("schema_version", "mode", "manifest_digest", "workflow", "source",
                                "authorization", "intended_image_digest", "intended_tags")})
    return record


def validate_pre_push(record: object) -> list:
    problems = []
    if not isinstance(record, dict):
        return [f"pre-push record is {type(record).__name__}, not a mapping"]
    if record.get("schema_version") != WORKFLOW_ASSURANCE_SCHEMA_VERSION:
        return [f"stale pre-push schema {record.get('schema_version')!r}"]
    if record.get("mode") != MODE_PRE_PUSH:
        problems.append(f"record mode is {record.get('mode')!r}, not {MODE_PRE_PUSH!r}")
    unknown = sorted(set(record) - set(_PRE_PUSH_FIELDS))
    if unknown:
        problems.append(f"unknown pre-push field(s) {unknown}")
    missing = sorted(set(_PRE_PUSH_FIELDS) - set(record))
    if missing:
        problems.append(f"missing pre-push field(s) {missing}")
    if problems:
        return problems
    if record["result"] != _VERIFY_PASS:
        problems.append(f"pre-push result is {record['result']!r}, not PASS")
    recomputed = _digest({k: record[k] for k in
                          ("schema_version", "mode", "manifest_digest", "workflow", "source",
                           "authorization", "intended_image_digest", "intended_tags")})
    if record["push_authorization_token"] != recomputed:
        problems.append("push_authorization_token does not match the record content")
    return problems


# ============================================================ result adjudication
def record_passes(record: dict) -> bool:
    """A record authorizes its step only when result is exactly PASS with no problems."""
    return isinstance(record, dict) and record.get("result") == _VERIFY_PASS \
        and not record.get("problems")


# ============================================================ CLI
def _load_json(path: str):
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Reusable publish-workflow assurance verifier.")
    sub = ap.add_subparsers(dest="mode", required=True)
    for m in (MODE_ESTABLISH, MODE_PRE_BUILD, MODE_IMAGE_BIND, MODE_PRE_PUSH):
        p = sub.add_parser(m)
        p.add_argument("--params", required=True, help="path to a JSON parameter file")
        p.add_argument("--out", help="path to write the canonical record JSON")

    args = ap.parse_args(argv)
    try:
        params = _load_json(args.params)
        if args.mode == MODE_ESTABLISH:
            src = source_content_manifest(REPO_ROOT, params["source_spec"])
            record = establish(workflow=params["workflow"], source_manifest=src,
                               authorization=params.get("authorization"),
                               expected_phase=params["expected_phase"],
                               commit_sha=params["commit_sha"],
                               tree_identity=params["tree_identity"],
                               expected_docker_state_digest=params.get(
                                   "expected_docker_state_digest"))
            ok = record_passes(record) and not validate_establishment(record)
        elif args.mode == MODE_PRE_BUILD:
            src = source_content_manifest(REPO_ROOT, params["source_spec"])
            record = pre_build_verify(establishment=params["establishment"],
                                      fresh_source_manifest=src, workflow=params["workflow"],
                                      build_inputs=params["build_inputs"],
                                      authorization=params.get("authorization"),
                                      fresh_commit_sha=params["commit_sha"],
                                      fresh_tree_identity=params["tree_identity"])
            ok = record_passes(record) and not validate_pre_build(record)
        elif args.mode == MODE_IMAGE_BIND:
            record = post_build_image_bind(pre_build_record=params["pre_build_record"],
                                           build_metadata=params["build_metadata"],
                                           resolved_tags=params["resolved_tags"],
                                           allow_multiple_digests=params.get(
                                               "allow_multiple_digests", False))
            ok = not validate_image_manifest(record)
        else:
            src = source_content_manifest(REPO_ROOT, params["source_spec"])
            record = pre_push_verify(image_manifest=params["image_manifest"],
                                     fresh_source_manifest=src, workflow=params["workflow"],
                                     intended_image_digest=params["intended_image_digest"],
                                     intended_tags=params["intended_tags"],
                                     authorization=params.get("authorization"),
                                     fresh_commit_sha=params["commit_sha"],
                                     fresh_tree_identity=params["tree_identity"])
            ok = record_passes(record) and not validate_pre_push(record)
    except (WorkflowAssuranceError, KeyError, ValueError) as exc:
        print(f"  {type(exc).__name__}: {exc}")
        print("WORKFLOW ASSURANCE: refused")
        return 2

    out = json.dumps(_thaw(record), indent=1, sort_keys=True, default=str)
    if args.out:
        Path(args.out).write_text(out + "\n", encoding="utf-8")
    print(out)
    print("WORKFLOW ASSURANCE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
