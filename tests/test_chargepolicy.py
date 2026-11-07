"""Reading macOS' native charge limit and explaining why the cell is parked."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chargepolicy import (parse_battlimit, describe_reason, plain_language,
                         native_limit_is_enforcing)  # noqa: E402

REAL = """
 chargeSocLimitReason = manualChargeLimit; chargeSocLimitSoc = 80; chargeSocLimitDrain = 1;
 chargeSocLimitIsEOC = 1;
"""

MULTIPLE = """
{ chargeSocLimitReason = optimizedChargeLimit; chargeSocLimitSoc = 80; }
{ chargeSocLimitReason = manualChargeLimit; chargeSocLimitSoc = 65; chargeSocLimitDrain = 0; }
"""


def test_parses_a_real_manual_limit():
    policy = parse_battlimit(REAL)
    assert policy["supported"] is True
    assert policy["limit"] == 80
    assert policy["reason"] == "manualChargeLimit"
    assert policy["entries"][0]["drain"] == 1


def test_no_limit_is_reported_as_supported_and_absent():
    assert parse_battlimit("no battery level limits") == {
        "supported": True, "limit": None, "reason": "none", "entries": []}
    assert parse_battlimit("")["supported"] is False


def test_manual_limit_beats_apples_optimised_one():
    policy = parse_battlimit(MULTIPLE)
    assert policy["limit"] == 65
    assert policy["reason"] == "manualChargeLimit"
    assert len(policy["entries"]) == 2


def test_reason_codes_are_decoded_without_overclaiming():
    assert describe_reason(0x10000000) == (
        "state of charge is at the configured limit (heuristic decode of code 0x10000000)")
    assert "heuristic" in describe_reason(0x00100004)
    assert describe_reason(None) == "unknown"
    assert describe_reason(0) == "no reason reported - charging is allowed"
    assert "not in the known table" in describe_reason(0x80000000)


def test_parked_at_limit_versus_actually_discharging():
    assert native_limit_is_enforcing(80, 80, False) is True
    assert native_limit_is_enforcing(80, 60, True) is False
    assert native_limit_is_enforcing(None, 80, False) is False


def test_plain_language_tells_the_user_the_truth():
    policy = {"limit": 80, "smc": {"charge_disable": False, "adapter_cloak": True}}
    assert "holding the battery at its 80% charge limit" in plain_language(policy, 80, False)
    assert "charging is stopped by a charge-limit tool" in plain_language(
        {"limit": None, "smc": {"charge_disable": True}}, 60, False)
    assert plain_language({"limit": None, "smc": None}, 60, False) == "no charge limit is configured"
