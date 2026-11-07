"""What macOS is doing with the charge, and what a guard could act on.

Two separate questions get conflated in every "battery saver" app:

  1. Is the battery *charging*?  macOS answers this natively on Apple Silicon with
     a Charge Limit (macOS 26.4+, 80-100% in System Settings). It is readable
     without root through `pmset -g battlimit`. ChargeWatch's headline feature is
     this same thing, extended below 80% by writing an SMC key from a root daemon.
  2. Is the battery *supplying power*?  No charge limit can prevent that. When the
     load exceeds what the adapter can deliver, the cell feeds the gap. That is the
     only thing power-guard can actually control, and it controls it by capping load.

`pmset` can only read the limit; there is no `pmset -a battlimit`. Writing the SMC
charge keys requires root, and the keys must exist on the machine - which they do
not on this one (see smc.smc_charge_capability()).
"""
from __future__ import annotations

import re
import subprocess

BATTLIMIT_CMD = ["pmset", "-g", "battlimit"]

# NotChargingReason codes seen in AppleSmartBattery's ChargerData. These are not
# documented by Apple; the names below are the ones the community has converged on
# and are reported as "heuristic" so nobody mistakes them for ground truth.
REASON_BITS = {
    0x00000001: "battery is full",
    0x00000004: "temperature outside the charging range",
    0x00100000: "optimized battery charging is holding the charge",
    0x01000000: "charge has been disabled by a tool",
    0x10000000: "state of charge is at the configured limit",
}


def parse_battlimit(text: str) -> dict:
    """Turn `pmset -g battlimit` output into {limit, reason, entries}. Pure."""
    text = text.strip()
    if not text:
        return {"supported": False, "limit": None, "reason": None, "entries": []}
    if "no battery level limits" in text.lower():
        return {"supported": True, "limit": None, "reason": "none", "entries": []}
    entries = []
    for chunk in text.split("}"):
        soc = re.search(r"chargeSocLimitSoc\s*=\s*(\d+)", chunk)
        if not soc:
            continue
        reason = re.search(r"chargeSocLimitReason\s*=\s*([A-Za-z]+)", chunk)
        drain = re.search(r"chargeSocLimitDrain\s*=\s*(\d+)", chunk)
        entries.append({
            "soc": int(soc.group(1)),
            "reason": reason.group(1) if reason else None,
            "drain": int(drain.group(1)) if drain else 0,
        })
    if not entries:
        return {"supported": True, "limit": None, "reason": "unknown", "entries": []}
    # A manual limit is the user's intent; anything else is Apple's optimisation.
    manual = next((e for e in entries if e["reason"] == "manualChargeLimit"), None)
    chosen = manual or min(entries, key=lambda e: e["soc"])
    return {
        "supported": True,
        "limit": chosen["soc"],
        "reason": chosen["reason"],
        "entries": entries,
    }


def describe_reason(code: int | None) -> str:
    """Best-effort translation of NotChargingReason. Never presented as fact."""
    if code is None:
        return "unknown"
    if code == 0:
        return "no reason reported - charging is allowed"
    bits = [name for bit, name in REASON_BITS.items() if code & bit]
    if bits:
        return ", ".join(bits) + " (heuristic decode of code %#08x)" % code
    return f"code {code:#08x} is not in the known table"


def charge_policy() -> dict:
    """The native macOS charge limit plus what the SMC would let us control."""
    try:
        out = subprocess.run(BATTLIMIT_CMD, capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        out = ""
    policy = parse_battlimit(out)
    try:
        from smc import smc_charge_capability
        policy["smc"] = smc_charge_capability()
    except Exception:
        policy["smc"] = None
    return policy


def native_limit_is_enforcing(limit: int | None, soc: int | None, charging: bool) -> bool:
    """True when the battery is parked at a configured limit rather than idle.

    Pure so the guard's wording can be tested without a battery.
    """
    if limit is None or soc is None:
        return False
    return not charging and soc >= limit - 5


def plain_language(policy: dict, soc: int | None, charging: bool) -> str:
    """One line explaining why the battery is not taking charge."""
    smc = policy.get("smc") or {}
    if policy.get("limit"):
        limit = policy["limit"]
        if soc is not None and soc >= limit and not charging:
            return (f"macOS is holding the battery at its {limit}% charge limit - "
                    f"it is parked, not charging")
        return f"macOS charge limit is set to {limit}%"
    if charging:
        return "the battery is charging; the adapter has headroom"
    if smc.get("charge_disable"):
        return "charging is stopped by a charge-limit tool"
    return "no charge limit is configured"
