#!/usr/bin/env python3
"""Identity of the npm → Node execution chain (Gate 4N-I28AS).

THE DEFECT THIS CLOSES. Gate 4N-I28AP finding ADV-I28AP-02. `external_executable_trust` authorized
npm by a path PREFIX: when the resolved path started with an approved installation root such as
`~/.nvm/versions/node/`, the approved-path membership test was skipped entirely (`approved = []`).
Anything under that root was therefore accepted, and the ATTACKER'S digest was recorded as the
bound content. Measured on this host before the fix, all of the following were accepted with
executable trust clean, the bootstrap established, and session-finish reverification clean:

    a four-line shell script at $NVM_DIR/versions/node/<attacker-dir>/bin/npm
    the same, printing a genuine-looking `10.8.2` for --version
    the GENUINE npm-cli.js copied into an attacker-created version directory
    an npm symlink redirected to an attacker CLI entrypoint
    an attacker package.json claiming the genuine npm identity
    an attacker `node` binary beside the attacker npm

and the attacker npm then EXECUTED. The identical fake placed OUTSIDE any approved prefix was
refused — which is what proves the prefix allowance was the whole basis of acceptance.

WHY A PREFIX WAS EVER USED. A version manager embeds a version number in the path, so no fixed
path set names it on every machine. That reasoning was sound; the conclusion was not. The answer is
not a looser path rule but a different KIND of evidence: bind the installation TREE and the
Node↔npm RELATIONSHIP, both of which an attacker directory cannot fabricate by being in the right
place.

WHAT IS BOUND. The chain is resolved and every load-bearing component is bound before npm runs:

    PATH-selected npm -> symlink chain -> canonical object -> object type -> shebang
      -> canonical Node executable -> npm CLI JavaScript entrypoint -> npm package root
      -> package.json -> installation-family identity -> content and mode

THE DECISIVE EVIDENCE FOR NVM. A genuine Node distribution carries `include/node/node_version.h`,
which DECLARES its own version, and the directory it sits in is named for that version. Binding the
declared version to the directory name and to the npm package root means an attacker-created
version directory fails on a file it has no reason to have and cannot make self-consistent by
placement. Being beneath `$NVM_DIR/versions/node/` proves nothing and is treated as proving nothing.

WHAT THIS MODULE DOES NOT CLAIM. It establishes the identity of the npm and Node BINARIES. It does
NOT vouch for the packages npm installs or the lifecycle scripts they run — see the lifecycle
boundary in the policy, which states that separation rather than blurring it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = REPO_ROOT / "tests" / "fixtures" / "npm-authority-policy.json"

# The authority models. A bare prefix rule is deliberately ABSENT: it is the defect.
CURRENT_NODE_DISTRIBUTION_BOUND = "CURRENT_NODE_DISTRIBUTION_BOUND"
APPROVED_PACKAGE_MANAGER_INSTALLATION_BOUND = "APPROVED_PACKAGE_MANAGER_INSTALLATION_BOUND"
APPROVED_EXACT_PATH_AND_CONTENT_BOUND = "APPROVED_EXACT_PATH_AND_CONTENT_BOUND"
APPROVED_DIRECTORY_TREE_AND_PROVENANCE_BOUND = "APPROVED_DIRECTORY_TREE_AND_PROVENANCE_BOUND"
EXTERNAL_CI_TOOLCHAIN_ASSUMPTION = "EXTERNAL_CI_TOOLCHAIN_ASSUMPTION"
PROHIBITED = "PROHIBITED"

AUTHORITY_MODELS = frozenset({
    CURRENT_NODE_DISTRIBUTION_BOUND, APPROVED_PACKAGE_MANAGER_INSTALLATION_BOUND,
    APPROVED_EXACT_PATH_AND_CONTENT_BOUND, APPROVED_DIRECTORY_TREE_AND_PROVENANCE_BOUND,
    EXTERNAL_CI_TOOLCHAIN_ASSUMPTION, PROHIBITED,
})

# Configuration dispositions, per Gate 4N-I28AS section 11.
FATAL_IF_SET = "FATAL_IF_SET"
REQUIRED_EXACT_VALUE = "REQUIRED_EXACT_VALUE"
ALLOWED_VALUE_SET = "ALLOWED_VALUE_SET"
CONTENT_BOUND = "CONTENT_BOUND"
NEUTRALIZED_BY_EXPLICIT_ARGV = "NEUTRALIZED_BY_EXPLICIT_ARGV"
EXTERNAL_CI_ASSUMPTION = "EXTERNAL_CI_ASSUMPTION"
IRRELEVANT_TO_ACTUAL_CALL = "IRRELEVANT_TO_ACTUAL_CALL"

DISPOSITIONS = frozenset({
    FATAL_IF_SET, REQUIRED_EXACT_VALUE, ALLOWED_VALUE_SET, CONTENT_BOUND,
    NEUTRALIZED_BY_EXPLICIT_ARGV, EXTERNAL_CI_ASSUMPTION, IRRELEVANT_TO_ACTUAL_CALL,
})

SEMVER_DIR = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
NATIVE_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
                b"\xcf\xfa\xed\xfe", b"\x7fELF")


class NpmAuthorityError(RuntimeError):
    """Fail closed. An npm whose identity cannot be established is never trusted."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_policy(path: Path | None = None) -> dict:
    p = path or POLICY
    if not p.is_file():
        raise NpmAuthorityError(
            f"the npm-authority policy is missing at {p}. Without it this control would have to "
            "guess which installations are legitimate, and guessing is what ADV-I28AP-02 "
            "exploited.")
    doc = json.loads(p.read_text(encoding="utf-8"))
    families = doc.get("installation_families")
    if not isinstance(families, dict) or not families:
        raise NpmAuthorityError("the npm-authority policy declares no installation family; an "
                                "empty policy would authorize vacuously")
    for name, entry in families.items():
        model = entry.get("authority_model")
        if model not in AUTHORITY_MODELS:
            raise NpmAuthorityError(
                f"{name}: authority model {model!r} is not one of {sorted(AUTHORITY_MODELS)}. A "
                "bare path-prefix rule is deliberately not among them — it is ADV-I28AP-02.")
        if not entry.get("why"):
            raise NpmAuthorityError(f"{name}: an authority model must state why it holds")
    configuration = doc.get("configuration") or {}
    # `files` is a GROUP of file-backed mechanisms, not a variable. Its members carry the
    # dispositions; validating the group itself would demand a disposition for a container.
    groups = {"files": configuration.get("files") or {}}
    for var, entry in configuration.items():
        if var in groups:
            continue
        if entry.get("disposition") not in DISPOSITIONS:
            raise NpmAuthorityError(
                f"{var}: disposition {entry.get('disposition')!r} is not one of "
                f"{sorted(DISPOSITIONS)}. An unclassified configuration mechanism fails closed.")
    for group, members in groups.items():
        for name, entry in members.items():
            if entry.get("disposition") not in DISPOSITIONS:
                raise NpmAuthorityError(
                    f"{group}.{name}: disposition {entry.get('disposition')!r} is not one of "
                    f"{sorted(DISPOSITIONS)}. An unclassified configuration mechanism fails "
                    "closed.")
    return doc


