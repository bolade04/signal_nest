#!/usr/bin/env python3
"""Stage the inputs of two merge-path scans that cannot read their population in place
(P6-CI-6 coverage gaps, operator order 2026-10-20).

    python3 security/stage_inputs.py vendored --out DIR --env LABEL ROOT INVENTORY [--env ...]

For every installed pip in each environment (read from the environment's own `pip inspect`
inventory), write the pins that pip declares in its pip/_vendor/vendor.txt to
DIR/LABEL/requirements-vendored-pip.txt, one exact pin per line. pip vendors those packages
without dist-info, so a scan of the installed environment sees them only through an SBOM pip ships (26.2.1
has one, 25.x has none); a Trivy
filesystem scan of the staged list analyses them by name and version. An environment with no pip
gets no file. The reading is security/gate.py's own (pip_vendored_pins), and the tooling gate
re-reads vendor.txt from the environment rather than trusting the staged file.

    python3 security/stage_inputs.py binaries --pins security/scanner-binaries.json --job JOB --out DIR --manifest FILE

Copy each executable the pins file names for JOB (`jobs`), resolved on PATH exactly as the job's steps resolve it, to DIR/NAME
(mode kept: Trivy analyses executables only) and record in FILE the path it resolved to and its
sha256, for a `trivy rootfs` Go binary analysis of the copies. The binary gate re-hashes both the
copy and the recorded path against the pinned release digest.

    python3 security/stage_inputs.py readonly ROOT [ROOT ...]

Remove every write permission from each installed environment (its directories and files; symbolic links are
left alone), right after its first inventory, so that an ordinary later install or uninstall into it fails its
step instead of escaping both inventories. The tooling gate's --require-read-only checks it at each gate. Code
that restores write permission first is not stopped by this (the README's disclosed limit).

DIR must not exist yet, so nothing stale is ever scanned. Exit 0 staged, 2 an input is missing or
cannot be accounted for, 5 usage. Stdlib only; no network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402 — the same reading of an environment the tooling gate applies

VENDORED_FILE = "requirements-vendored-pip.txt"


def _fresh(out: pathlib.Path) -> None:
    if out.exists() or out.is_symlink():
        raise gate.ReportError(f"{out} already exists: stage into a fresh directory so nothing stale is scanned")
    out.mkdir(parents=True)


def stage_vendored(out: pathlib.Path, envs: list[tuple[str, str, str]]) -> list[str]:
    labels = [label for label, _, _ in envs]
    if len(set(labels)) != len(labels) or any(not label or "/" in label or label.startswith(".") for label in labels):
        raise gate.ReportError(f"environment labels must be distinct plain names: {labels}")
    _fresh(out)
    lines = []
    for label, root, inventory in envs:
        installed = gate.load_installed(pathlib.Path(inventory))
        vendors = gate.pip_vendored_pins(installed, pathlib.Path(root))
        if not vendors:
            lines.append(f"{label}: no pip installed in {root}; nothing staged")
            continue
        pins = sorted({pin for _, _, ps in vendors for pin in ps})
        text = [f"# Staged by security/stage_inputs.py from {root}: the packages pip vendors (no dist-info)."]
        text += [f"# pip {version}: {vrel}" for version, vrel, _ in vendors]
        text += [f"{name}=={version}" for name, version in pins]
        (out / label).mkdir()
        (out / label / VENDORED_FILE).write_text("\n".join(text) + "\n", encoding="utf-8")
        lines.append(f"{label}: {len(pins)} vendored pins of {', '.join(f'pip {v}' for v, _, _ in vendors)} -> {label}/{VENDORED_FILE}")
    return lines


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stage_binaries(out: pathlib.Path, manifest: pathlib.Path, names: list[str]) -> list[str]:
    if len(set(names)) != len(names) or any(not n or "/" in n or n.startswith(".") for n in names):
        raise gate.ReportError(f"binary names must be distinct plain names: {names}")
    found = {}
    for name in names:
        where = shutil.which(name)
        if where is None:
            raise gate.ReportError(f"{name} is not on PATH: the binary the job executes cannot be staged")
        origin = pathlib.Path(os.path.realpath(where))
        if not origin.is_file():
            raise gate.ReportError(f"{name} resolves to {origin}, which is not a regular file")
        found[name] = origin
    _fresh(out)
    record, lines = {}, []
    for name, origin in found.items():
        shutil.copy2(origin, out / name)
        digest = _sha256(origin)
        if _sha256(out / name) != digest:
            raise gate.ReportError(f"the staged copy of {name} differs from {origin}")
        record[name] = {"origin": str(origin), "on_path_as": shutil.which(name), "sha256": digest, "staged": str(out / name)}
        lines.append(f"{name}: {origin} sha256 {digest} -> {out / name}")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": 1, "binaries": record}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return lines


def make_read_only(roots: list[str]) -> list[str]:
    lines = []
    for root in roots:
        top = pathlib.Path(root)
        if top.is_symlink() or not top.is_dir():
            raise gate.ReportError(f"{root} is not a directory: nothing to make read-only")
        count = 0
        for here, dirs, files in os.walk(top, topdown=False):
            for name in files + dirs:
                path = os.path.join(here, name)
                if os.path.islink(path):
                    continue
                os.chmod(path, os.lstat(path).st_mode & ~0o222)
                count += 1
        os.chmod(top, os.lstat(top).st_mode & ~0o222)
        lines.append(f"{root}: {count + 1} files and directories made read-only")
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("vendored")
    v.add_argument("--out", required=True)
    v.add_argument("--env", nargs=3, action="append", required=True, metavar=("LABEL", "ROOT", "INVENTORY"))
    b = sub.add_parser("binaries")
    b.add_argument("--pins", required=True)
    b.add_argument("--job", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--manifest", required=True)
    r = sub.add_parser("readonly")
    r.add_argument("roots", nargs="+")
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return gate.EXIT_USAGE if exc.code else 0
    try:
        if args.command == "vendored":
            lines = stage_vendored(pathlib.Path(args.out), [tuple(e) for e in args.env])
        elif args.command == "readonly":
            lines = make_read_only(args.roots)
        else:
            names = list(gate.load_binary_pins(pathlib.Path(args.pins), args.job))
            lines = stage_binaries(pathlib.Path(args.out), pathlib.Path(args.manifest), names)
    except (gate.ReportError, gate.IncompleteError, OSError) as exc:
        sys.stderr.write(f"stage_inputs: {exc}\n")
        return gate.EXIT_REPORT
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
