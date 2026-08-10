#!/usr/bin/env python3
"""Explicit two-tier anchor loading (Gate 4N-I13, Defect 1).

THE DEFECT. Every anchor resolved through `Path.home() / ".signalnest" / "anchor"`. On a CI
runner with an empty HOME that path does not exist, so five guard scripts exited non-zero and
nine test files failed at collection. Worse, the Gate 4N-I10 "clean checkout" reported
`933 passed` and I offered it as evidence of portability — it was a fresh clone that INHERITED
$HOME, so it read the anchor off my machine and never modelled CI at all. The check that
should have caught the problem was scoped so it could not.

TWO TIERS, declared explicitly. There is no default and no discovery:

  TIER 1  SYNTHETIC — a tracked fixture with a synthetic account, marked
          NON_PRODUCTION_TEST_FIXTURE. Ordinary CI runs on this. It validates MECHANICS and
          is structurally incapable of certifying a production candidate.
  TIER 2  PROTECTED — the real anchor, supplied through an environment variable holding
          canonical JSON, with its expected SHA-256 supplied SEPARATELY. Missing anchor,
          missing hash, or mismatched hash all fail. There is no fallback.

The rule that makes this worth anything: `certifies_production` is True only for Tier 2, and
a synthetic anchor presented to Tier 2 is refused by name. A synthetic fixture must never be
able to bless a real candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TIER_SYNTHETIC = "TIER_1_SYNTHETIC"
TIER_PROTECTED = "TIER_2_PROTECTED"
TIERS = (TIER_SYNTHETIC, TIER_PROTECTED)

SYNTHETIC_MARKER = "NON_PRODUCTION_TEST_FIXTURE"

# Ordinary CI provisions this file, which is STAGED FOR ADDITION (not yet in HEAD; see
# scripts/tracked_state.py). It is in the repository ON PURPOSE: it holds no
# real account, no real permission-set id and no retained evidence.
SYNTHETIC_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "synthetic-anchor.json"

# Tier 2 inputs. Names only — this module never reads a secret's value into a log.
ENV_ANCHOR_JSON = "SIGNALNEST_ANCHOR_JSON"
ENV_ANCHOR_SHA256 = "SIGNALNEST_ANCHOR_SHA256"
ENV_ANCHOR_PATH = "SIGNALNEST_ANCHOR_PATH"


class AnchorError(Exception):
    """Fail-closed. Never downgraded to a warning, never satisfied by a fallback."""


@dataclass(frozen=True)
class LoadedAnchor:
    tier: str
    anchor: dict
    canonical_sha256: str
    provenance: str
    certifies_production: bool

    def account_last4(self) -> str:
        return self.anchor["approved_account_id"][-4:]

    def redacted(self) -> dict:
        """Safe for ordinary logs: never the full account id."""
        return {"tier": self.tier, "canonical_sha256": self.canonical_sha256,
                "approved_account_last4": self.account_last4(),
                "partition": self.anchor["partition"],
                "certifies_production": self.certifies_production,
                "provenance": self.provenance}


REQUIRED_FIELDS = ("approved_account_id", "partition", "approved_region", "role_name_prefix")


def _validate(anchor: dict, *, tier: str) -> None:
    for field in REQUIRED_FIELDS:
        if not anchor.get(field):
            raise AnchorError(f"anchor is missing required field {field!r}")
    account = anchor["approved_account_id"]
    if not re.fullmatch(r"\d{12}", account):
        raise AnchorError(f"approved_account_id {account!r} is not 12 digits")

    is_synthetic = anchor.get("_classification") == SYNTHETIC_MARKER
    if tier == TIER_PROTECTED and is_synthetic:
        raise AnchorError(
            "a synthetic fixture was supplied to the PROTECTED tier. Tier 1 validates "
            "mechanics only and must never certify a production candidate.")
    if tier == TIER_SYNTHETIC and not is_synthetic:
        raise AnchorError(
            f"the Tier 1 anchor is not marked {SYNTHETIC_MARKER}. Ordinary CI must not be "
            "handed real environment evidence.")


def _validate_anchor_schema(anchor: dict, tier: str) -> None:
    """GATE 4N-I24D. `anchor_version` and `permission_sets` were authored, hashed and consumed
    by NOTHING: an anchor could declare an unknown schema, or omit the permission sets the
    identity model resolves against, and still load clean.

    It is called on BOTH load paths deliberately. The first version of this check lived only
    after the mid-function return, so for TIER_1_SYNTHETIC it was unreachable and the
    falsification sweep caught it surviving — a control in dead code, which is the exact
    defect class this chain exists to remove.
    """
    version = str(anchor.get("anchor_version", ""))
    if version not in ("synthetic-1", "protected-1"):
        raise AnchorError(
            f"anchor_version {version!r} is not a known schema version; an anchor whose schema "
            "is unrecognised must fail closed rather than be parsed on assumption.")
    sets = anchor.get("permission_sets")
    if not isinstance(sets, dict) or not sets:
        raise AnchorError(
            "the anchor declares no permission_sets; the identity model resolves permission set "
            "names against this map, so an empty map would silently resolve nothing.")
    # The three permission sets the identity model resolves against. These are the names
    # the anchor actually carries; an earlier draft of this check invented "ReadOnlyVerifier"
    # and the baseline run refused it, which is the check doing its job on its own author.
    for required in ("W0Operator", "ReadOnly", "ICPermAdmin"):
        if required not in sets:
            raise AnchorError(f"the anchor omits permission set {required!r}")
    if tier == TIER_SYNTHETIC:
        bad = sorted(n for n, v in sets.items()
                     if str((v or {}).get("confidence", "")).upper() != "SYNTHETIC")
        if bad:
            raise AnchorError(
                f"tier {TIER_SYNTHETIC} anchor declares non-synthetic permission sets {bad}; a "
                "synthetic anchor must never carry a real-confidence identity.")


def load(tier: str, *, env: dict | None = None,
         synthetic_path: Path | None = None) -> LoadedAnchor:
    """Load an anchor for an EXPLICITLY DECLARED tier. No default, no discovery."""
    env = os.environ if env is None else env
    if tier not in TIERS:
        raise AnchorError(f"undeclared or unknown tier {tier!r}; expected one of {TIERS}")

    if tier == TIER_SYNTHETIC:
        path = synthetic_path or SYNTHETIC_FIXTURE
        if not path.exists():
            raise AnchorError(f"synthetic anchor fixture missing at {path}")
        raw = path.read_bytes()
        anchor = json.loads(raw.decode("utf-8"))
        _validate(anchor, tier=tier)
        _validate_anchor_schema(anchor, tier)
        return LoadedAnchor(
            tier=tier, anchor=anchor,
            canonical_sha256=hashlib.sha256(raw).hexdigest(),
            provenance=f"SYNTHETIC_TEST_FIXTURE: {path.relative_to(REPO_ROOT)}",
            certifies_production=False)

    # --- Tier 2 ---------------------------------------------------------------------------
    inline = env.get(ENV_ANCHOR_JSON)
    path_value = env.get(ENV_ANCHOR_PATH)
    if inline and path_value:
        raise AnchorError(
            f"both {ENV_ANCHOR_JSON} and {ENV_ANCHOR_PATH} are set; refusing to guess which "
            "anchor is authoritative")
    if not inline and not path_value:
        raise AnchorError(
            f"protected tier requires {ENV_ANCHOR_JSON} or {ENV_ANCHOR_PATH}. There is NO "
            "fallback to a home directory: an anchor read off a developer machine cannot "
            "certify anything in CI.")

    expected = env.get(ENV_ANCHOR_SHA256)
    if not expected:
        raise AnchorError(
            f"protected tier requires {ENV_ANCHOR_SHA256}, supplied SEPARATELY from the "
            "anchor. An anchor that vouches for its own integrity vouches for nothing.")

    if inline:
        raw = inline.encode("utf-8")
        source = f"environment variable {ENV_ANCHOR_JSON}"
    else:
        path = Path(path_value)
        if not path.exists():
            raise AnchorError(f"{ENV_ANCHOR_PATH} points at a missing file: {path}")
        raw = path.read_bytes()
        source = f"explicit path {ENV_ANCHOR_PATH}"

    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise AnchorError(
            f"protected anchor hash mismatch: expected {expected[:16]}…, got {actual[:16]}…")

    try:
        anchor = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AnchorError(f"protected anchor is not valid JSON: {exc}") from exc
    _validate(anchor, tier=tier)

    _validate_anchor_schema(anchor, tier)
    return LoadedAnchor(tier=tier, anchor=anchor, canonical_sha256=actual,
                        provenance=f"PROTECTED: {source}, hash verified independently",
                        certifies_production=True)


def declared_tier(env: dict | None = None) -> str:
    """The tier must be STATED. An unset tier is an error, not 'probably synthetic'."""
    env = os.environ if env is None else env
    tier = env.get("SIGNALNEST_ANCHOR_TIER")
    if not tier:
        raise AnchorError(
            "SIGNALNEST_ANCHOR_TIER is not set. The tier must be declared explicitly — "
            "inferring it is how a synthetic fixture ends up certifying production.")
    if tier not in TIERS:
        raise AnchorError(f"unknown tier {tier!r}; expected one of {TIERS}")
    return tier


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", choices=TIERS)
    args = parser.parse_args()
    try:
        loaded = load(args.tier or declared_tier())
    except AnchorError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("ANCHOR: fail-closed")
        return 2
    print(json.dumps(loaded.redacted(), indent=2, ensure_ascii=True))
    print("ANCHOR: loaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