# --------------------------------------------------------------------------- chain resolution
def _object_type(path: Path) -> str:
    """What KIND of filesystem object npm actually is, read from the bytes rather than the name."""
    if path.is_symlink():
        return "symlink"
    try:
        head = path.open("rb").read(4)
    except OSError:
        return "unreadable"
    if any(head.startswith(m) for m in NATIVE_MAGIC):
        return "native executable"
    if head.startswith(b"#!"):
        return "script with shebang"
    return "data or script without shebang"


def _shebang(path: Path) -> str | None:
    try:
        first = path.open("rb").readline(512).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return first[2:].strip() if first.startswith("#!") else None


def _symlink_chain(start: Path) -> list:
    """Every hop, recorded. A chain that escapes its installation is a finding, not a detail."""
    chain, seen, cur = [], set(), start
    while cur.is_symlink():
        if str(cur) in seen:
            chain.append({"from": str(cur), "to": "<cycle>"})
            break
        seen.add(str(cur))
        target = os.readlink(cur)
        nxt = (cur.parent / target).resolve() if not os.path.isabs(target) else Path(target)
        chain.append({"from": str(cur), "raw_target": target, "to": str(nxt)})
        cur = nxt
    return chain


def _package_root(cli: Path) -> Path | None:
    """Walk up from the CLI entrypoint to the directory whose package.json calls itself npm."""
    for parent in [cli.parent, *cli.parents]:
        candidate = parent / "package.json"
        if candidate.is_file():
            try:
                if json.loads(candidate.read_text(encoding="utf-8")).get("name") == "npm":
                    return parent
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
    return None


