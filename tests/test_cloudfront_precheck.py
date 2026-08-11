"""Offline tests for the CloudFront pre-execution inventory (Gate 4N-I5).

Restores a precheck inherited from Gate 4N-H4 and dropped in 4N-I3/I4. Never calls AWS —
`evaluate()` is pure, and `collect()` (the only function that touches AWS) is not used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cloudfront_precheck as cf  # noqa: E402

import protected_inventory  # noqa: E402

# GATE 4N-I18, SEC-1: the expected edge identifiers come from the tier-resolved inventory,
# never from a repository file carrying live AWS-assigned ids.
EXPECTED = protected_inventory.load().data["cloudfront"]
GOOD = {
    "distributions": [{"Id": EXPECTED["distribution_id"], "Status": "Deployed", "Enabled": True,
                       "Aliases": {"Items": ["web.example"]}, "Origins": {"Items": [{"Id": "spa"}]}}],
    "origin_access_controls": [{"Id": EXPECTED["oac_id"]}],
}


def test_expected_surface_is_clean():
    assert cf.evaluate(GOOD, EXPECTED)["clean"]


def test_getdistributionconfig_is_never_used():
    """Provider source proves it is not required; the precheck must not reintroduce it."""
    assert "cloudfront:GetDistributionConfig" in cf.DELIBERATELY_NOT_USED
    assert "cloudfront:GetDistributionConfig" not in cf.REFRESH_READS
    assert "cloudfront:GetDistributionConfig" not in cf.INVENTORY_READS


def test_permission_classes_are_distinct():
    """Conflating refresh reads with one-time inventory is how the check went missing."""
    assert set(cf.REFRESH_READS) < set(cf.INVENTORY_READS), "inventory must be a strict superset"
    assert not set(cf.INVENTORY_READS) & set(cf.MUTATIONS_NEVER_GRANTED)


@pytest.mark.parametrize("mutate,expect", [
    (lambda i: i["distributions"].append({"Id": "EROGUE", "Status": "Deployed", "Enabled": True,
                                          "Origins": {"Items": [{"Id": "x"}]}}), "found 2"),
    (lambda i: i["origin_access_controls"].append({"Id": "EXTRA"}), "found 2"),
    (lambda i: i["distributions"][0].__setitem__("Id", "EWRONG"), "identity mismatch"),
    (lambda i: i["distributions"][0].__setitem__("Status", "InProgress"), "status"),
    (lambda i: i["distributions"][0].__setitem__("Enabled", False), "Enabled"),
    (lambda i: i["distributions"][0].__setitem__("Origins", {"Items": []}), "no origins"),
    (lambda i: i["origin_access_controls"][0].__setitem__("Id", "EWRONG"), "identity mismatch"),
    (lambda i: i.__setitem__("distributions", []), "found 0"),
])
def test_each_deviation_fails_closed(mutate, expect):
    import copy
    inventory = copy.deepcopy(GOOD)
    mutate(inventory)
    result = cf.evaluate(inventory, EXPECTED)
    assert not result["clean"]
    assert any(expect in f for f in result["findings"]), result["findings"]


def test_alias_assertion_is_opt_in():
    """aliases come from a git-ignored tfvars, so null must mean 'do not assert'."""
    assert EXPECTED.get("aliases") is None
    assert cf.evaluate(GOOD, dict(EXPECTED, aliases=["other.example"]))["clean"] is False
