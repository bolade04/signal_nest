"""Verify the committed API dependency locks (P6-PLAT-4) against ``pyproject.toml``.

Run from ``apps/api`` with the pinned lock toolchain (pip 25.1.1, pip-tools 7.5.2)::

    ../../.locktools/bin/python tools/verify_locks.py

For ``requirements-runtime.txt`` (project dependencies + extra ``full``) and
``requirements-dev.txt`` (+ extra ``dev``, constrained to the runtime lock) it checks:

* every entry is an exact ``name==version`` pin carrying at least one sha256 hash;
* re-resolving ``pyproject.toml`` with the committed pins supplied to pip-tools as its
  existing pins (pip-tools keeps an existing pin unless ``--upgrade`` is given or the pin no
  longer satisfies a requirement) yields exactly the committed pins. A pin that no longer
  satisfies a declared requirement is discarded by pip-tools, a dependency the lock lacks is
  added, and a pin that nothing requires any more is not emitted: each is a difference;
* every hash is recomputed from the package index for the pinned version. The pin list handed
  to pip-tools carries no hashes and ``--no-reuse-hashes`` is passed, so the committed hashes
  never reach the output: a corrupted, missing or extra hash is a difference.

A newer release of a dependency on the index is therefore not a failure. Moving to it is an
explicit maintenance action (``pip-compile --upgrade-package NAME``; see README-deps.md).
pip-compile runs with ``--no-config``, no ``PIP_*`` environment variable and pip's own
configuration files disabled, so neither a pip-tools config file nor pip configuration can
turn verification into an upgrade or add a package source. ``--compile-arg`` accepts only
the options the offline regression tests need.

What it accepts by design: a lock whose pins all satisfy ``pyproject.toml`` and whose hashes
match the index. A hand edit to an OLDER compatible version with its correct hashes therefore
passes, exactly as pip-tools itself would keep it. Pre-release and development versions,
environment markers and anything but plain hash-carrying pins are refused.

The committed files are never written. Exit status: 0 verified, 1 difference or resolution
failure, 2 usage or toolchain error.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from pip._vendor.packaging.version import InvalidVersion, Version

# The documented lock toolchain (README-deps.md). The existing-pin and hash behaviour this
# script relies on was measured on exactly these versions; any other version is refused.
TOOLCHAIN = {"pip": "25.1.1", "pip-tools": "7.5.2"}

RUNTIME = "requirements-runtime.txt"
DEV = "requirements-dev.txt"
# (lock file, extras, lock it is constrained to): the compile inputs README-deps.md documents.
LOCKS = (
    (RUNTIME, ("full",), None),
    (DEV, ("full", "dev"), RUNTIME),
)

# The only pip-compile options --compile-arg may pass: what the offline regression tests need
# to resolve from a local wheel directory. Everything else is refused, so no option can turn
# verification into an upgrade, reuse the committed hashes, redirect output or load a config.
_ALLOWED_FLAGS = ("--no-index", "--no-emit-find-links")
_ALLOWED_VALUED = ("--find-links", "--cache-dir")

# pip-tools' own output format: "name==version \" then four-space-indented hash lines, the
# last one without a continuation backslash.
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.!+]*) \\$")
_HASH = re.compile(r"^    --hash=sha256:([0-9a-f]{64})( \\)?$")


class LockError(Exception):
    """The lock file is not a list of hash-carrying exact pins."""


@dataclass
class Pin:
    name: str
    version: str
    hashes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return re.sub(r"[-_.]+", "-", self.name).lower()


def parse_lock(path: Path) -> list[Pin]:
    """Parse a pip-tools lock; refuse anything that is not an exact pin with sha256 hashes.

    Lines are read the way pip joins them: a pin line ends with a continuation backslash and
    is followed only by its hash lines until one without a backslash closes the entry.
    Comments and blank lines are allowed only between entries, never inside one.
    """
    pins: list[Pin] = []
    current: Pin | None = None
    open_entry = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        where = f"{path.name}:{number}"
        if open_entry:
            digest = _HASH.match(line)
            if not digest or current is None:
                raise LockError(f"{where}: expected a sha256 hash line continuing "
                                f"{current.name if current else 'the entry'}: {line!r}")
            current.hashes.append(digest.group(1))
            open_entry = digest.group(2) is not None
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pin = _PIN.match(line)
        if not pin:
            raise LockError(f"{where}: not an exact pin followed by sha256 hashes: {line!r}")
        try:
            version = Version(pin.group(2))
        except InvalidVersion as exc:
            raise LockError(f"{where}: invalid version {pin.group(2)!r}") from exc
        if version.is_prerelease or version.is_devrelease:
            raise LockError(f"{where}: {pin.group(1)}=={pin.group(2)} is a pre-release or "
                            "development version")
        current = Pin(pin.group(1), pin.group(2))
        pins.append(current)
        open_entry = True
    if open_entry:
        raise LockError(f"{path.name}: the last entry's continuation is never closed")
    if not pins:
        raise LockError(f"{path.name}: no pinned requirements")
    seen: set[str] = set()
    for pin in pins:
        if pin.key in seen:
            raise LockError(f"{path.name}: {pin.name} is pinned more than once")
        seen.add(pin.key)
        if not pin.hashes:
            raise LockError(f"{path.name}: {pin.name}=={pin.version} carries no sha256 hash")
    return pins


def pin_list(pins: list[Pin]) -> str:
    """The existing-pin input for pip-tools: versions only, never the committed hashes."""
    return "".join(f"{pin.name}=={pin.version}\n" for pin in pins)


def body(path: Path) -> list[str]:
    """Pin and hash lines in order; comments (header, ``# via``) are not compared."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if not line.lstrip().startswith("#")]


def check_toolchain() -> list[str]:
    problems = []
    for dist, wanted in TOOLCHAIN.items():
        try:
            found = metadata.version(dist)
        except metadata.PackageNotFoundError:
            found = None
        if found != wanted:
            problems.append(f"{dist} {found or 'is not installed'} (required {wanted})")
    return problems


def forbidden_compile_args(args: list[str]) -> list[str]:
    """Every --compile-arg that is not exactly an allowed flag or an allowed ``--opt=value``."""
    bad = []
    for arg in args:
        name, has_value, value = arg.partition("=")
        allowed = (arg in _ALLOWED_FLAGS
                   or (name in _ALLOWED_VALUED and has_value and value != ""))
        if not allowed:
            bad.append(arg)
    return bad


def compile_environment() -> dict[str, str]:
    """The process environment without pip/pip-tools configuration: no ``PIP_*`` variable
    (pip options and pip-tools' PIP_TOOLS_* both live there) and pip's configuration files
    disabled (pip skips every config file when PIP_CONFIG_FILE is os.devnull)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def compile_lock(
    project: Path,
    output: Path,
    extras: tuple[str, ...],
    constraint: Path | None,
    compile_args: list[str],
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable, "-m", "piptools", "compile", "--quiet", "--no-config",
        "--generate-hashes", "--no-reuse-hashes", "--strip-extras",
    ]
    for extra in extras:
        command += ["--extra", extra]
    if constraint is not None:
        command += ["--constraint", str(constraint)]
    command += [*compile_args, "--output-file", str(output), "pyproject.toml"]
    return subprocess.run(command, cwd=project, capture_output=True, text=True, check=False,
                          env=compile_environment())


def describe_difference(committed: list[Pin], fresh: list[Pin]) -> list[str]:
    old = {pin.key: pin for pin in committed}
    new = {pin.key: pin for pin in fresh}
    out = [f"  only in the committed lock: {old[k].name}=={old[k].version}"
           for k in sorted(old.keys() - new.keys())]
    out += [f"  missing from the committed lock: {new[k].name}=={new[k].version}"
            for k in sorted(new.keys() - old.keys())]
    for key in sorted(old.keys() & new.keys()):
        a, b = old[key], new[key]
        if a.version != b.version:
            out.append(f"  version: {a.name} committed {a.version}, resolved {b.version}")
        elif set(a.hashes) != set(b.hashes):
            out.append(f"  hashes: {a.name}=={a.version} committed-only "
                       f"{len(set(a.hashes) - set(b.hashes))}, index-only "
                       f"{len(set(b.hashes) - set(a.hashes))}")
    return out


def verify(project: Path, compile_args: list[str]) -> int:
    failed = False
    if compile_args:
        print("NOTE: pip-compile also receives " + " ".join(compile_args)
              + " (offline regression-test sources, not the package index)")
    with tempfile.TemporaryDirectory(prefix="verify-locks-") as tmp:
        for lock, extras, constrained_to in LOCKS:
            committed = project / lock
            try:
                pins = parse_lock(committed)
            except LockError as exc:
                print(f"LOCK INVALID: {exc}")
                failed = True
                continue
            fresh = Path(tmp) / lock
            fresh.write_text(pin_list(pins), encoding="utf-8")
            constraint = Path(tmp) / constrained_to if constrained_to else None
            if constraint is not None and not constraint.is_file():
                print(f"{lock}: not verified, because {constrained_to} did not resolve")
                failed = True
                continue
            proc = compile_lock(project, fresh, extras, constraint, compile_args)
            if proc.stderr.strip():
                print(proc.stderr.rstrip())
            if proc.returncode != 0:
                print(f"LOCK UNRESOLVABLE: {lock}: pip-compile exited {proc.returncode}")
                fresh.unlink(missing_ok=True)
                failed = True
                continue
            a, b = body(committed), body(fresh)
            if a == b:
                print(f"{lock}: verified, {len(pins)} pins and "
                      f"{sum(len(p.hashes) for p in pins)} hashes, pins kept and every hash "
                      "recomputed from the index")
                continue
            failed = True
            print(f"LOCK MISMATCH: {lock} is not what pyproject.toml resolves to with its own pins")
            try:
                for line in describe_difference(pins, parse_lock(fresh)):
                    print(line)
            except LockError as exc:
                print(f"  (resolved output not parseable: {exc})")
            sys.stdout.writelines(
                line + "\n" for line in difflib.unified_diff(
                    a, b, f"{lock} (committed)", f"{lock} (resolved)", lineterm="", n=1))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", type=Path, default=Path(__file__).resolve().parents[1],
                        help="directory holding pyproject.toml and both locks (default: apps/api)")
    parser.add_argument("--compile-arg", action="append", default=[], metavar="ARG",
                        help="extra pip-compile argument, e.g. --compile-arg=--no-index")
    args = parser.parse_args(argv)
    bad = forbidden_compile_args(args.compile_arg)
    if bad:
        print(f"REFUSED: {' '.join(bad)} (only {', '.join(_ALLOWED_FLAGS + _ALLOWED_VALUED)} "
              "may be passed through)")
        return 2
    problems = check_toolchain()
    if problems:
        print("TOOLCHAIN MISMATCH: " + "; ".join(problems))
        return 2
    return verify(args.project_dir.resolve(), args.compile_arg)


if __name__ == "__main__":
    sys.exit(main())