def resolve_chain(path_env: str | None = None) -> dict:
    """The complete npm → Node execution chain, resolved from the filesystem.

    Everything here is STATIC. Nothing in this function executes npm or node, because the identity
    has to be authorized BEFORE the tool runs — asking a binary to describe itself is asking the
    attacker.
    """
    chain: dict = {"problems": []}
    search = path_env or os.environ.get("PATH", "")
    which = shutil.which("npm", path=search)
    chain["path_selected_npm"] = which

    # A DANGLING npm earlier on PATH is refused rather than skipped. `shutil.which` ignores an
    # entry it cannot execute and silently selects the next one, so a broken chain would otherwise
    # read as "npm is fine" — or, where npm is not required, as "npm is absent". Section 7 requires
    # the broken symlink itself to be refused, and the reason is substitution: the toolchain is
    # demonstrably not what the PATH says it is.
    dangling = []
    for element in search.split(os.pathsep):
        if not element:
            continue
        candidate = Path(element) / "npm"
        if candidate.is_symlink() and not candidate.exists():
            dangling.append(str(candidate))
        if which and str(candidate) == which:
            break
    chain["dangling_npm_entries"] = dangling
    for entry in dangling:
        chain["problems"].append(
            f"{entry} is a BROKEN symlink on PATH ahead of the selected npm. A dangling entry is "
            "refused rather than skipped: PATH resolution silently moving on to a different npm is "
            "a substitution, not a recovery.")

    if not which:
        chain["problems"].append("npm is not resolvable on PATH")
        return chain

    selected = Path(which)
    chain["symlink_chain"] = _symlink_chain(selected)
    try:
        canonical = selected.resolve(strict=True)
    except OSError as exc:
        chain["problems"].append(f"npm at {selected} does not resolve: {exc}. A broken symlink is "
                                 "refused rather than followed to whatever appears next on PATH.")
        return chain
    chain["canonical_npm"] = str(canonical)
    chain["object_type"] = _object_type(canonical)
    try:
        st = canonical.stat()
        chain["mode"] = oct(stat.S_IMODE(st.st_mode))
        chain["npm_sha256"] = _digest(canonical)
    except OSError as exc:
        chain["problems"].append(f"cannot read npm at {canonical}: {exc}")
        return chain

    chain["shebang"] = _shebang(canonical)

    root = _package_root(canonical)
    chain["npm_package_root"] = str(root) if root else None
    if root is None:
        chain["problems"].append(
            f"no npm package root was found above {canonical}: no package.json naming the package "
            "'npm' exists on the path to it. An npm with no package metadata cannot be identified, "
            "only assumed.")
    else:
        meta = root / "package.json"
        chain["npm_package_json"] = str(meta)
        chain["npm_package_json_sha256"] = _digest(meta)
        doc = json.loads(meta.read_text(encoding="utf-8"))
        chain["npm_declared_version"] = doc.get("version")
        chain["npm_declared_bin"] = doc.get("bin")
        declared_cli = (doc.get("bin") or {}).get("npm")
        if declared_cli:
            expected = (root / declared_cli).resolve()
            chain["declared_cli_entrypoint"] = str(expected)
            if expected != canonical:
                chain["problems"].append(
                    f"the npm package declares its CLI entrypoint as {expected}, but the executable "
                    f"selected from PATH resolves to {canonical}. The wrapper and the package "
                    "disagree about which JavaScript runs, so neither can be trusted to describe "
                    "the other.")

    # The interpreter. `#!/usr/bin/env node` means the Node that PATH selects, which is the Node an
    # attacker would supply — so it is resolved and bound rather than assumed.
    shebang = chain.get("shebang") or ""
    node_which = None
    if "env node" in shebang or shebang.endswith("/node") or shebang == "node":
        node_which = (shebang if shebang.startswith("/") and shebang.endswith("/node")
                      else shutil.which("node", path=path_env or os.environ.get("PATH", "")))
    elif shebang:
        node_which = shebang.split()[0]
    chain["path_selected_node"] = node_which
    if node_which:
        try:
            node_canonical = Path(node_which).resolve(strict=True)
            chain["canonical_node"] = str(node_canonical)
            chain["node_object_type"] = _object_type(node_canonical)
            chain["node_sha256"] = _digest(node_canonical)
            chain["node_mode"] = oct(stat.S_IMODE(node_canonical.stat().st_mode))
        except OSError as exc:
            chain["problems"].append(f"the Node interpreter {node_which} does not resolve: {exc}")
    elif chain["object_type"] != "native executable":
        chain["problems"].append(
            "no Node interpreter could be derived for a non-native npm, so the process that would "
            "actually run is unknown")
    return chain


