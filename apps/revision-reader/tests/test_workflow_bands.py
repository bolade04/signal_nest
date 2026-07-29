"""Anti-drift guard for the two in-image reader verification bands (Gate 4J.2).

`.github/workflows/ci.yml` and `.github/workflows/reader-publish.yml` each replay the reader's
config/connect behaviour against the built image. They are deliberately NOT consolidated
(two independent executions, different baked hosts), which means they can silently DIVERGE —
which is exactly what happened before 4J.2: reader-publish.yml kept the pre-baked-host
assertions (`expect 52` for db.invalid) that the current reader returns 51 for.

This test makes divergence loud. It:
  1. Extracts the `expect <code> [-e DATABASE_URL=...]` probes from BOTH bands, normalises each
     band's own baked-host token to `<BAKED>`, and asserts both equal ONE canonical table.
  2. Runs each canonical case through the REAL reader (fake psycopg, monkeypatched pins,
     scoped env) and asserts the reader actually produces the canonical exit code — so the
     fixture cannot drift from reader.py. Redirect cases must also make ZERO connect attempts.
  3. Requires the dangerous case CLASSES to be present (a count floor alone is defeated by
     adding cheap cases while deleting dangerous ones).
  4. Asserts the removed false controls do not reappear as prose, `--network none` guards every
     docker run, the baked host is never inlined into a run: body, and the reader-run legend
     names the tamper meaning.

Stdlib only (no PyYAML). Every parse/locate failure FAILS rather than skips.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from revision_reader import reader as R  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
CI = REPO / ".github" / "workflows" / "ci.yml"
PUBLISH = REPO / ".github" / "workflows" / "reader-publish.yml"
RUN = REPO / ".github" / "workflows" / "reader-run.yml"

for _p in (CI, PUBLISH, RUN):
    assert _p.is_file(), f"workflow not found: {_p} — anti-drift test cannot run (fail, not skip)"

# ------------------------------------------------------------------ #
# Canonical case table. `<BAKED>` is the placeholder for whichever host a band bakes.
# Each entry: (expected_exit, normalised_dsn_or_None, class_label).
# ------------------------------------------------------------------ #
BAKED = "<BAKED>"
_NUL = f"postgresql+psycopg://app_role:s3cr3t%00pw@{BAKED}:5432/signalnest?sslmode=require"
_MYSQL = f"mysql://app_role:pw@{BAKED}:5432/signalnest?sslmode=require"
_MULTI = f"postgresql://app_role:pw@evil.invalid,{BAKED}:5432/d?sslmode=require"
_POS1 = f"postgresql+psycopg://app_role:s3cr3tpw@{BAKED}:5432/signalnest?sslmode=require"
_POS2 = f"postgresql://app_role:p%40ss@{BAKED}:5432/signalnest?sslmode=require"
CANONICAL = [
    (51, None, "no_dsn"),
    (51, "postgresql://u:pw@attacker.example.com:5432/d?sslmode=require", "arbitrary_host"),
    (51, "postgresql://u:pw@203.0.113.9:5432/d?sslmode=require", "arbitrary_ip"),
    (51, "postgresql://u:pw@evil.invalid%2Cdb.invalid[v1.x]/d?sslmode=require", "bracket"),
    (51, _NUL, "decoded_nul_password"),
    (51, _MYSQL, "wrong_scheme"),
    (51, _MULTI, "multi_host"),
    (51, "postgresql://u:p@ss@evil.invalid:443/d?sslmode=require", "multiple_at"),
    (52, _POS1, "positive_control"),
    (52, _POS2, "positive_control"),
]
CANONICAL_SET = {(code, dsn) for code, dsn, _ in CANONICAL}
REDIRECT_CLASSES = {"no_dsn", "arbitrary_host", "arbitrary_ip", "bracket",
                    "decoded_nul_password", "wrong_scheme", "multi_host", "multiple_at"}

# Phrases describing controls the baked-host reader does NOT implement. Must never reappear.
STALE_PHRASES = [
    "port pin", "single-destination guarantee", "sslmode is PARSED",
    "allowlist of exactly", "TLS not required", "composes with the SG",
    "security group to close that path",
]

_EXPECT_RE = re.compile(
    r"""^\s*expect\s+(\d+)\s*(?:-e\s+DATABASE_URL=(['"])(?P<dsn>.*?)\2\s*)?(?:\#.*)?$"""
)


def _extract_band(path: Path, anchor: str, baked_token: str):
    """Return the ordered list of (code, normalised_dsn_or_None) probes from the reader band.

    The band starts at the first line containing `anchor` and ends at the next step (`- name:`)
    or the pins step. Fails (not skips) if the anchor is not found or no probes are extracted.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if anchor in ln), None)
    assert start is not None, f"reader band anchor {anchor!r} not found in {path.name}"
    probes = []
    for ln in lines[start + 1:]:
        if re.match(r"\s*- name:", ln) or "baked pins" in ln.lower() or "== (7)" in ln:
            break
        m = _EXPECT_RE.match(ln)
        if not m:
            continue
        code = int(m.group(1))
        dsn = m.group("dsn")
        if dsn is not None:
            dsn = dsn.replace(baked_token, BAKED)
        probes.append((code, dsn))
    assert probes, f"no expect-probes extracted from {path.name} after {anchor!r} (fail, not skip)"
    return probes


# BOTH bands reference the baked host in DSN text as the shell variable ${BAKED_HOST}
# (ci.yml assigns it a synthetic literal; reader-publish.yml sources it from env vars). The
# literal only appears in ci.yml's assignment line, never in a probe DSN — so the token to
# normalise in DSN text is the same for both.
_BAKED_TOKEN = "${BAKED_HOST}"
CI_PROBES = _extract_band(CI, "destination is BAKED", _BAKED_TOKEN)
PUBLISH_PROBES = _extract_band(PUBLISH, "Destination is baked", _BAKED_TOKEN)


# ------------------------------------------------------------------ #
# A3 — both bands normalise to ONE canonical table.
# ------------------------------------------------------------------ #
def test_ci_band_matches_canonical():
    assert set(CI_PROBES) == CANONICAL_SET, set(CI_PROBES) ^ CANONICAL_SET


def test_publish_band_matches_canonical():
    assert set(PUBLISH_PROBES) == CANONICAL_SET, set(PUBLISH_PROBES) ^ CANONICAL_SET


def test_both_bands_agree_after_normalisation():
    assert set(CI_PROBES) == set(PUBLISH_PROBES)


# ------------------------------------------------------------------ #
# A6 — non-vacuity: a real band with a real floor of probes.
# ------------------------------------------------------------------ #
def test_bands_are_non_vacuous():
    assert len(CI_PROBES) >= 10 and len(PUBLISH_PROBES) >= 10
    for probes in (CI_PROBES, PUBLISH_PROBES):
        assert len({c for c, _ in probes}) >= 2, "all cases share one exit code (vacuous)"


# ------------------------------------------------------------------ #
# A1/A2 — a positive control exists and its host is the baked token (exact authority).
# A5 — every non-baked-host probe expects 51.
# ------------------------------------------------------------------ #
def _authority_host(dsn: str) -> str | None:
    # host is between the (last) '@' and the next ':' or '/'
    after_at = dsn.rsplit("@", 1)[-1]
    return re.split(r"[:/]", after_at, maxsplit=1)[0] or None


@pytest.mark.parametrize("probes", [CANONICAL_SET])
def test_positive_control_names_the_baked_host(probes):
    pos = [(c, d) for c, d in probes if c == 52]
    assert pos, "no positive (52) control"
    for _, dsn in pos:
        assert _authority_host(dsn) == BAKED, f"52-case host is not the baked token: {dsn}"


def test_non_baked_hosts_all_expect_51():
    for code, dsn in CANONICAL_SET:
        if dsn is None:
            continue
        host = _authority_host(dsn)
        if host != BAKED:
            assert code == 51, f"non-baked host {host!r} must expect 51, got {code}: {dsn}"


# ------------------------------------------------------------------ #
# A10 — the dangerous case CLASSES are present (count floor is not enough).
# ------------------------------------------------------------------ #
def test_all_required_case_classes_present():
    classes = {label for _, _, label in CANONICAL}
    required = {"no_dsn", "arbitrary_host", "arbitrary_ip", "bracket",
                "decoded_nul_password", "wrong_scheme", "multi_host", "multiple_at",
                "positive_control"}
    assert required <= classes, required - classes


# ------------------------------------------------------------------ #
# A9 — executable oracle: the canonical codes are what the REAL reader produces, and
# redirect cases make ZERO connect attempts. Ties the fixture to reader.py.
# ------------------------------------------------------------------ #
FAKE_BAKED_HOST = "baked.example.test"


def _install_fake_psycopg(monkeypatch):
    state = {"connects": 0}
    mod = types.ModuleType("psycopg")

    def connect(*args, **kwargs):
        state["connects"] += 1
        raise RuntimeError("no network in oracle")  # -> EXIT_CONNECT_FAILED (52)

    mod.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    return state


@pytest.mark.parametrize("code,dsn,label", CANONICAL)
def test_oracle_reader_produces_canonical_code(monkeypatch, tmp_path, code, dsn, label):
    ca = tmp_path / "rds.pem"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\n" + b"x" * 2000)
    monkeypatch.setattr(R._pinned, "EXPECTED_DB_HOST", FAKE_BAKED_HOST)
    monkeypatch.setattr(R._pinned, "EXPECTED_DB_NAME", "signalnest")
    monkeypatch.setattr(R._pinned, "EXPECTED_DB_USER", "app_role")
    monkeypatch.setattr(R._pinned, "CA_BUNDLE_PATH", str(ca))
    state = _install_fake_psycopg(monkeypatch)

    if dsn is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", dsn.replace(BAKED, FAKE_BAKED_HOST))

    rc = R.main([])
    assert rc == code, f"[{label}] reader returned {rc}, canonical says {code}: {dsn}"
    if label in REDIRECT_CLASSES:
        assert state["connects"] == 0, f"[{label}] a redirect case reached connect(): {dsn}"
    if code == 52:
        assert state["connects"] == 1, f"[{label}] positive control did not reach connect(): {dsn}"


def test_canonical_codes_map_to_reader_exit_constants():
    # A renumber of the reader's exit codes must break this, not silently pass.
    seen = {c for c, _, _ in CANONICAL}
    assert seen == {R.EXIT_CONFIG_FAILED, R.EXIT_CONNECT_FAILED} == {51, 52}


# ------------------------------------------------------------------ #
# A4 — the removed false controls do not reappear (matched outside DSN string literals).
# ------------------------------------------------------------------ #
def _prose(path: Path) -> str:
    # Drop DATABASE_URL='...' literals so a probe DSN can't self-trip a phrase match.
    return re.sub(r"DATABASE_URL=(['\"]).*?\1", "", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", [CI, PUBLISH])
def test_no_stale_control_phrases(path):
    prose = _prose(path)
    for phrase in STALE_PHRASES:
        assert phrase not in prose, f"stale control phrase {phrase!r} reappeared in {path.name}"


def test_reader_run_legend_names_the_tamper_meaning():
    text = RUN.read_text(encoding="utf-8")
    assert "TLS not required" not in text, "stale exit-51 legend still present in reader-run.yml"
    assert "tamper" in text.lower(), "reader-run legend must name the host-tamper meaning of 51"


# ------------------------------------------------------------------ #
# A7 — every docker run in each band is --network none.
# A8 — the baked host is never inlined via ${{ vars.* }} in a run: body.
# A12/A1 — the expect helper is fail-closed and passes flags BEFORE the image.
# A11 — the publish workflow verifies its own baked pins and CA.
# ------------------------------------------------------------------ #
def test_reader_docker_runs_use_network_none():
    # Scope to docker runs against the READER image (tracked by the most recent image=
    # assignment), so unrelated jobs (e.g. the api import check) are not swept in. A reader
    # run that reached the network could try to authenticate to the real staging RDS.
    for path in (CI, PUBLISH):
        cur_image = ""
        for ln in path.read_text(encoding="utf-8").splitlines():
            m = re.search(r'image="([^"]+)"', ln)
            if m:
                cur_image = m.group(1)
            if "docker run" not in ln:
                continue
            targets_reader = "signalnest-revision-reader" in ln or (
                '"$image"' in ln and "revision-reader" in cur_image
            )
            if targets_reader:
                assert "--network none" in ln, (
                    f"{path.name}: reader docker run without --network none: {ln.strip()}"
                )


def test_publish_does_not_inline_vars_into_run_bodies():
    # The baked host must arrive via env:, never ${{ vars.* }} inside a run: script.
    text = PUBLISH.read_text(encoding="utf-8")
    in_run = False
    for ln in text.splitlines():
        if re.match(r"\s*run:\s*\|", ln):
            in_run = True
            continue
        if re.match(r"\s*(- name:|env:|with:|uses:)", ln):
            in_run = False
        if in_run:
            assert "${{ vars." not in ln, f"vars.* inlined into a run body: {ln.strip()}"


def test_expect_helper_is_fail_closed_and_orders_args():
    for path in (CI, PUBLISH):
        text = path.read_text(encoding="utf-8")
        assert 'rc" -ne "$want"' in text, f"{path.name}: expect() not fail-closed on rc"
        # "$@" must precede "$image" — flags after the image become argv (every probe -> 50).
        assert re.search(r'docker run[^\n]*"\$@"\s+"\$image"', text), \
            f"{path.name}: expect() must pass \"$@\" before \"$image\""


def test_publish_verifies_its_own_baked_pins_and_ca():
    text = PUBLISH.read_text(encoding="utf-8")
    assert "Baked pins and the CA bundle are present" in text, "publish missing baked-pins step"
    assert "byte-identical to the reviewed, pinned asset" in text, "publish missing CA step"
    assert "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3" in text


# ------------------------------------------------------------------ #
# A13 — the in-process test that actually proves destination authenticity still exists.
# ------------------------------------------------------------------ #
def test_connect_kwargs_unit_test_still_present():
    t = (Path(__file__).resolve().parent / "test_reader.py").read_text(encoding="utf-8")
    assert "def test_connect_uses_discrete_kwargs_and_never_forwards_the_dsn" in t
