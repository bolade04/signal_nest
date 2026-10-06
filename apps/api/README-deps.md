# API dependency locks (P6-PLAT-4)

`pyproject.toml` declares the API's dependencies with open-ended ranges. The two files
beside it are the **locked** resolution of those ranges, compiled by pip-tools with
artifact hashes:

| File | Compiled from | Consumed by |
|---|---|---|
| `requirements-runtime.txt` | `[project.dependencies]` + the `full` extra | `Dockerfile` builder stage (`pip install --require-hashes`) — exactly what reaches the production image |
| `requirements-dev.txt` | the same, plus the `dev` extra, constrained to the runtime pins | CI (`.github/workflows/ci.yml`) and local test environments |

Rules:

- The image and CI install every **application dependency** from these files with
  `--require-hashes`; the project itself is then installed with `--no-deps`, so no
  application dependency can be pulled in outside the lock. Two installs remain unpinned,
  exactly as before this change: the `pip` self-upgrade (`pip install --upgrade pip`) in the
  image and CI, and the build-isolation fetch of `setuptools>=68` that `pip install .`
  performs for the project's own metadata. Neither is application code; pinning them is a
  separate decision.
- Never edit a lock by hand — a manual edit breaks the hashes. Change `pyproject.toml`
  (or just re-resolve) and regenerate both files.
- Dependabot watches `apps/api` and updates these files alongside `pyproject.toml`.

Regenerate with the documented toolchain — Python 3.12 (the image's `python:3.12-slim`),
`pip==25.1.1`, `pip-tools==7.5.2` (pip-tools 7.5.2 imports a pip-internal symbol that pip 26
removed, so pip must stay on 25.x), package source: the default index, PyPI
(`https://pypi.org/simple`). Run in place, these commands keep every existing pin that
still satisfies `pyproject.toml` (pip-tools' default when the output file exists and
`--upgrade` is not given):

```bash
cd apps/api
pip-compile --generate-hashes --strip-extras --extra full \
  --output-file requirements-runtime.txt pyproject.toml
pip-compile --generate-hashes --strip-extras --extra full --extra dev \
  --constraint requirements-runtime.txt \
  --output-file requirements-dev.txt pyproject.toml
```

**Upgrading is an explicit maintenance action.** A newer release on PyPI never changes the
locks by itself. To move a dependency, add `--upgrade-package NAME` (or `--upgrade` for
everything) to both commands above, review the diff and commit both files together.

## Verification in CI

Job *Backend quality*, step "Lock reproducibility (linux/amd64)", runs
`tools/verify_locks.py` with the toolchain above in a separate venv (from `apps/api`:
`../../.locktools/bin/python tools/verify_locks.py`). For each lock it checks that:

- every entry is an exact `name==version` pin (no pre-release or development version, no
  environment marker) followed only by its sha256 hash lines, read the way pip joins them;
- re-resolving `pyproject.toml` with the committed pins given to pip-tools as its existing
  pins yields exactly the committed pins. A pin that no longer satisfies a declared
  requirement, a dependency missing from the lock and a pin that nothing requires any more
  each fail the check;
- every hash, recomputed from the index for the pinned version, equals the committed set.
  The pin list handed to pip-tools carries no hashes and `--no-reuse-hashes` is set, so the
  committed hashes never reach the recomputed output.

A newer compatible release of a dependency does not fail the check. pip-compile runs with
`--no-config`, without any `PIP_*` environment variable and with pip's configuration files
disabled, so a pip-tools config file or pip configuration cannot turn the check into an
upgrade or add a package source; the script's `--compile-arg` accepts only the offline
options its tests need. By design it also accepts a lock hand-edited to an older version that
still satisfies `pyproject.toml`, with that version's correct hashes; adding an environment
marker to a dependency needs a change to the verifier. Its regression tests,
`tools/test_verify_locks.py`, run first in the same step, offline against generated local
wheels. They cover a newer compatible release, incompatible and unsatisfiable requirements
for both locks, a dev lock diverging from the runtime lock, a missing dependency, a corrupted
or missing hash, a removed dependency, configuration and environment isolation, and the
refused options and lock forms. Separately, the job's fresh `--require-hashes` install
checks the bytes of every artifact it installs on that runner against the committed hashes,
and `pip check` and the entry-point imports run on the result.

What the check does not establish: it needs PyPI to be reachable and still serving every
pinned artifact. Recomputed hashes come from the index's published digests (the PyPI JSON
API), so a file added to or removed from an already-pinned release changes the expected set
and fails the check until the locks are regenerated. Resolution reads the index's current
metadata. pip-tools reads the project's static `[project]` metadata, so no build backend runs;
dynamic dependencies would bring an unpinned `setuptools>=68` fetch into the check. Only pip
and pip-tools are pinned in the tool venv; their own dependencies are not. The check runs on
linux/amd64 with Python 3.12 only; it is not an offline or universal reproducibility
guarantee.

**Header caveat.** The autogenerated comment inside each lock shows `--no-index`. That text
is wrong and is a defect in pip-tools 7.5.2's header reconstruction (`piptools/utils.py`,
`get_compile_command`, handling of the negative-only `--no-index` flag): it prints the flag
for its *default* value. Reproduction: `printf 'six\n' > req.in && pip-compile req.in`
prints `--no-index` in the header while resolving from PyPI, and a real
`pip-compile --no-index req.in` fails with "No matching distribution found". Use the
commands above, never the header line.

Hashes cover every published artifact of each pinned version, so one lock is intended to
serve the linux/amd64 image, CI and macOS development; that equivalence is established by
the CI reproducibility step on linux/amd64 (the locks carry no environment markers), not
by the image build alone, and not by a macOS install.