# --------------------------------------------------------------------------- family + provenance
def _node_version_header(install_root: Path) -> str | None:
    """The version a genuine Node distribution DECLARES about itself.

    This is the evidence an attacker directory cannot supply by being in the right place. It is a
    file a real distribution ships and a planted `bin/npm` has no reason to carry, and its contents
    must agree with the directory the distribution was unpacked into.
    """
    header = install_root / "include" / "node" / "node_version.h"
    if not header.is_file():
        return None
    text = header.read_text(encoding="utf-8", errors="replace")
    parts = []
    for key in ("NODE_MAJOR_VERSION", "NODE_MINOR_VERSION", "NODE_PATCH_VERSION"):
        m = re.search(rf"#define\s+{key}\s+(\d+)", text)
        if not m:
            return None
        parts.append(m.group(1))
    return "v" + ".".join(parts)


def classify_installation(chain: dict, policy: dict) -> dict:
    """Which installation family this npm belongs to, and whether it satisfies that family."""
    result = {"family": None, "authority_model": None, "problems": [], "evidence": {}}
    canonical = chain.get("canonical_npm")
    if not canonical:
        result["problems"].append("no canonical npm to classify")
        result["family"] = "UNRESOLVED"
        result["authority_model"] = PROHIBITED
        return result

    canonical_path = Path(canonical)
    families = policy["installation_families"]

    # CI first: an explicit, declared external assumption rather than a silent one.
    if policy.get("ci_assumption", {}).get("active_when_env") and os.environ.get(
            policy["ci_assumption"]["active_when_env"]):
        entry = families.get("github_actions_setup_node")
        result["family"] = "github_actions_setup_node"
        result["authority_model"] = entry["authority_model"]
        result["evidence"]["ci_env"] = policy["ci_assumption"]["active_when_env"]
        result.update(_verify_ci_assumption(chain, policy))
        return result

    for name, entry in sorted(families.items()):
        roots = entry.get("installation_roots") or []
        for root in roots:
            expanded = Path(os.path.expandvars(os.path.expanduser(root)))
            try:
                relative = canonical_path.relative_to(expanded)
            except ValueError:
                continue
            result["family"] = name
            result["authority_model"] = entry["authority_model"]
            result["evidence"]["installation_root"] = str(expanded)
            result["evidence"]["relative"] = str(relative)
            if entry["authority_model"] == APPROVED_DIRECTORY_TREE_AND_PROVENANCE_BOUND:
                result["problems"].extend(_verify_version_manager_tree(chain, expanded, entry))
            elif entry["authority_model"] == APPROVED_PACKAGE_MANAGER_INSTALLATION_BOUND:
                result["problems"].extend(_verify_package_manager_tree(chain, expanded, entry))
            elif entry["authority_model"] == PROHIBITED:
                result["problems"].append(
                    f"npm resolves inside {name}, which the policy PROHIBITS: {entry['why']}")
            return result

    result["family"] = "UNKNOWN"
    result["authority_model"] = PROHIBITED
    result["problems"].append(
        f"npm resolves to {canonical}, which lies in no declared installation family. Gate "
        "4N-I28AS: an unknown installation layout FAILS CLOSED. To authorize this installation, "
        "add an installation family to tests/fixtures/npm-authority-policy.json declaring its "
        "root, expected layout and provenance evidence — do not add a path prefix, which is the "
        "defect ADV-I28AP-02 named.")
    return result


def manager_selected_versions(root: Path) -> set:
    """The version directories the version manager's OWN METADATA references.

    Gate 4N-I28AS sections 6 and 8 require installation-manager metadata in the binding, and the
    attack matrix showed exactly why. A fully SELF-CONSISTENT attacker installation — correct
    layout, correct symlink, plausible package.json, a copied genuine node binary, a semver
    directory name and a node_version.h agreeing with it — passes every internal-consistency check,
    because an attacker only has to be consistent with themselves. Manager metadata is the first
    evidence that is not about the directory itself.

    NVM records aliases under $NVM_DIR/alias. A bare major (`default -> 20`) resolves to the
    highest installed matching version, which is what nvm itself does.
    """
    nvm_dir = root.parent.parent                       # .../versions/node -> $NVM_DIR
    alias_dir = nvm_dir / "alias"
    installed = {d.name for d in root.iterdir() if d.is_dir()} if root.is_dir() else set()
    referenced: set = set()
    if not alias_dir.is_dir():
        # FAIL CLOSED, and this line was a real escape before it said so.
        #
        # Falsification arm f10 pointed NVM_DIR at an attacker tree containing a self-consistent
        # v20.20.2 installation and NO alias directory. Returning `installed` here meant "no
        # metadata, so trust everything installed", the caller's membership test passed trivially,
        # and the hostile tree was ACCEPTED. A fallback that returns the very set it is supposed to
        # constrain is not a fallback; it is the check switching itself off exactly when the
        # evidence it depends on is missing.
        return set()
    for alias in alias_dir.rglob("*"):
        if not alias.is_file():
            continue
        try:
            value = alias.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not value:
            continue
        if value in installed:
            referenced.add(value)
            continue
        if SEMVER_DIR.match(value):
            continue                                   # names a version that is not installed here
        prefix = value if value.startswith("v") else f"v{value}"
        matches = sorted(
            (v for v in installed if v == prefix or v.startswith(prefix + ".")),
            key=lambda v: [int(x) for x in SEMVER_DIR.match(v).groups()] if SEMVER_DIR.match(v)
            else [0, 0, 0])
        if matches:
            referenced.add(matches[-1])
    return referenced


