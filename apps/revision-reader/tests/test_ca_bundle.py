"""Gate 4J.1 — the committed AWS RDS CA bundle is what the build verifies and bakes.

The reader connects with ``sslmode=verify-full`` against this bundle, so its integrity is a
security control. A full-file SHA-256 is pinned in three places that must agree: this test,
the Dockerfile's build-time ``sha256sum -c``, and (implicitly) the bytes on disk. Any
substitution — same length or not — changes the digest; a legitimate refresh is meant to
change it and force PR review. That supersedes per-certificate fingerprint pinning.

Authenticity note (documented residual): this pins byte-STABILITY, not provenance. The
bundle was fetched once from AWS's published truststore
(https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem) and committed; that the
committed bytes are genuinely AWS's is a one-time manual review step, not something this
test can establish.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CA = ROOT / "assets" / "rds-global-bundle.pem"
DOCKERFILE = ROOT / "Dockerfile"

PINNED_SHA256 = "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"


def test_committed_bundle_matches_the_pinned_digest():
    assert CA.is_file(), "the RDS CA bundle must be committed, not downloaded at build time"
    got = hashlib.sha256(CA.read_bytes()).hexdigest()
    assert got == PINNED_SHA256


def test_dockerfile_verifies_the_same_digest():
    # The build's sha256sum -c uses this exact value, so the image bytes equal these bytes.
    assert PINNED_SHA256 in DOCKERFILE.read_text(encoding="utf-8")


def test_bundle_is_certificates_only_no_private_key():
    body = CA.read_text(encoding="utf-8")
    assert body.count("-----BEGIN CERTIFICATE-----") >= 50
    assert "PRIVATE KEY" not in body


def test_bundle_is_not_empty_or_truncated():
    # A real global bundle is ~165 KB; guards a silently emptied/truncated asset.
    assert CA.stat().st_size > 100_000
