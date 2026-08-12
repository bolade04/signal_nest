"""Bounded temporary-expiry authorization (Gate 4N-I19, ADV-A).

WHAT WENT WRONG. Gate 4N-I17's adversarial lane found the stamped expiry was bound by nothing:
`require_valid_expiry` validated SYNTAX only, so a 2020 already-expired stamp and a 2099 stamp
— a 73-year "temporary" grant — both generated cleanly with the whole suite green. The expiry
is the only temporal control on the temporary Stage-A operator grant.

The subtle part, and the reason this file separates its two halves so firmly: Gate 4N-I17 ALSO
had ten passing IAM runtime boundary tests at the same time. Runtime correctness — does
DateLessThan admit a request at instant T — says nothing about whether the window was one an
operator was allowed to grant. Both layers are tested here, and neither is accepted as evidence
for the other.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import expiry_authorization as ea  # noqa: E402
import iam_eval  # noqa: E402

I = ea.ACTIVE_ISSUANCE_UTC
E = ea.ACTIVE_EXPIRY_UTC


def _shift(instant: str, **delta) -> str:
    """An instant a fixed offset from one half of the reviewed pair, in canonical UTC.

    GATE 4N-I28R. These boundary fixtures used to be absolute instants derived by hand from
    whichever pair was active when they were written. That silently rots at every restamp, and
    it had already rotted in the direction that hides a problem: after the I28R restamp the
    case named "exact 24h maximum" still PASSED, but as a 4h24m window — it was no longer
    testing the maximum at all, and no failure said so. A test that keeps its name while losing
    its meaning is worse than one that breaks.

    The offsets below stay LITERAL on purpose. Deriving them from ea.MAX_DURATION or
    ea.MIN_DURATION would make each fixture move together with the very constant it exists to
    bound, so a corrupted bound would still look green — the self-authored-oracle defect this
    chain keeps finding. Anchoring to the pinned pair while hard-coding the offsets means a
    restamp moves the window and nothing else.
    """
    base = datetime.datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    return (base + datetime.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def authorize(expiry, issuance=I, **kw):
    return ea.authorize(issuance=issuance, expiry=expiry, purpose="stage_a_operator", **kw)


# =====================================================================================
# LAYER 1 — authorization: was this window one we were allowed to grant?
# =====================================================================================


def test_the_active_reviewed_pair_is_authorized():
    result = ea.active_pair()
    assert result["authorized"]
    assert result["duration_seconds"] <= ea.MAX_DURATION.total_seconds()


ACCEPTED = {
    "exact 24h maximum": _shift(I, hours=24),
    "exact 15m minimum": _shift(I, minutes=15),
    "the active pair": ea.ACTIVE_EXPIRY_UTC,
}

REFUSED = {
    # THE TWO GATE 4N-I17 DEFECTS
    "2020 already expired": "2020-01-01T00:00:00Z",
    "2099 seventy-three years": "2099-12-31T23:59:59Z",
    # boundary
    "maximum plus one second": _shift(I, hours=24, seconds=1),
    "expiry before issuance": _shift(I, hours=-1),
    "expiry equals issuance": I,
    "shorter than the minimum": _shift(I, minutes=15, seconds=-1),
    # form
    "malformed": "2026-08-01T16:00",
    "placeholder": "<EXPIRY-ISO8601>",
    "naive, no zone": "2026-08-01T16:00:00",
    "offset form naming the same instant": "2026-08-01T18:00:00+02:00",
    "empty": "",
}


@pytest.mark.parametrize("name", sorted(ACCEPTED))
def test_authorized_windows_are_accepted(name):
    assert authorize(ACCEPTED[name])["authorized"]


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_unauthorized_windows_are_refused(name):
    with pytest.raises(ea.ExpiryAuthorizationError):
        authorize(REFUSED[name])


def test_a_missing_issuance_is_refused():
    with pytest.raises(ea.ExpiryAuthorizationError, match="REQUIRED"):
        authorize(ea.ACTIVE_EXPIRY_UTC, issuance="")


def test_a_caller_may_tighten_the_maximum_but_never_raise_it():
    """Otherwise the bound is back under the control of the thing it bounds."""
    assert authorize(_shift(I, hours=2), max_duration=datetime.timedelta(hours=4))
    with pytest.raises(ea.ExpiryAuthorizationError, match="may not raise"):
        authorize(ea.ACTIVE_EXPIRY_UTC, max_duration=datetime.timedelta(days=365 * 100))


# Gate 4N-I26A. The expiry side had eleven refusal cases and the issuance side had ONE
# (empty). Both halves bound the window and both are supplied by the same caller, so a
# malformed issuance is exactly as dangerous as a malformed expiry — it just had no test.
# An asymmetric check is a check that is only half present, which is the shape of defect this
# chain keeps finding; the two sides are now enumerated together.
REFUSED_ISSUANCE = {
    "empty": "",
    "missing": None,
    "malformed": "2026-08-01T14:45",
    "placeholder": "<ISSUANCE-ISO8601>",
    "naive, no zone": "2026-08-01T14:45:00",
    "offset form naming the same instant": "2026-08-01T16:45:00+02:00",
    "not a string": 20260801,
    "after the expiry": _shift(E, hours=1),
    "equal to the expiry": ea.ACTIVE_EXPIRY_UTC,
}


@pytest.mark.parametrize("name", sorted(REFUSED_ISSUANCE))
def test_an_unauthorized_issuance_is_refused(name):
    with pytest.raises(ea.ExpiryAuthorizationError):
        ea.authorize(issuance=REFUSED_ISSUANCE[name], expiry=ea.ACTIVE_EXPIRY_UTC,
                     purpose="stage_a_operator")


@pytest.mark.parametrize("half", ["issuance", "expiry"])
def test_neither_half_of_the_pair_may_be_omitted(half):
    """Gate 4N-I19's rule at the API boundary: one half alone is the unbounded window."""
    kwargs = {"issuance": ea.ACTIVE_ISSUANCE_UTC, "expiry": ea.ACTIVE_EXPIRY_UTC,
              "purpose": "stage_a_operator"}
    kwargs[half] = None
    with pytest.raises(ea.ExpiryAuthorizationError, match="REQUIRED"):
        ea.authorize(**kwargs)