def _verify_version_manager_tree(chain: dict, root: Path, entry: dict) -> list:
    """Version-manager installs: bind the TREE and the Node↔npm relationship, not the location.

    Gate 4N-I28AS section 9 is explicit that being beneath `$NVM_DIR/versions/node/` is not proof
    of provenance, and the reproduction showed exactly why: six different attacker layouts under
    that root were all accepted by a prefix rule.
    """
    problems = []
    canonical = Path(chain["canonical_npm"])
    # The installation is the version directory directly beneath the manager's versions root.
    try:
        version_dir = root / canonical.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return [f"cannot determine the installation directory for {canonical} under {root}"]

    if not SEMVER_DIR.match(version_dir.name):
        problems.append(
            f"{version_dir.name} is not a version directory of the form v<major>.<minor>.<patch>. "
            "An attacker-created directory is refused before any of its contents are read.")

    declared = _node_version_header(version_dir)
    if declared is None:
        problems.append(
            f"{version_dir} carries no include/node/node_version.h, so it is not a Node "
            "distribution — it is a directory in the right place. This is the ADV-I28AP-02 "
            "condition: location is not provenance.")
    elif declared != version_dir.name:
        problems.append(
            f"{version_dir} is named {version_dir.name} but the Node distribution inside it "
            f"declares {declared}. A genuine install agrees with itself.")

    node_bin = version_dir / "bin" / "node"
    if not node_bin.is_file():
        problems.append(f"{version_dir} has no bin/node, so npm has no interpreter of its own")
    elif _object_type(node_bin) != "native executable":
        problems.append(
            f"{node_bin} is a {_object_type(node_bin)} rather than a native executable. A "
            "shell script standing in for Node is refused.")

    # THE RELATIONSHIP. The Node that would actually run npm must be the Node of this same
    # installation. A genuine npm beside an attacker's node is not a genuine toolchain.
    canonical_node = chain.get("canonical_node")
    if canonical_node and Path(canonical_node).resolve() != node_bin.resolve():
        problems.append(
            f"npm resolves inside {version_dir} but its interpreter resolves to {canonical_node}, "
            f"not {node_bin}. The npm and Node halves of the chain come from different places, so "
            "the toolchain identity is not established.")

    expected_root = version_dir / "lib" / "node_modules" / "npm"
    actual_root = chain.get("npm_package_root")
    if actual_root and Path(actual_root).resolve() != expected_root.resolve():
        problems.append(
            f"the npm package root is {actual_root}, not {expected_root} as this installation "
            "layout requires")
    for required in entry.get("required_layout") or []:
        if not (version_dir / required).exists():
            problems.append(f"{version_dir} is missing {required}, which this family requires")

    # MANAGER SELECTION. Everything above can be satisfied by an attacker who is merely
    # self-consistent. This is the first check that appeals to evidence outside the directory.
    if entry.get("require_manager_selection", True):
        selected = manager_selected_versions(root)
        if selected and version_dir.name not in selected:
            problems.append(
                f"{version_dir.name} is installed under {root} but the version manager's own "
                f"metadata references none of it (it selects {sorted(selected)}). An installation "
                "the manager does not know about is a directory someone placed there, and a "
                "self-consistent attacker layout is indistinguishable from a genuine one on "
                "internal evidence alone.")
        elif not selected:
            problems.append(
                f"the version manager at {root.parent.parent} publishes no alias metadata, so no "
                "installation there can be shown to be manager-selected. This fails closed rather "
                "than falling back to 'it looks right'.")
    return problems


