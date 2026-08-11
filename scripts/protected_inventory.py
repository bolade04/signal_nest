#!/usr/bin/env python3
"""Explicit two-tier resource-inventory loading (Gate 4N-I18, SEC-1).

THE DEFECT THIS CLOSES. Gate 4N-I17's security lane blocked the commit:
`infra/aws/live-resource-inventory.json` sat in the working tree, unprotected by
`.gitignore`, carrying the real account id, live bucket names with AWS-assigned suffixes,
CloudTrail and RDS ARNs, KMS key ids and the state lock-table name. `git log --all -S`
returned ZERO hits for every one of those values, so committing the gate package would have
been FIRST DISCLOSURE into permanent git history — unrecoverable, because deleting a file
later does not remove it from history. The repository's own `.gitignore` already treats this
exact class as never-committed for `backend.hcl` ("carries the state bucket/table/KMS
identifiers"), and the inventory carried the same identifiers with no matching rule.

THE MODEL, deliberately identical in shape to scripts/anchor_loader.py so there is one
mental model for "where does trusted external evidence come from":

  TIER 1  SYNTHETIC — tests/fixtures/synthetic-inventory.json, a TRACKED fixture whose
          values are wholly synthetic and whose account is the documentation placeholder.
          Ordinary CI runs on this. It exercises every consumer's MECHANISM and is
          structurally incapable of certifying anything about production.
  TIER 2  PROTECTED — the real inventory, supplied through an explicit absolute PATH with
          its expected SHA-256 supplied SEPARATELY. Missing path, missing hash, mismatched
          hash, a synthetic fixture presented as real, or a path inside the repository all
          fail closed. There is no fallback and no discovery.

WHY THE HASH MUST ARRIVE SEPARATELY. If the expected hash were read from the inventory it
would be the inventory attesting to itself — the Gate 4N-I16 self-certification defect in a
new place. `SIGNALNEST_INVENTORY_SHA256` is a distinct input, and an inventory that carries
its own `sha256`/`expected_sha256` field is REFUSED rather than silently trusted.

WHY THERE IS NO REPOSITORY FALLBACK. A fallback is what made the Gate 4N-I10 "clean
checkout" read a developer-local anchor and report portability it had never tested. An
absent Tier 2 input is an error, never a quiet downgrade to Tier 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TIER_SYNTHETIC = "TIER_1_SYNTHETIC"
TIER_PROTECTED = "TIER_2_PROTECTED"
TIERS = (TIER_SYNTHETIC, TIER_PROTECTED)

SYNTHETIC_MARKER = "NON_PRODUCTION_TEST_FIXTURE"
SYNTHETIC_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "synthetic-inventory.json"

ENV_TIER = "SIGNALNEST_ANCHOR_TIER"
ENV_INVENTORY_PATH = "SIGNALNEST_INVENTORY_PATH"
ENV_INVENTORY_SHA256 = "SIGNALNEST_INVENTORY_SHA256"

# A repository path that must never hold a live inventory again. Its presence is a finding,
# not an input: the containment is only real if re-adding the file is noticed.
PROHIBITED_REPOSITORY_INVENTORY = REPO_ROOT / "infra" / "aws" / "live-resource-inventory.json"

# Fields whose presence inside the inventory would mean the document is supplying its own
# verification. Refused outright — see the module docstring.
#
# GATE 4N-I27O. This tuple is NO LONGER AUTHORITATIVE. It was four exact names, so it caught
# `sha256` because that string is spelled here and would have missed `file_digest`,
# `inventory_hash` or `checksum`; emptying it let a document carrying its own digest validate
# cleanly (executed at Gate 4N-I27K and reproduced at I27O). Recognising bad field NAMES means
# the unrecognised name passes. `self_attesting_fields()` below decides, and it asks two
# questions neither of which depends on this tuple. Retained only as documentation of the
# original four and as a fast pre-filter.
SELF_ATTESTING_FIELDS = ("sha256", "expected_sha256", "canonical_sha256", "digest")

# An integrity-bearing field NAME, by shape rather than by enumeration.
_INTEGRITY_SHAPED_KEY = re.compile(r"sha\d*|digest|hash|checksum|signature|hmac|fingerprint",
                                   re.IGNORECASE)
_HEX_VALUE = re.compile(r"[0-9a-fA-F]{32,}")


def self_attesting_fields(data: dict) -> list[str]:
    """Every top-level field that would let this document verify itself.

    TWO INDEPENDENT QUESTIONS, so neither list nor name can be the single point of failure:

      1. NAME SHAPE — does the key look like an integrity field at all? This catches
         `file_digest` and `inventory_hash`, which the four-name tuple did not.
      2. STRUCTURE, NAME-INDEPENDENT — does the value ACTUALLY equal this document's own
         canonical digest computed with that field removed? A self-attesting document is
         built exactly that way, and this question does not care what the field is called.

    Question 2 is the one that cannot be evaded by renaming. Question 1 is the one that
    catches a placeholder digest that has not been computed yet.
    """
    # GATE 4N-I27R. The traversal is RECURSIVE.
    #
    # THE DEFECT THIS CLOSES. This function inspected only `data.items()` — the top level. Gate
    # 4N-I27Q's security and adversarial lanes both showed the evasion: place the same digest
    # one level down (inside `aliases`, a required nested dict, or inside a list of mappings)
    # and BOTH questions were skipped, because neither was ever asked below the surface. The
    # module's claim that question 2 "cannot be evaded by renaming" was true and beside the
    # point: it was evaded by re-nesting.
    #
    # Every digest candidate is now compared against the canonical digest of the WHOLE document
    # with that one value removed, at whatever depth it sits — so moving it deeper changes
    # nothing, which is the property the claim always needed.
    found: list[str] = []
    _walk_for_self_attestation(data, data, (), found)
    return found


def _prune(node, path: tuple):
    """A copy of `node` with the value at `path` removed. Used to ask, at any depth, whether a
    value equals the digest of the document that would remain without it."""
    if not path:
        return node
    head, rest = path[0], path[1:]
    if isinstance(node, dict):
        out = {k: (_prune(v, rest) if k == head else v) for k, v in node.items()}
        if not rest:
            out.pop(head, None)
        return out
    if isinstance(node, list):
        out = list(node)
        if not rest:
            del out[head]
        else:
            out[head] = _prune(out[head], rest)
        return out
    return node


def _walk_for_self_attestation(node, root, path: tuple, found: list) -> None:
    items = (node.items() if isinstance(node, dict)
             else enumerate(node) if isinstance(node, (list, tuple)) else ())
    for key, value in items:
        here = path + (key,)
        where = ".".join(str(p) for p in here)
        if isinstance(key, str) and _INTEGRITY_SHAPED_KEY.search(key):
            found.append(f"{where!r} (an integrity-shaped field name)")
            continue
        if isinstance(value, str) and _HEX_VALUE.fullmatch(value):
            reduced = _prune(root, here)
            if hashlib.sha256(canonical_bytes(reduced)).hexdigest() == value.lower():
                found.append(f"{where!r} (holds this document's own canonical digest)")
        elif isinstance(value, (dict, list, tuple)):
            _walk_for_self_attestation(value, root, here, found)

REQUIRED_FIELDS = ("buckets_by_role", "trails", "db", "aliases", "lock_table_name")


class InventoryError(Exception):
    """Fail-closed. Never downgraded to a warning, never satisfied by a fallback."""


@dataclass(frozen=True)
class LoadedInventory:
    tier: str
    data: dict
    canonical_sha256: str
    provenance: str
    certifies_production: bool
    source_path: str = ""

    def dig(self, field: str):
        """Dotted path lookup, list indices included: 'trails.0.1', 'buckets_by_role.audit'."""
        node = self.data
        for part in field.split("."):
            if isinstance(node, list):
                idx = int(part)
                if idx >= len(node):
                    return None, False
                node = node[idx]
            elif isinstance(node, dict):
                if part not in node:
                    return None, False
                node = node[part]
            else:
                return None, False
        return node, True

    def redacted(self) -> dict:
        """Safe for ordinary logs: no identifier value is ever printed."""
        return {"tier": self.tier, "canonical_sha256": self.canonical_sha256,
                "certifies_production": self.certifies_production,
                "provenance": self.provenance,
                "fields": sorted(self.data.keys())}


def canonical_bytes(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _validate(data: dict, *, tier: str) -> None:
    for field in REQUIRED_FIELDS:
        if field not in data:
            raise InventoryError(f"inventory is missing required field {field!r}")

    offenders = self_attesting_fields(data)
    if offenders:
        raise InventoryError(
            "inventory carries its own verification value(s): " + "; ".join(offenders) +
            ". An inventory may not supply the value it is verified against; the expected "
            f"hash must arrive separately through {ENV_INVENTORY_SHA256}.")

    is_synthetic = data.get("_classification") == SYNTHETIC_MARKER
    if tier == TIER_PROTECTED and is_synthetic:
        raise InventoryError(
            "a SYNTHETIC inventory was presented for TIER_2_PROTECTED. A tracked fixture "
            "validates the MECHANISM and must never certify production values.")
    if tier == TIER_SYNTHETIC and not is_synthetic:
        raise InventoryError(
            f"TIER_1_SYNTHETIC requires the fixture to be marked {SYNTHETIC_MARKER}. An "
            "unmarked document may be a real inventory, and Tier 1 must never read one.")


def load(env: dict | None = None) -> LoadedInventory:
    env = os.environ if env is None else env
    tier = env.get(ENV_TIER)
    if not tier:
        raise InventoryError(
            f"{ENV_TIER} is not set. The tier must be declared EXPLICITLY; there is no "
            "default and no discovery.")
    if tier not in TIERS:
        raise InventoryError(f"unknown tier {tier!r}; expected one of {TIERS}")

    if tier == TIER_SYNTHETIC:
        if not SYNTHETIC_FIXTURE.exists():
            raise InventoryError(f"the tracked synthetic inventory is missing: {SYNTHETIC_FIXTURE}")
        data = json.loads(SYNTHETIC_FIXTURE.read_text(encoding="utf-8"))
        _validate(data, tier=tier)
        return LoadedInventory(
            tier=tier, data=data, canonical_sha256=hashlib.sha256(canonical_bytes(data)).hexdigest(),
            provenance=f"SYNTHETIC_TEST_FIXTURE: {SYNTHETIC_FIXTURE.relative_to(REPO_ROOT)}",
            certifies_production=False,
            source_path=str(SYNTHETIC_FIXTURE.relative_to(REPO_ROOT)))

    raw_path = env.get(ENV_INVENTORY_PATH)
    expected = env.get(ENV_INVENTORY_SHA256)
    if not raw_path:
        raise InventoryError(
            f"{TIER_PROTECTED} requires {ENV_INVENTORY_PATH}. There is no repository fallback: "
            "reading a live inventory out of the tree is exactly what Gate 4N-I18 removed.")
    if not expected:
        raise InventoryError(
            f"{TIER_PROTECTED} requires {ENV_INVENTORY_SHA256}, supplied SEPARATELY from the "
            "inventory itself. An unverified external file is not evidence.")

    path = Path(raw_path)
    if not path.is_absolute():
        raise InventoryError(f"{ENV_INVENTORY_PATH} must be absolute, got {raw_path!r}")
    if path.is_symlink():
        raise InventoryError(
            f"{ENV_INVENTORY_PATH} points at a SYMLINK: {path}. Protected evidence must be "
            "named directly, not through an indirection that can be repointed after review.")
    try:
        inside_repo = path.resolve().is_relative_to(REPO_ROOT.resolve())
    except AttributeError:  # pragma: no cover - Python < 3.9
        inside_repo = str(path.resolve()).startswith(str(REPO_ROOT.resolve()))
    if inside_repo:
        raise InventoryError(
            f"{ENV_INVENTORY_PATH} points INSIDE the repository ({path}). The live inventory "
            "must live outside the tree; a repository-internal path is the disclosure risk "
            "this loader exists to prevent.")
    if not path.exists():
        raise InventoryError(f"{ENV_INVENTORY_PATH} points at a missing file: {path}")

    raw = path.read_bytes()
    actual_file = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InventoryError(f"protected inventory is not valid JSON: {exc}") from exc

    _validate(data, tier=tier)
    actual_canonical = hashlib.sha256(canonical_bytes(data)).hexdigest()
    if expected not in (actual_file, actual_canonical):
        raise InventoryError(
            "protected inventory HASH MISMATCH: the supplied expected digest matches neither "
            "the file bytes nor the canonical form. Refusing to proceed on an unverified "
            "external document.")

    return LoadedInventory(
        tier=tier, data=data, canonical_sha256=actual_canonical,
        provenance=f"EXTERNAL_PROTECTED_EVIDENCE: verified against {ENV_INVENTORY_SHA256}",
        certifies_production=True, source_path=str(path))


def repository_is_clean() -> tuple[bool, str]:
    """The containment invariant: no live inventory may sit in the tree, ever again."""
    if PROHIBITED_REPOSITORY_INVENTORY.exists():
        return False, (f"{PROHIBITED_REPOSITORY_INVENTORY.relative_to(REPO_ROOT)} exists in the "
                       "working tree. Gate 4N-I18 moved the live inventory outside the "
                       "repository; re-adding it re-creates the first-disclosure risk.")
    return True, "no live inventory in the repository tree"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    clean, why = repository_is_clean()
    if not clean:
        print(f"  {why}", file=sys.stderr)
        print("PROTECTED INVENTORY: containment violated")
        return 1
    try:
        loaded = load()
    except InventoryError as exc:
        print(f"  {exc}", file=sys.stderr)
        print("PROTECTED INVENTORY: fail-closed")
        return 2
    if args.json:
        print(json.dumps(loaded.redacted(), indent=2, ensure_ascii=True))
    else:
        r = loaded.redacted()
        print(f"  tier={r['tier']}  canonical={r['canonical_sha256'][:16]}...  "
              f"certifies_production={r['certifies_production']}")
        print(f"  {why}")
    print("PROTECTED INVENTORY: loaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
