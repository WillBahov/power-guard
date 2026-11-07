"""Power telemetry that is actually reachable from an ordinary process.

Sources, tried in order:

  1. `sudo -n powermetrics` - instantaneous watts, but only if sudo is
     passwordless.
  2. The AppleSMC user client (smc.py) - live DC-in, system load and battery
     watts with no root and no third-party app. This is the sensor the guard
     wants; the others are fallbacks.
  3. The Stats app (eu.exelban.Stats). It samples the adapter and the battery
     every ~50 s into its own LevelDB under
     ~/Library/Application Support/Stats/lldb. Its records carry `adapterPower`
     (DC in, watts), `batteryPower` (positive = charging, negative = draining)
     and `ACwatts` (the adapter's rating). It is the user's own file, so no
     privileges are needed.

If none of them answer, the caller falls back to battery state-of-charge drift,
which is still a real measurement of overdraw - just much slower.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time

from smartbattery import smart_battery_reading
from smc import smc_reading

STATS_DB = os.path.expanduser("~/Library/Application Support/Stats/lldb")
STALE_AFTER = 300.0          # Stats samples roughly every 50 s
RECORD = re.compile(rb"\{[^{}]*adapterPower[^{}]*\}")
POWER_CMD = ["sudo", "-n", "powermetrics", "--samplers", "cpu_power",
             "-i", "1000", "-n", "1"]
# The tasks sampler gives per-process watts including GPU, which is the only way
# to see a GPU-bound app without guessing. Same shape Battery Hog uses.
TASKS_CMD = ["sudo", "-n", "powermetrics", "--samplers", "tasks,cpu_power",
             "-n", "1", "-i", "500"]


def parse_power(text: str) -> dict:
    """Watts out of powermetrics output.

    powermetrics prints values in mW or W, e.g.
        CPU Power: 12345 mW
        GPU Power: 2100 mW
        Total Power: 14445 mW
        Battery Power: -1200 mW
    """
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        if "power" not in label.lower():
            continue
        value = value.strip()
        unit = "mW" if "mW" in value else "W"
        number = value.replace(unit, "").replace("W", "").strip()
        try:
            watts = float(number)
        except ValueError:
            continue
        if unit == "mW":
            watts /= 1000.0
        out[label.strip().lower()] = watts
    return out


def parse_tasks(text: str) -> list[dict]:
    """Per-process watts from `powermetrics --samplers tasks`.

    The table Battery Hog parses looks like:

        Energy Impact (last 10s)     ID   CPU    GPU   ANE   ...
        kernel_task                  1    0.0    0.0   0.0   ...

    so each row is <name...> <pid> <numbers...>. Lenient on purpose: a format
    change should yield [] rather than a crash.
    """
    tasks = []
    in_table = False
    for line in text.splitlines():
        if "Energy Impact" in line and "ID" in line:
            in_table = True
            continue
        if not in_table:
            continue
        stripped = line.strip()
        if not stripped:
            break
        tokens = stripped.split()
        pid_index = next((i for i, t in enumerate(tokens) if t.isdigit()), None)
        if pid_index is None or pid_index == 0:
            continue
        name = " ".join(tokens[:pid_index])
        if name.upper().startswith("ALL_TASKS"):
            continue
        numbers = [float(t) for t in tokens[pid_index + 1:] if _is_number(t)]
        tasks.append({"name": name, "pid": int(tokens[pid_index]),
                      "cpu": numbers[0] if numbers else 0.0,
                      "gpu": numbers[1] if len(numbers) > 1 else 0.0})
    return tasks


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def live_power() -> dict | None:
    """Watts from powermetrics, if sudo is passwordless. Otherwise None."""
    try:
        r = subprocess.run(POWER_CMD, capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    watts = parse_power(r.stdout)
    if not watts:
        return None
    return {
        "name": "powermetrics",
        "dc_in": watts.get("total power"),
        "battery": watts.get("battery power"),
        "cpu": watts.get("cpu power"),
        "gpu": watts.get("gpu power"),
        "adapter_rating": None,
        "soc": None,
        "charging": None,
    }


def process_watts_root() -> list[dict] | None:
    """Per-process watts from powermetrics' tasks sampler, if root is free."""
    try:
        r = subprocess.run(TASKS_CMD, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    tasks = parse_tasks(r.stdout)
    return tasks or None


class StatsSensor:
    """The latest adapter/battery reading written by the Stats app."""

    def __init__(self, path: str = STATS_DB):
        self.path = path
        self.key = None
        self.cache = None

    def reading(self) -> dict | None:
        try:
            files = sorted(glob.glob(os.path.join(self.path, "*")), key=os.path.getmtime)
        except OSError:
            return None
        for f in reversed(files):
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            if (f, mtime) == self.key and self.cache is not None:
                return self.cache
            if time.time() - mtime > STALE_AFTER:
                continue
            try:
                size = os.path.getsize(f)
                with open(f, "rb") as fh:
                    fh.seek(max(0, size - 262144))
                    data = fh.read()
            except OSError:
                continue
            records = RECORD.findall(data)
            if not records:
                continue
            try:
                rec = json.loads(records[-1].decode("utf-8", "replace"))
            except Exception:
                continue
            self.key, self.cache = (f, mtime), rec
            return rec
        return None


def sensor_feed(stats: StatsSensor | None, use_sudo: bool, use_smc: bool = True) -> dict | None:
    """The best watt reading available: powermetrics if root is free, then the
    SMC user client (live, unprivileged), then the Stats cache, then nothing.

    The IOKit AppleSmartBattery node is merged in as a supplement whenever it is
    reachable: it adds `adapter_rating` (the adapter's real watts) and the charge
    state (`is_charging`, `not_charging_reason`) without needing root or Stats.
    It cannot give DC-in watts, so it never overrides a real watt feed - it only
    fills gaps and labels *why* the battery is not charging.
    """
    feed = None
    if use_sudo:
        feed = live_power()
    if feed is None and use_smc:
        reading = smc_reading()
        if reading:
            feed = {
                "name": "SMC",
                "dc_in": reading["dc_in"],
                "dc_in_raw": reading["dc_in_raw"],
                "system_load": reading["system_load"],
                "battery": reading["battery"],
                "adapter_rating": None,
                "soc": None,
                "charging": None,
                "cell_temps": reading["cell_temps"],
                "observed_ceiling": reading["observed_adapter_ceiling"],
            }
    if feed is None and stats is not None:
        rec = stats.reading()
        if rec:
            feed = {
                "name": "Stats",
                "dc_in": rec.get("adapterPower"),
                "battery": rec.get("batteryPower"),
                "adapter_rating": rec.get("ACwatts"),
                "soc": round(rec.get("level", 0) * 100),
                "charging": rec.get("isCharging", False),
            }

    sb = smart_battery_reading()
    if sb:
        if feed is None:
            feed = {
                "name": "AppleSmartBattery",
                "dc_in": None,
                "battery": sb["battery_watts"],
                "adapter_rating": sb["adapter_watts"],
                "soc": sb["soc_percent"],
                "charging": sb["is_charging"],
            }
        else:
            if feed.get("adapter_rating") is None and sb["adapter_watts"]:
                feed["adapter_rating"] = sb["adapter_watts"]
            # The IOKit charge state is fresher than the Stats cache.
            feed["charging"] = sb["is_charging"]
        feed["not_charging_reason"] = sb.get("not_charging_reason")
        feed["external_connected"] = sb.get("external_connected")

    return feed