def _verify_package_manager_tree(chain: dict, root: Path, entry: dict) -> list:
    """System / Homebrew installs: the package manager owns the tree, so bind the tree's shape."""
    problems = []
    actual_root = chain.get("npm_package_root")
    if not actual_root:
        problems.append("no npm package root, so this installation cannot be identified")
        return problems
    for required in entry.get("required_layout") or []:
        if not (root / required).exists():
            problems.append(f"{root} is missing {required}, which this family requires")
    canonical_node = chain.get("canonical_node")
    if canonical_node and not str(Path(canonical_node).resolve()).startswith(str(root.resolve())):
        problems.append(
            f"npm resolves inside {root} but its interpreter is {canonical_node}, outside that "
            "installation. A mixed toolchain is not an identified toolchain.")
    return problems


def _verify_ci_assumption(chain: dict, policy: dict) -> dict:
    """The CI toolchain is an EXTERNAL assumption, stated rather than pretended away.

    Gate 4N-I28AS section 10: do not treat the CI prefix as self-authenticating. What can be
    checked here is that the chain at RUNTIME matches the assumption the workflow declares, and
    that the repository has not replaced npm after setup-node ran.
    """
    assumption = policy["ci_assumption"]
    problems = []
    canonical = chain.get("canonical_npm") or ""
    if not any(canonical.startswith(os.path.expandvars(r)) for r in assumption["expected_roots"]):
        problems.append(
            f"npm resolves to {canonical}, outside every location the declared CI toolchain "
            f"assumption covers ({assumption['expected_roots']}). The assumption is that "
            "setup-node provisions npm; an npm from anywhere else is not covered by it.")
    if chain.get("npm_package_root") is None:
        problems.append("the CI npm has no package root, so even the assumed toolchain cannot be "
                        "identified")
    repo = str(REPO_ROOT.resolve())
    if canonical.startswith(repo):
        problems.append(
            f"npm resolves INSIDE the repository ({canonical}). A repository-controlled npm after "
            "setup-node is exactly the substitution the assumption forbids.")
    return {"problems": problems, "assumption": assumption["statement"]}


# --------------------------------------------------------------------------- configuration
def configuration_state() -> dict:
    """Every npm-relevant configuration input actually present, read rather than assumed."""
    state = {"environment": {}, "files": {}}
    for var in sorted(os.environ):
        if var.startswith("npm_config_") or var.startswith("NPM_CONFIG_") or var in (
                "NVM_DIR", "NODE_OPTIONS", "NODE_PATH", "XDG_CONFIG_HOME", "COREPACK_ENABLE_STRICT"):
            state["environment"][var] = os.environ[var]
    candidates = {
        "project_npmrc": REPO_ROOT / ".npmrc",
        "user_npmrc": Path(os.environ.get("NPM_CONFIG_USERCONFIG",
                                          str(Path.home() / ".npmrc"))),
        "global_npmrc": Path(os.environ.get("NPM_CONFIG_GLOBALCONFIG", "/usr/local/etc/npmrc")),
    }
    for label, path in candidates.items():
        if path.is_file():
            state["files"][label] = {"path": str(path), "sha256": _digest(path),
                                     "bytes": path.stat().st_size}
        else:
            state["files"][label] = {"path": str(path), "absent": True}
    return state


def configuration_problems(state: dict, policy: dict) -> list:
    """Adjudicate the observed configuration against its declared disposition.

    Only mechanisms that can SUBSTITUTE THE TOOL or change a current load-bearing result are
    controlled. Gate 4N-I28AS section 11 is explicit that this must not become a general
    dependency-supply-chain redesign, and a control that objects to harmless settings is a control
    that gets switched off.
    """
    problems = []
    declared = policy.get("configuration") or {}
    for var, value in sorted(state["environment"].items()):
        entry = declared.get(var)
        if entry is None:
            generic = declared.get("npm_config_*") if var.startswith("npm_config_") else None
            entry = generic or declared.get("NPM_CONFIG_*") if var.startswith("NPM_CONFIG_") else generic
        if entry is None:
            problems.append(
                f"{var} is set and no disposition classifies it. An unclassified npm configuration "
                "mechanism fails closed rather than being assumed harmless.")
            continue
        disposition = entry["disposition"]
        if disposition == FATAL_IF_SET:
            problems.append(
                f"{var} is set, and it is FATAL_IF_SET: {entry['why']}")
        elif disposition == REQUIRED_EXACT_VALUE and value != entry.get("value"):
            problems.append(f"{var}={value!r} but this session requires {entry.get('value')!r}")
        elif disposition == ALLOWED_VALUE_SET and value not in (entry.get("values") or []):
            problems.append(
                f"{var}={value!r} is outside the allowed set {entry.get('values')}")
    for label, entry in sorted(declared.get("files", {}).items()):
        observed = state["files"].get(label, {})
        if entry.get("disposition") == FATAL_IF_SET and not observed.get("absent"):
            problems.append(
                f"{label} exists at {observed.get('path')} and is FATAL_IF_SET: {entry['why']}")
    return problems


