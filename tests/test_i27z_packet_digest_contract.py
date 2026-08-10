"""Gate 4N-I27Z, Agenda E — the reviewer-packet digest contract.

Gate 4N-I27Y's packet declared one `packet_sha256`. It was a digest of the canonical JSON
object; the file reviewers opened was serialised with `indent=1`. Two lanes tried to recompute
it from the distributed bytes and could not — one after eight serialisation attempts — and both
correctly reported that raw-file integrity was never bound.

These tests pin the corrected contract. They are written against BEHAVIOUR — reformat a packet
and see which digest moves — rather than against the module's own constants, because a test that
recomputes a digest the same way the module does can only ever agree with it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import review_packet_digest as rpd  # noqa: E402

PACKET = {"candidate_id": "SYNTHETIC-1", "b": 2, "a": [1, {"z": 0, "y": 1}],
          "nested": {"deep": {"value": "x"}}}


def test_the_two_digests_are_different_values():
    """If they were equal the distinction would be decorative."""
    record = rpd.digests(PACKET)
    assert (record["review_packet_raw_file_sha256"]
            != record["review_packet_canonical_object_sha256"])


def test_pretty_printing_changes_the_raw_digest(tmp_path):
    """The raw digest must track the BYTES — that is its entire job."""
    compact = json.dumps(PACKET, ensure_ascii=True).encode("utf-8")
    pretty = json.dumps(PACKET, indent=4, ensure_ascii=True).encode("utf-8")
    import hashlib
    assert hashlib.sha256(compact).hexdigest() != hashlib.sha256(pretty).hexdigest()


def test_reformatting_does_not_change_the_canonical_digest():
    """The canonical digest must survive insignificant formatting — its entire job."""
    reordered = {"nested": {"deep": {"value": "x"}}, "a": [1, {"y": 1, "z": 0}],
                 "b": 2, "candidate_id": "SYNTHETIC-1"}
    assert (rpd.digests(PACKET)["review_packet_canonical_object_sha256"]
            == rpd.digests(reordered)["review_packet_canonical_object_sha256"])


def test_both_digests_are_recomputable_from_the_distributed_file(tmp_path):
    packet_path = tmp_path / "packet.json"
    payload = rpd.serialize(PACKET)
    packet_path.write_bytes(payload)
    record = rpd.digests(PACKET, raw=payload)
    result = rpd.verify(packet_path, record)
    assert result["both_recomputed_from_the_distributed_file"] is True
    assert result["raw_and_canonical_are_distinct"] is True


def test_a_canonical_digest_in_the_raw_field_is_refused(tmp_path):
    """THE I27Y DEFECT, as a test. Mislabelling must fail, not pass quietly."""
    packet_path = tmp_path / "packet.json"
    payload = rpd.serialize(PACKET)
    packet_path.write_bytes(payload)
    record = rpd.digests(PACKET, raw=payload)
    record["review_packet_raw_file_sha256"] = record["review_packet_canonical_object_sha256"]
    with pytest.raises(rpd.PacketDigestError, match="mislabelled|hash to"):
        rpd.verify(packet_path, record)


# The required set is stated HERE, not read from the module. Gate 4N-I27Z's own falsification
# caught the difference: parametrising over rpd.REQUIRED_FIELDS meant deleting a field from that
# tuple silently deleted its own test, so "omit the raw packet digest" passed. An oracle that
# enumerates from the thing under test can only confirm it.
EXPECTED_REQUIRED_FIELDS = (
    "review_packet_raw_file_sha256", "review_packet_canonical_object_sha256",
    "serialization_format", "encoding", "newline_convention",
    "canonicalization_algorithm", "canonicalization_version")


def test_the_contract_still_requires_every_field_it_is_supposed_to():
    """THE FALSIFICATION THIS CLOSES: shrinking REQUIRED_FIELDS must fail, not go unnoticed."""
    assert set(rpd.REQUIRED_FIELDS) == set(EXPECTED_REQUIRED_FIELDS), (
        "the contract's required-field set changed; a packet digest record that omits the raw "
        "file digest is exactly what Gate 4N-I27Y shipped")


@pytest.mark.parametrize("field", EXPECTED_REQUIRED_FIELDS)
def test_every_required_field_is_mandatory(field, tmp_path):
    packet_path = tmp_path / "packet.json"
    payload = rpd.serialize(PACKET)
    packet_path.write_bytes(payload)
    record = rpd.digests(PACKET, raw=payload)
    record.pop(field, None)
    with pytest.raises(rpd.PacketDigestError):
        rpd.verify(packet_path, record)


def test_a_changed_canonicalization_version_is_refused(tmp_path):
    """A canonical digest is meaningless without the algorithm that produced it."""
    packet_path = tmp_path / "packet.json"
    payload = rpd.serialize(PACKET)
    packet_path.write_bytes(payload)
    record = rpd.digests(PACKET, raw=payload)
    record["canonicalization_version"] = "0"
    with pytest.raises(rpd.PacketDigestError, match="version"):
        rpd.verify(packet_path, record)


def test_a_single_altered_byte_in_the_distributed_file_is_caught(tmp_path):
    packet_path = tmp_path / "packet.json"
    payload = rpd.serialize(PACKET)
    packet_path.write_bytes(payload)
    record = rpd.digests(PACKET, raw=payload)
    packet_path.write_bytes(payload.replace(b"SYNTHETIC-1", b"SYNTHETIC-2"))
    with pytest.raises(rpd.PacketDigestError):
        rpd.verify(packet_path, record)


def test_the_raw_digest_covers_the_file_that_was_written_not_a_reserialisation(tmp_path):
    """If a packet is written by one path and hashed by another, the raw digest must follow the
    file. Gate 4N-I27Y's declared value followed the object instead, which is why it could not
    be reproduced from what reviewers held."""
    packet_path = tmp_path / "packet.json"
    written = (json.dumps(PACKET, indent=4, ensure_ascii=True) + "\n").encode("utf-8")
    packet_path.write_bytes(written)
    record = rpd.digests(PACKET, raw=written)
    assert rpd.verify(packet_path, record)["raw_file_sha256"]
    assert record["review_packet_raw_file_sha256"] != rpd.digests(PACKET)[
        "review_packet_raw_file_sha256"], (
        "the raw digest ignored the supplied bytes and re-serialised instead")