def test_an_unknown_purpose_is_refused():
    with pytest.raises(ea.ExpiryAuthorizationError, match="purpose"):
        ea.authorize(issuance=I, expiry=ea.ACTIVE_EXPIRY_UTC, purpose="whatever")


def test_the_authorized_maximum_is_no_longer_than_twenty_four_hours():
    """A later edit that loosens the bound is the defect returning; pin it."""
    assert ea.MAX_DURATION <= datetime.timedelta(hours=24)


# =====================================================================================
# Every generator enforces it — not just the tests, and not just CI
# =====================================================================================

GENERATORS = {
    "stage_a": ("gen_operator_policies", "bootstrap_temp_policy"),
    "role_bootstrap": ("gen_role_bootstrap_policy", "role_bootstrap_policy"),
    "boundary_bootstrap": ("gen_bootstrap_operator_policy", "bootstrap_operator_policy"),
    "readonly_verifier": ("gen_readonly_verifier_policy", "readonly_verifier_policy"),
}


@pytest.mark.parametrize("name", sorted(GENERATORS))
@pytest.mark.parametrize("expiry", ["2020-01-01T00:00:00Z", "2099-12-31T23:59:59Z",
                                    _shift(I, hours=24, seconds=1)])
def test_every_generator_refuses_an_unauthorized_window(name, expiry):
    import importlib

    module_name, func_name = GENERATORS[name]
    func = getattr(importlib.import_module(module_name), func_name)
    with pytest.raises(ea.ExpiryAuthorizationError):
        func(expiry)


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_every_generator_accepts_the_active_pair(name):
    import importlib

    module_name, func_name = GENERATORS[name]
    func = getattr(importlib.import_module(module_name), func_name)
    assert func(ea.ACTIVE_EXPIRY_UTC)["Statement"]


def test_generation_fails_before_any_output_exists():
    """The refusal must precede policy construction, not filter it afterwards."""
    import gen_operator_policies as gen

    with pytest.raises(ea.ExpiryAuthorizationError):
        gen.bootstrap_temp_policy("2099-12-31T23:59:59Z")