# --------------------------------------------------------------------------- lifecycle boundary
def lifecycle_problems(policy: dict) -> list:
    """Every graded npm call site must carry an explicit lifecycle classification.

    Gate 4N-I28AS section 12 forbids claiming that npm binary identity validates arbitrary package
    scripts. `npm ci` genuinely runs preinstall/install/postinstall/prepare, and pretending
    otherwise would be a stronger claim than the evidence supports.
    """
    problems = []
    valid = {"LIFECYCLE_EXECUTION_REQUIRED", "LIFECYCLE_EXECUTION_PROHIBITED",
             "LIFECYCLE_EXECUTION_EXTERNALLY_ASSUMED", "LIFECYCLE_EXECUTION_NON_LOAD_BEARING"}
    sites = policy.get("call_sites") or []
    if not sites:
        problems.append("no npm call site is declared, so the lifecycle boundary adjudicates "
                        "nothing")
    for site in sites:
        klass = site.get("lifecycle")
        if klass not in valid:
            problems.append(
                f"{site.get('id')}: lifecycle classification {klass!r} is not one of "
                f"{sorted(valid)}; an unclassified call site fails closed")
        if klass == "LIFECYCLE_EXECUTION_PROHIBITED" and "--ignore-scripts" not in (
                site.get("argv") or ""):
            problems.append(
                f"{site.get('id')}: lifecycle execution is classified PROHIBITED but the argv "
                f"{site.get('argv')!r} does not pass --ignore-scripts, so the prohibition is "
                "declared and not enforced")
    return problems


# --------------------------------------------------------------------------- verification
def verify(policy: dict | None = None, *, path_env: str | None = None) -> dict:
    """Authorize the npm toolchain BEFORE npm runs. Fail closed on anything unestablished."""
    doc = policy if policy is not None else load_policy()
    chain = resolve_chain(path_env=path_env)
    problems = list(chain["problems"])

    required = doc.get("required_present", False)
    if chain.get("path_selected_npm") is None:
        if required:
            return {"clean": False, "problems": problems, "chain": chain, "family": None,
                    "policy_sha256": _digest(POLICY) if POLICY.is_file() else ""}
        # npm absent is not a violation on a host that does not have it; the graded CI job that
        # needs npm provisions it, and a developer without npm simply cannot run that job.
        return {"clean": True, "problems": [], "chain": chain, "family": "ABSENT",
                "configuration": {}, "policy_sha256": _digest(POLICY)}

    classification = classify_installation(chain, doc)
    problems.extend(classification["problems"])

    config = configuration_state()
    problems.extend(configuration_problems(config, doc))
    problems.extend(lifecycle_problems(doc))

    if classification["authority_model"] == PROHIBITED and not classification["problems"]:
        problems.append(f"npm installation family {classification['family']} is PROHIBITED")

    return {
        "clean": not problems,
        "problems": problems,
        "chain": chain,
        "family": classification["family"],
        "authority_model": classification["authority_model"],
        "configuration": config,
        "policy_sha256": _digest(POLICY),
    }


NPM_WORDS = frozenset({"npm", "npx"})


