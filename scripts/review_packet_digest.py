#!/usr/bin/env python3
"""Review-packet digest contract — Gate 4N-I27Z, Agenda E.

THE DEFECT THIS CLOSES. Gate 4N-I27Y shipped a reviewer packet whose single `packet_sha256`
field was a digest of the canonical JSON OBJECT, while the file the reviewers actually opened was
serialised with `indent=1`. Two lanes independently tried to recompute the declared value from
the distributed bytes and could not; one tested eight serialisations before giving up and
treating the packet as advisory. All six lanes cited the same declared string, so reviewer
IDENTITY agreement held — but RAW-FILE INTEGRITY was never bound at all.

A field consumed as "the packet's digest" that cannot be recomputed from the packet is not a
binding. It is a number everyone copies.

THE CONTRACT. Two digests, never one, and each labelled for what it actually covers:

    review_packet_raw_file_sha256          the exact bytes distributed to reviewers
    review_packet_canonical_object_sha256  the parsed object under a named canonicalisation

The raw digest is what a reviewer can verify with `shasum -a 256 packet.json` and nothing else.
The canonical digest survives insignificant reformatting, which is what makes it useful for
comparing two renderings of the same packet — and useless for proving what was distributed.
Both are recorded, both are independently checkable, and the serialisation metadata says exactly
how each was produced so neither has to be guessed at.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CANONICALIZATION = "json.dumps(sort_keys=True, ensure_ascii=True, separators=(',', ':'))"
CANONICALIZATION_VERSION = "1"
SERIALIZATION = "json.dumps(indent=1, ensure_ascii=True) + '\\n'"
ENCODING = "utf-8"
NEWLINE = "\\n (LF), single trailing newline"


class PacketDigestError(ValueError):
    """Fail-closed. A mislabelled or missing digest is never accepted."""


def canonical_bytes(packet: dict) -> bytes:
    """The object's canonical form. Stable across formatting, useless for byte provenance."""
    return json.dumps(packet, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode(ENCODING)


def serialize(packet: dict) -> bytes:
    """The exact bytes to distribute. Whatever this returns is what the raw digest covers."""
    return (json.dumps(packet, indent=1, ensure_ascii=True) + "\n").encode(ENCODING)


def digests(packet: dict, *, raw: bytes | None = None) -> dict:
    """Both digests plus the metadata needed to recompute either one.

    `raw` may be supplied when the bytes already exist on disk, so the recorded raw digest
    covers the file that was actually written rather than a re-serialisation of the object.
    """
    payload = serialize(packet) if raw is None else raw
    return {
        "review_packet_raw_file_sha256": hashlib.sha256(payload).hexdigest(),
        "review_packet_canonical_object_sha256":
            hashlib.sha256(canonical_bytes(packet)).hexdigest(),
        "serialization_format": SERIALIZATION,
        "encoding": ENCODING,
        "newline_convention": NEWLINE,
        "canonicalization_algorithm": CANONICALIZATION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "raw_file_bytes": len(payload),
    }


REQUIRED_FIELDS = ("review_packet_raw_file_sha256", "review_packet_canonical_object_sha256",
                   "serialization_format", "encoding", "newline_convention",
                   "canonicalization_algorithm", "canonicalization_version")


def verify(path: Path, declared: dict) -> dict:
    """Recompute both digests from the distributed FILE and refuse any mismatch."""
    missing = [f for f in REQUIRED_FIELDS if f not in declared]
    if missing:
        raise PacketDigestError(
            f"the packet digest record is missing {missing}. Both digests and the "
            "canonicalisation version are mandatory: a single unlabelled digest is exactly "
            "what Gate 4N-I27Y shipped, and no reviewer could tell which one it was.")
    if declared["canonicalization_version"] != CANONICALIZATION_VERSION:
        raise PacketDigestError(
            f"canonicalisation version {declared['canonicalization_version']!r} is not the "
            f"version this module implements ({CANONICALIZATION_VERSION!r}); the canonical "
            "digest cannot be compared across versions")

    payload = path.read_bytes()
    raw = hashlib.sha256(payload).hexdigest()
    canonical = hashlib.sha256(canonical_bytes(json.loads(payload.decode(ENCODING)))).hexdigest()

    if raw != declared["review_packet_raw_file_sha256"]:
        raise PacketDigestError(
            f"the distributed bytes hash to {raw}, not the declared "
            f"{declared['review_packet_raw_file_sha256']}. The raw field must cover the file a "
            "reviewer opens; if it holds a canonical-object digest instead, it is mislabelled.")
    if canonical != declared["review_packet_canonical_object_sha256"]:
        raise PacketDigestError(
            f"the canonical object hashes to {canonical}, not the declared "
            f"{declared['review_packet_canonical_object_sha256']}")
    return {"raw_file_sha256": raw, "canonical_object_sha256": canonical,
            "both_recomputed_from_the_distributed_file": True,
            "raw_and_canonical_are_distinct": raw != canonical}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("packet", type=Path)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text(encoding=ENCODING))
    print(json.dumps(digests(packet, raw=args.packet.read_bytes()), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