# =====================================================================================
# LAYER 2 — IAM runtime. Correct here proves NOTHING about layer 1.
# =====================================================================================


def _verifier():
    import gen_readonly_verifier_policy as rv

    return rv.readonly_verifier_policy(ea.ACTIVE_EXPIRY_UTC)


ACTION, RESOURCE = "sts:GetCallerIdentity", "*"


def _decide(current_time):
    """Uses decide(), never effect().

    The repository's own vacuous-assertion guard (tests/test_deny_shadow.py) rejects effect()
    here, and it is right to: effect() collapses IMPLICIT_DENY and EXPLICIT_DENY into one
    string, so an assertion built on it cannot tell a control that is present from one that
    was deleted. The expiry boundary is exactly a place where that distinction matters — a
    lapsed window should fall to IMPLICIT_DENY because the Allow stopped matching, not because
    something denied it.
    """
    ctx = {"aws:RequestedRegion": "us-east-1"}
    if current_time is not None:
        ctx["aws:CurrentTime"] = current_time
    return iam_eval.decide(_verifier(), ACTION, RESOURCE, ctx).decision


def test_one_second_before_expiry_is_permitted():
    assert _decide(_shift(E, seconds=-1)) is iam_eval.Decision.EXPLICIT_ALLOW


def test_the_exact_expiry_instant_is_not_permitted():
    """DateLessThan excludes the instant itself."""
    assert _decide(ea.ACTIVE_EXPIRY_UTC) is iam_eval.Decision.IMPLICIT_DENY


def test_one_second_after_expiry_is_not_permitted():
    assert _decide(_shift(E, seconds=1)) is iam_eval.Decision.IMPLICIT_DENY


def test_a_missing_current_time_fails_closed():
    assert _decide(None) is not iam_eval.Decision.EXPLICIT_ALLOW


def test_runtime_correctness_is_not_evidence_of_authorization():
    """The two layers are independent, and this pins that they are checked independently.

    A 2099 policy would satisfy every runtime assertion above — one second before its expiry is
    inside the window, the exact instant is not, and so on. Gate 4N-I17 passed exactly those
    checks while shipping an unbounded window.
    """
    with pytest.raises(ea.ExpiryAuthorizationError):
        authorize("2099-12-31T23:59:59Z")

    # ...and the runtime layer would have been perfectly happy with it.
    hypothetical = {"Version": "2012-10-17", "Statement": [{
        "Sid": "X", "Effect": "Allow", "Action": ACTION, "Resource": RESOURCE,
        "Condition": {"DateLessThan": {"aws:CurrentTime": "2099-12-31T23:59:59Z"}}}]}
    assert iam_eval.decide(hypothetical, ACTION, RESOURCE,
                           {"aws:CurrentTime": "2026-08-01T18:00:00Z"}
                           ).decision is iam_eval.Decision.EXPLICIT_ALLOW


# =====================================================================================
# LAYER 3 — GATE 4N-I26A: the pair has ONE authority, and every copy of it must agree
# =====================================================================================
#
# THE DEFECT THIS CLOSES. The restamp at I26A found the active pair written out as literals in
# two places besides its authoritative home: `scripts/action_classifier.py` and the CI step that
# generates the ReadOnlyVerifier policy. Both were still the SUPERSEDED window. Nothing failed —
# the old values still parsed, still produced a positive duration, and still sat inside the
# 24-hour maximum, so a stale copy is invisible to every check that only asks "is this window
# authorized?" It is only visible to a check that asks "is this window THE ACTIVE ONE?"
#
# That is the shape of the whole restamp risk: a second source for a value with one authority
# does not announce itself when it drifts. `action_classifier` now reads the constant, and the
# tests below make CI's literals unable to drift silently.

CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SUPERSEDED_PAIRS = (
    # Every window this chain has retired. A load-bearing occurrence of one of these is a
    # consumer that a restamp missed, not a historical note.
    ("2026-08-01T00:00:00Z", "2026-08-01T16:00:00Z"),   # retired at Gate 4N-I26A
    ("2026-08-01T14:45:00Z", "2026-08-02T06:00:00Z"),   # retired at Gate 4N-I27M-A
    ("2026-08-02T01:00:00Z", "2026-08-02T18:00:00Z"),   # retired at Gate 4N-I27U-A
    ("2026-08-02T14:00:00Z", "2026-08-03T12:00:00Z"),   # retired at Gate 4N-I28H
    ("2026-08-03T08:23:37Z", "2026-08-04T06:23:37Z"),   # retired at Gate 4N-I28R
    ("2026-08-04T03:59:30Z", "2026-08-05T01:59:30Z"),   # retired at Gate 4N-I28AD
    ("2026-08-04T19:18:10Z", "2026-08-05T17:18:10Z"),   # retired at the Gate 4N-I28AR restamp
    ("2026-08-05T11:32:43Z", "2026-08-06T09:32:43Z"),   # retired at Gate 4N-I28BA
    ("2026-08-06T01:35:35Z", "2026-08-06T23:35:35Z"),   # retired at Gate 4N-I28BF-A4S
    ("2026-08-06T15:30:42Z", "2026-08-07T13:30:42Z"),   # retired at Gate 4N-I28BG-A2
    ("2026-08-06T22:44:33Z", "2026-08-07T20:44:33Z"),   # retired at Gate 4N-I28BH-R
    ("2026-08-07T08:14:44Z", "2026-08-08T06:14:44Z"),   # retired at Gate 4N-I28BH-B-R
    ("2026-08-08T03:51:41Z", "2026-08-09T01:51:41Z"),   # retired at Gate 4N-I28BH-B0w-R2-R
    ("2026-08-08T16:45:59Z", "2026-08-09T14:45:59Z"),   # retired at Gate 4N-I28BH-B0w-R2-SLICE1
    ("2026-08-08T23:19:50Z", "2026-08-09T21:19:50Z"),   # retired at Gate 4N-I28BH-B0w-R2-SLICE1-CLOSED-CAPABILITY-REDESIGN
    ("2026-08-09T06:36:36Z", "2026-08-10T04:36:36Z"),   # retired at Gate 4N-I28BH-B0a-SLICE2
    ("2026-08-09T17:00:00Z", "2026-08-10T15:00:00Z"),   # retired at Gate 4N-I28BH-B-SLICE3
    ("2026-08-10T00:00:00Z", "2026-08-10T22:00:00Z"),   # retired at Gate 4N-I28BH-B-ARCHITECTURAL-ADJUDICATION
    ("2026-08-10T06:00:00Z", "2026-08-11T04:00:00Z"),   # retired at the Phase-4 expiry-authorization pin remediation
)