def derive_call_sites() -> dict:
    """Every tracked source position that invokes npm, derived TWO independent ways.

    GATE 4N-I28BB. Before this gate there was no npm call-site inventory at all: npm identity was
    verified for the process, but nothing enumerated WHERE npm is invoked, so `exec npm ci` could
    not have been noticed as bypassing anything. ADV-I28AX-01 made that gap load-bearing.

    Both derivations must agree AND, on a source known to contain npm, both must be NON-EMPTY.
    Agreement between two empty results is the correlated-omission failure this gate exists to
    make impossible, so emptiness is never by itself a pass.
    """
    import exec_transfer_oracle as oracle
    import shell_positions as sp

    shared, independent, problems = [], [], []
    for origin, text in sorted(oracle.tracked_sources().items()):
        scanned = (sp.scan_script(text, origin=origin) if origin.endswith((".sh", ".bash"))
                   else sp.scan(text, origin=origin))
        if not scanned.is_trustworthy():
            problems.append(f"{origin}: parse is not trustworthy ({scanned.status}), so npm call "
                            "sites cannot be derived from it")
            continue
        for command in scanned.commands:
            if command.word in NPM_WORDS:
                shared.append({"origin": origin, "line": command.line, "word": command.word})
        # The independent pass never uses the production parser: it reads command positions from
        # the oracle's own line model, plus plain line-leading npm invocations.
        for lineno, raw in enumerate(text.splitlines(), 1):
            stripped = oracle._strip_inert(raw).strip()
            if not stripped:
                continue
            for segment in re.split(r"\|\||&&|[|;&]", stripped):
                words = segment.split()
                index = 0
                while index < len(words) and re.match(r"^[A-Za-z_]\w*=", words[index]):
                    index += 1
                if index < len(words) and words[index] == "exec":
                    index += 1
                    while index < len(words) and words[index].startswith("-"):
                        index += 2 if words[index] == "-a" else 1
                if index < len(words) and words[index].strip("\"'") in NPM_WORDS:
                    independent.append({"origin": origin, "line": lineno,
                                        "word": words[index].strip("\"'")})
    key = lambda s: (s["origin"], s["line"], s["word"])
    shared_keys, independent_keys = {key(s) for s in shared}, {key(s) for s in independent}
    for missing in sorted(independent_keys - shared_keys):
        problems.append(f"the independent npm deriver found a call site the shared parser did not: "
                        f"{missing}")
    return {"shared": shared, "independent": independent,
            "shared_count": len(shared_keys), "independent_count": len(independent_keys),
            "agree": shared_keys == independent_keys, "problems": problems,
            "clean": not problems}


def snapshot(path_env: str | None = None) -> dict:
    """The identity to compare against at session finish.

    Verifying at configure and consuming for the rest of the session leaves a window: an npm file,
    symlink, Node binary or npmrc replaced AFTER the bootstrap passed is invisible to a fresh
    check, and visible only as a DIFFERENCE from what was validated.
    """
    chain = resolve_chain(path_env=path_env)
    config = configuration_state()
    return {
        "path_selected_npm": chain.get("path_selected_npm"),
        "canonical_npm": chain.get("canonical_npm"),
        "npm_sha256": chain.get("npm_sha256"),
        "npm_mode": chain.get("mode"),
        "symlink_chain": [h.get("to") for h in chain.get("symlink_chain") or []],
        "canonical_node": chain.get("canonical_node"),
        "node_sha256": chain.get("node_sha256"),
        "npm_package_root": chain.get("npm_package_root"),
        "npm_package_json_sha256": chain.get("npm_package_json_sha256"),
        "npm_declared_version": chain.get("npm_declared_version"),
        "path_env_sha256": hashlib.sha256(os.environ.get("PATH", "").encode()).hexdigest(),
        "nvm_dir": os.environ.get("NVM_DIR"),
        "configuration": config,
    }


def compare(before: dict, after: dict) -> list:
    """Every field that moved between configure and session finish, named individually."""
    drift = []
    labels = {
        "path_selected_npm": "the npm selected from PATH",
        "canonical_npm": "the canonical npm object",
        "npm_sha256": "the npm file CONTENT",
        "npm_mode": "the npm file mode",
        "symlink_chain": "the npm symlink chain",
        "canonical_node": "the Node interpreter",
        "node_sha256": "the Node binary CONTENT",
        "npm_package_root": "the npm package root",
        "npm_package_json_sha256": "the npm package metadata",
        "npm_declared_version": "the declared npm version",
        "path_env_sha256": "PATH",
        "nvm_dir": "NVM_DIR",
    }
    for key, label in labels.items():
        if before.get(key) != after.get(key):
            drift.append(f"{label} changed after verification: {before.get(key)!r} -> "
                         f"{after.get(key)!r}")
    if before.get("configuration") != after.get("configuration"):
        drift.append("npm configuration changed after verification")
    return drift


def main(argv=None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Verify npm/Node toolchain identity.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = verify()
    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True, default=str))
    else:
        for p in result["problems"]:
            print(f"  {p}")
        print(f"  family: {result.get('family')} / {result.get('authority_model')}")
        print("NPM TOOLCHAIN IDENTITY: " + ("verified" if result["clean"] else "refused"))
    return 0 if result["clean"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
