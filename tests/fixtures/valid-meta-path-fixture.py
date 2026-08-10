# A VALID attacker sitecustomize for the sys.meta_path substitution attack (Gate 4N-I28AM).
#
# WHY THIS FILE EXISTS. Gate 4N-I28AL finding ADV-I28AL-01: the historical I28AE
# SYS_META_PATH_VARIANT fixture generates Python that does not compile — `unexpected indent` at
# line 2 — so its sitecustomize was never imported, its finder was never installed, and the
# assertions in test_a03 described a session in which the attack did not exist. Every one of those
# assertions is trivially true of nothing happening.
#
# This file is the current control. It is a real, compiling, executing attack, and it writes a
# SENTINEL at every stage so a test can never again pass because the attack silently failed to run.
# The historical fixture is preserved untouched; this supersedes it as CURRENT proof only.
#
# Stage sentinels, in order:
#   SITECUSTOMIZE_EXECUTED   this module ran at all
#   FINDER_INSTALLED         the finder reached sys.meta_path
#   FINDER_CONSULTED         the import system asked it about the target module
#   LOADER_SELECTED          it returned a spec, so its loader was chosen
#   MODULE_EXECUTED          the substituted module body actually executed
#
# A test asserting "the session was refused" MUST also assert which sentinels fired. Refusal with
# no sentinels means the attack never started, which is precisely the failure this file exists to
# make impossible to mistake for a catch.

import importlib.abc
import importlib.util
import os
import pathlib
import sys

SENTINEL = pathlib.Path(os.environ.get(
    "SIGNALNEST_METAPATH_SENTINEL",
    str(pathlib.Path(__file__).resolve().parent.parent / "metapath-sentinel.txt")))
TARGET = "pytest_session_guard"
GENUINE = pathlib.Path(__file__).resolve().parent / f"{TARGET}.py"


def _mark(stage: str) -> None:
    with SENTINEL.open("a", encoding="utf-8") as handle:
        handle.write(stage + "\n")


class SubstitutingFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Claims the target module and loads a weakened copy of it."""

    def find_spec(self, name, path=None, target=None):
        if name != TARGET:
            return None
        _mark("FINDER_CONSULTED")
        spec = importlib.util.spec_from_loader(name, self, origin=str(GENUINE))
        _mark("LOADER_SELECTED")
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        # __file__ MUST be set before exec: the target reads it at module level to derive
        # REPO_ROOT. Gate 4N-I28AC hit this exact defect, and so did my first draft of this
        # fixture — a fixture that raises NameError is another way of not running the attack.
        module.__file__ = str(GENUINE)
        module.__spec__ = importlib.util.spec_from_loader(TARGET, self, origin=str(GENUINE))
        source = GENUINE.read_text(encoding="utf-8")
        # The substitution that matters: the guard stops failing the session.
        weakened = source.replace("session.exitstatus = 3", "pass")
        exec(compile(weakened, str(GENUINE), "exec"), module.__dict__)
        _mark("MODULE_EXECUTED")


_mark("SITECUSTOMIZE_EXECUTED")
sys.meta_path.insert(0, SubstitutingFinder())
_mark("FINDER_INSTALLED")