def _verifier_policy_step() -> str:
    """The body of the CI step that generates the verifier policy, by exact step id."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    marker = "\n        id: verifier_policy\n"
    assert marker in workflow, "the verifier_policy step id is gone; this test is now vacuous"
    after = workflow.split(marker, 1)[1]
    # Up to the next step boundary, so a neighbouring step's flags can never satisfy this.
    return after.split("\n      - name:", 1)[0]


def test_ci_supplies_the_authoritative_pair_and_not_a_copy_of_an_older_one():
    body = _verifier_policy_step()
    assert f"--issuance {ea.ACTIVE_ISSUANCE_UTC}" in body, (
        "the CI verifier_policy step does not pass the ACTIVE issuance. A restamp that updates "
        "scripts/expiry_authorization.py and leaves this literal behind makes CI generate under "
        "a superseded window while every local check reports the new one.")
    assert f"--expiry {ea.ACTIVE_EXPIRY_UTC}" in body, (
        "the CI verifier_policy step does not pass the ACTIVE expiry")


def test_ci_never_supplies_one_half_of_the_pair_without_the_other():
    """Gate 4N-I19's rule, asserted rather than commented: a free-floating expiry is what let
    Gate 4N-I17 ship a window nothing bounded."""
    body = _verifier_policy_step()
    assert ("--issuance" in body) == ("--expiry" in body), (
        "issuance and expiry must travel together; one alone reintroduces the unbounded window")


def _load_bearing_pair_positions(text: str) -> list[tuple[str, str]]:
    """Every position where a timestamp literal ACTS AS the active pair. Returns (role, value).

    POSITION, NOT PRESENCE. A first draft of this check scanned for the retired strings anywhere
    in a file and immediately flagged three false positives: the restamp's own explanatory
    comment, and a fabricated certification artifact whose `certified_at_utc` happens to be the
    same instant. The fix is not a list of exempt paths — that is the hand-authored-list defect
    this chain keeps paying for, and it would grow silently wrong. A value is load-bearing when
    it is ASSIGNED to the active constants or PASSED as the generator's issuance/expiry flag;
    everywhere else the same characters are data, and data is allowed to be historical.
    """
    found = []
    for role, pattern in (
        ("ACTIVE_ISSUANCE_UTC assignment", r'^\s*ACTIVE_ISSUANCE_UTC\s*=\s*"([^"]+)"'),
        ("ACTIVE_EXPIRY_UTC assignment", r'^\s*ACTIVE_EXPIRY_UTC\s*=\s*"([^"]+)"'),
        ("--issuance argument", r'--issuance[ =]+([0-9T:\-]+Z)'),
        ("--expiry argument", r'--expiry[ =]+([0-9T:\-]+Z)'),
    ):
        for match in re.finditer(pattern, text, re.M):
            found.append((role, match.group(1)))
    return found


def test_no_superseded_active_pair_survives_in_a_load_bearing_position():
    retired = {v for pair in SUPERSEDED_PAIRS for v in pair}
    offenders = []
    for path in list((REPO_ROOT / "scripts").rglob("*.py")) + \
            list((REPO_ROOT / "tests").rglob("*.py")) + [CI_WORKFLOW]:
        for role, value in _load_bearing_pair_positions(path.read_text(encoding="utf-8")):
            if value in retired:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {role} = retired {value}")
    assert not offenders, (
        "a superseded active value survives in a load-bearing position:\n  " +
        "\n  ".join(offenders))


def test_every_load_bearing_position_holds_the_one_authoritative_value():
    """The complement, and the half that actually catches a missed consumer.

    Absence of a RETIRED value proves nothing on its own — a typo, or a window retired before
    anyone thought to register it, passes the check above trivially. This direction requires
    every load-bearing position to hold the CURRENT authoritative value, so an unregistered
    third value is caught too. Checking one direction was the I25 finding; this is both.
    """
    allowed = {ea.ACTIVE_ISSUANCE_UTC, ea.ACTIVE_EXPIRY_UTC}
    wrong = []
    positions = 0
    for path in list((REPO_ROOT / "scripts").rglob("*.py")) + \
            list((REPO_ROOT / "tests").rglob("*.py")) + [CI_WORKFLOW]:
        for role, value in _load_bearing_pair_positions(path.read_text(encoding="utf-8")):
            positions += 1
            if value not in allowed:
                wrong.append(f"{path.relative_to(REPO_ROOT)}: {role} = {value}")
    assert positions >= 4, (
        f"only {positions} load-bearing positions found; the two constants and CI's two flags "
        "are the minimum, so the detector has stopped seeing them")
    assert not wrong, ("a load-bearing position holds a value that is not the authoritative "
                       "pair:\n  " + "\n  ".join(wrong))


def test_active_pair_actually_validates_rather_than_reporting_its_constants(monkeypatch):
    """FOUND BY THIS GATE'S OWN FALSIFICATION. Replacing active_pair()'s body with a dict
    literal carrying the right numbers passed every test — because every test checked the
    VALUES, and a literal supplies values perfectly. Nothing required the function to run the
    authorization at all, so it could have stopped validating silently.

    The fix is to check the BEHAVIOUR that only real validation can produce: point the constant
    at a window outside the envelope and require the refusal.
    """
    monkeypatch.setattr(ea, "ACTIVE_EXPIRY_UTC", "2099-12-31T23:59:59Z")
    with pytest.raises(ea.ExpiryAuthorizationError):
        ea.active_pair()


def test_the_guard_cli_exits_non_zero_when_the_pair_is_unauthorized():
    """ALSO FOUND BY FALSIFICATION. The CLI's refusal branch could return 0 instead of 2 and no
    test noticed, because every test called authorize() directly and none ran the command CI
    runs. A guard whose failure path is never executed is a guard nobody has seen fail.
    """
    import subprocess

    ok = subprocess.run([sys.executable, "scripts/expiry_authorization.py",
                         "--issuance", ea.ACTIVE_ISSUANCE_UTC, "--expiry", ea.ACTIVE_EXPIRY_UTC],
                        cwd=REPO_ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, f"the authorized pair must pass the CLI: {ok.stdout}{ok.stderr}"

    for label, issuance, expiry in (
        ("2099", ea.ACTIVE_ISSUANCE_UTC, "2099-12-31T23:59:59Z"),
        ("2020", ea.ACTIVE_ISSUANCE_UTC, "2020-01-01T00:00:00Z"),
        ("expiry before issuance", ea.ACTIVE_ISSUANCE_UTC, _shift(I, hours=-1)),
        ("placeholder", ea.ACTIVE_ISSUANCE_UTC, "<EXPIRY-ISO8601>"),
    ):
        bad = subprocess.run([sys.executable, "scripts/expiry_authorization.py",
                              "--issuance", issuance, "--expiry", expiry],
                             cwd=REPO_ROOT, capture_output=True, text=True)
        assert bad.returncode != 0, (
            f"the CLI exited 0 on an unauthorized window ({label}); CI grades this step by its "
            f"exit code, so a zero here means the guard cannot fail the job:\n{bad.stdout}")
        assert "refused" in (bad.stdout + bad.stderr)


def test_no_script_assigns_a_timestamp_to_its_own_issuance_or_expiry_name():
    """FOUND BY FALSIFICATION, and the most useful of the three.

    A generator can hold `expiry = "2026-08-02T05:00:00Z"` — an independent second source for a
    value with one authority — and the earlier position check walked straight past it, because
    that check's idea of a "load-bearing position" was itself a hand-authored list of two
    assignment names and two CLI flags. This walks the AST instead: ANY assignment binding a
    date-shaped literal to a name mentioning issuance or expiry is a competing authority,
    whatever it is called.
    """
    import ast

    offenders = []
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value.value):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = getattr(target, "id", "")
                if not re.search(r"issuance|expiry", name, re.I):
                    continue
                if name in ("ACTIVE_ISSUANCE_UTC", "ACTIVE_EXPIRY_UTC") and \
                        path.name == "expiry_authorization.py":
                    continue                      # the one authority
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} {name} = {value.value!r}")
    assert not offenders, (
        "a script binds a hardcoded instant to its own issuance/expiry name, creating a second "
        "authority that a restamp will silently leave behind:\n  " + "\n  ".join(offenders))


def test_the_active_pair_is_exactly_the_authorized_i28r_window():
    """Pinned so a later edit cannot quietly widen the window it replaced.

    The retired pair was 22h and this one is also 22h — a restamp is a NEW window for the next
    gate, never an extension of the one that ended, so equal duration is the expected shape and
    a longer one would need its own justification. Neither is near the maximum, and that is the
    point — the maximum is a ceiling, never a target.
    """
    pair = ea.active_pair()
    assert pair["issuance_utc"] == "2026-08-12T05:00:00Z"
    assert pair["expiry_utc"] == "2026-08-13T03:00:00Z"
    assert pair["duration_seconds"] == 79200, "22h in seconds"
    assert pair["duration_seconds"] <= ea.MAX_DURATION.total_seconds()


def test_gen_bootstrap_operator_policy_has_no_issuance_cli_escape_hatch():
    """The window is bounded from the reviewed ACTIVE_ISSUANCE_UTC pin, never from a
    caller-supplied issuance. gen_bootstrap_operator_policy therefore exposes only --hash and
    --expiry; a --issuance flag would let a caller move the lower bound and manufacture an
    arbitrarily long window past the pin. argparse rejects the unknown flag with exit 2.
    """
    import subprocess

    valid_expiry = _shift(ea.ACTIVE_ISSUANCE_UTC, hours=6)
    rv = subprocess.run(
        [sys.executable, "scripts/gen_bootstrap_operator_policy.py",
         "--expiry", valid_expiry, "--issuance", ea.ACTIVE_ISSUANCE_UTC],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert rv.returncode != 0, (
        "gen_bootstrap_operator_policy accepted a --issuance flag; the executor window must be "
        "bounded from the reviewed pin, not from caller input")
    assert "unrecognized arguments" in rv.stderr or "issuance" in rv.stderr
