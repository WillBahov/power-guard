#!/usr/bin/env python3
"""Power guard - keep a plugged-in MacBook off the battery under heavy load.

Problem: a 100 W charger cannot feed an M4 Pro under heavy load, so the
battery quietly supplies the difference and drains while plugged in. macOS has
no setting that says "do not draw from battery", so the only real lever is the
load itself: cap aggregate CPU to what the charger can deliver.

How it works:

  1. A duty-cycle scheduler SIGSTOPs the busy processes for part of each tick
     so aggregate CPU converges on a core budget. It is an integral controller
     (duty *= budget/measured each tick), so it settles instead of oscillating.
     An idle core draws well under a watt, so the machine's draw tracks the
     duty cycle.
  2. Optional --auto feedback from battery state of charge. If SOC is falling
     while AC is attached, the budget is beyond what the charger can supply and
     it is pulled down until the drain stops.

Sensing, best to worst: `sudo -n powermetrics` (if passwordless), the AppleSMC
user client (live watts, no root - see smc.py), the Stats app's cache, then
state-of-charge drift. Per-process watts come from the kernel's rusage counters
(energy.py), so the dashboard reports measured watts rather than cores x 7.5.

Stdlib only: ps, sysctl, pmset, os.kill, ctypes. No pip install, no root -
except for the optional Low Power Mode toggle and the powermetrics sampler,
which use sudo when it is free.

Usage:
    python3 power_guard.py status
    python3 power_guard.py guard --budget-cores 8
    python3 power_guard.py guard --auto --low-power auto
    python3 power_guard.py wrap --cores 6 python3 main.py model.json
    python3 power_guard.py gate -- cargo build
    python3 power_guard.py charge
    python3 power_guard.py calibrate
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

from sensor import StatsSensor, live_power, parse_power, sensor_feed
from smartbattery import smart_battery_reading
from tui import Dashboard, HAS_CURSES
from chargepolicy import charge_policy, plain_language
from energy import EnergySampler
from workloads import Gate, cap_workers, classify, detect
import lowpower

CONFIG_PATH = os.path.expanduser("~/.config/power_guard.json")
# The SMC reports a DC-in plateau only when the machine is actually working; an
# idle 30 W reading is not the adapter's ceiling. This is how much CPU load we
# require before a plateau is treated as evidence.
CEILING_EVIDENCE_CORES = 6.0

# Never stop these. Names alone are not enough: a process can be root-owned or
# an accessibility daemon that SIGSTOP would silently break, so the guard also
# checks uid and role (see `controllable`). The list is modelled on OpenTamer's
# safety policy (internal/core/safety.go), which blocks essential system
# processes, accessibility agents, its own privileged helper and itself.
ESSENTIAL = {
    "kernel_task", "launchd", "loginwindow", "WindowServer", "hidd",
    "securityd", "tccd",
}

# Accessibility/assistive agents: stopping them breaks VoiceOver, switch
# control and other input paths, and they are not the load we are trying to cap.
ACCESSIBILITY = {
    "VoiceOver", "AXVisualSupportAgent",
}

PROTECTED = ESSENTIAL | ACCESSIBILITY | {
    "powerd", "coreservicesd", "airportd", "bluetoothd", "runningboardd",
    "mDNSResponder", "syspolicyd", "trustd", "coreauthd", "kernel",
    "Spotlight", "Dock", "ControlCenter", "usernoted", "sharingd",
    "power_guard.py",
}


def controllable(pid: int, name: str, uid: int) -> bool:
    """Can this process be SIGSTOP'd without breaking the machine?

    Essential and accessibility processes are never touched. Root-owned ones are
    skipped outright: they are system daemons, and if the guard is not itself
    root a SIGSTOP to them would fail with EPERM anyway - stopping it here keeps
    the dashboard honest instead of reporting a throttle that did nothing.
    """
    if pid == 1 or pid == os.getpid():
        return False
    if name in PROTECTED:
        return False
    if uid == 0:
        return False
    return True

# Measured on this machine (2026-11-03, `calibrate`-style run):
# ~12 cores of CPU load drained 1% of a 71 Wh battery in 8 min = ~8.5 W over the
# 100 W adapter, i.e. ~108 W total. That puts ~7.5 W per loaded core and ~15 W of
# base load. Only used to translate watts into cores; --auto corrects it from
# actual battery drift.
W_PER_CORE = 7.5
IDLE_W = 20.0

# Fallback only. The real capacity is measured from the battery node at startup
# (see measure_battery_wh); this stands in on desktops or when the keys are
# absent. M4 Pro design capacity is 6249 mAh at 11.5 V nominal = ~71 Wh.
NOMINAL_BATTERY_WH = 71.0
CHARGER_W = 85.0        # measured knee: above this the battery starts contributing
MAX_CHARGER_W = 100.0   # the most the machine will accept
BATTERY_MARGIN = 0.0    # any negative battery power means the battery is feeding


def log(msg: str) -> None:
    """Print, tolerating a closed pipe (e.g. `guard ... | head`)."""
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        sys.exit(0)


def watts_to_cores(watts: float, idle_watts: float = IDLE_W,
                   w_per_core: float = W_PER_CORE) -> float:
    """Core budget whose estimated draw is exactly `watts`.
    75 W cap, 15 W base, 7.5 W per core -> 8 cores."""
    return max(0.0, (watts - idle_watts) / w_per_core)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------- power state
POWER_CMD = ["sudo", "-n", "powermetrics", "--samplers", "power", "-i", "1000", "-n", "1"]


def parse_power(text: str) -> dict:
    """Instantaneous watts from powermetrics output, e.g. 'Battery Power: -12.4 W'.

    Returns {name: watts}; a negative battery power means the machine is drawing
    more than the adapter can supply. Pure function, so it is testable.
    """
    out = {}
    for line in text.splitlines():
        m = re.search(r"([A-Za-z][A-Za-z ]*Power)\s*:\s*(-?\d+(?:\.\d+)?)\s*W", line)
        if m:
            out[m.group(1).strip().lower()] = float(m.group(2))
    return out


def live_power() -> dict | None:
    """Real wattage if powermetrics is reachable (needs sudo), else None."""
    try:
        r = subprocess.run(POWER_CMD, capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    watts = parse_power(r.stdout)
    if not watts:
        return None
    if "battery power" in watts:
        watts["overdraw_w"] = max(0.0, -watts["battery power"])
    return watts


def power_state() -> dict:
    """{source, soc, charging} - AppleSmartBattery (IOKit) first, pmset as a fallback.

    The IOKit node gives the same charge state as pmset but sub-second and without
    spawning a subprocess, plus it is the source WhatBattery uses, so it is the
    most trustworthy unprivileged signal available.
    """
    sb = smart_battery_reading()
    if sb:
        return {
            "source": "AC" if sb["external_connected"] else "Battery",
            "soc": sb["soc_percent"],
            "charging": sb["is_charging"],
            # Extra telemetry from the same node read, for the dashboard and for
            # Guard's own capacity estimate.
            "health_percent": sb.get("health_percent"),
            "temperature_c": sb.get("temperature_c"),
            "battery_watts": sb.get("battery_watts"),
            "discharging_while_plugged": sb.get("discharging_while_plugged"),
        }
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    src = "AC" if "AC Power" in out else "Battery"
    soc = charging = None
    for line in out.splitlines():
        if "InternalBattery" in line:
            m = re.search(r"(\d+)%", line)
            soc = int(m.group(1)) if m else None
            charging = "charging" in line and "not charging" not in line
    return {"source": src, "soc": soc, "charging": charging}


def measure_battery_wh() -> float:
    """Watt-hours the battery can actually hold, from the IOKit node.

    DesignCapacity is the factory pack capacity in mAh; NominalChargeCapacity is
    what the cell can still accept. Their ratio is the health figure macOS shows
    (never `MaxCapacity`, which is pinned to 100 on Apple Silicon and hides a
    worn cell), so usable Wh = nominal pack Wh x that ratio. The pack's Wh rating
    is derived from its design voltage, ~11.3 V for this 6249 mAh pack. Falls
    back to NOMINAL_BATTERY_WH when the keys are absent (desktop Macs, CI).

    This matters because --auto converts a SOC percentage drop into watts: on a
    battery that has lost 20% of its capacity, assuming 71 Wh overestimates the
    drain and the throttle under-corrects.
    """
    sb = smart_battery_reading()
    if not sb:
        return NOMINAL_BATTERY_WH
    design = sb.get("design_capacity_mah")
    nominal = sb.get("nominal_capacity_mah")
    if not design or not nominal:
        return NOMINAL_BATTERY_WH
    # DesignCapacity is the factory capacity in mAh; NominalChargeCapacity is what
    # the cell can still accept. Their ratio is the health figure macOS shows (not
    # `MaxCapacity`, which is pinned to 100 on Apple Silicon and hides a worn
    # cell). Usable Wh = DesignCapacity x design voltage x that ratio, so a new
    # battery returns NOMINAL_BATTERY_WH unchanged and a worn one scales down.
    design_volts = NOMINAL_BATTERY_WH * 1000.0 / design     # ~11.3 V for a 6249 mAh pack
    wh = design * design_volts * (nominal / design) / 1000.0
    return round(wh, 1) if 20.0 <= wh <= 200.0 else NOMINAL_BATTERY_WH


def n_cores() -> int:
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.ncpu"],
                                  capture_output=True, text=True).stdout.strip())
    except Exception:
        return 8


def _cpu_seconds(text: str) -> float:
    """ps reports cumulative CPU time as h:mm:ss or m:ss.ff."""
    parts = text.strip().split(":")
    parts = [p for p in parts if p != ""]
    if not parts:
        return 0.0
    if len(parts) == 3:
        h, m, s = parts
    else:
        h, m, s = 0, parts[0], parts[1]
    return int(h) * 3600 + int(m) * 60 + float(s)


def _exec_name(args: str) -> str:
    """Executable basename from a full argv string.

    Handles "/Applications/Foo.app/Contents/MacOS/Foo --flag", "node script.js"
    and bare "cloudphotod". Uses the argv[0] path, which is what ps's `comm` was
    trying to give before it truncated it to 15 chars.
    """
    first = args.strip().split(None, 1)[0] if args.strip() else ""
    if not first:
        return ""
    leaf = first.split(".app/Contents/MacOS/")[-1]
    return os.path.basename(leaf.split()[0]) if leaf else ""


def snapshot(scope: set | None = None) -> dict[int, tuple[float, str, int, str]]:
    """pid -> (cumulative CPU seconds, name, uid, start time).

    `lstart` is the process launch time and acts as a generation stamp: PIDs are
    recycled, so a pid stopped at tick 3 may belong to a different program by the
    time we release it. Carrying the start time lets the guard verify identity
    before sending SIGCONT (OpenTamer calls this a process-generation validator).

    `comm` is truncated to 15 characters by ps, which silently breaks name-based
    protection ("distnoted" reads as "distno"), so the full argv is used instead.
    pid, cpu and uid are single tokens; lstart is five tokens (weekday month day
    time year); everything after that is the command line.
    """
    out = subprocess.run(["ps", "-u", os.environ.get("USER", str(os.getuid())),
                         "-o", "pid=,cputime=,uid=,lstart=,args="],
                         capture_output=True, text=True).stdout
    procs: dict[int, tuple[float, str, int, str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # ps prints lstart as "Thu  5 Nov 22:40:41 2026": weekday, day-of-month,
        # month, time, year. The day is a number, not a month name.
        m = re.match(r"^(\d+)\s+(\d+(?::\d+)+(?:\.\d+)?)\s+(\d+)\s+"
                     r"([A-Za-z]{3}\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$",
                     line)
        if not m:
            continue
        pid = int(m.group(1))
        procs[pid] = (_cpu_seconds(m.group(2)), _exec_name(m.group(5)),
                      int(m.group(3)), m.group(4))
    if scope is not None:
        procs = {p: v for p, v in procs.items() if p in scope}
    return procs


def descendants(root_pid: int) -> set[int]:
    """All descendants of root_pid, from the parent-child table in ps."""
    out = subprocess.run(["ps", "-u", os.environ.get("USER", str(os.getuid())),
                         "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)", line)
        if m:
            children.setdefault(int(m.group(2)), []).append(int(m.group(1)))
    seen, stack = {root_pid}, [root_pid]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


# ----------------------------------------------------------- load measurement
class LoadSampler:
    """Per-process CPU demand in core-equivalent units (1.0 = one core busy).

    SIGSTOP'd processes accrue no CPU time, so the measurement reflects exactly
    what the throttle lets through - which is what the controller needs.
    """

    def __init__(self, scope: set | None = None, exclude: set | None = None):
        self.scope = scope
        self.exclude = set(PROTECTED) | (exclude or set())
        self.prev: dict[int, tuple[float, float]] = {}
        self.last: dict[int, str] = {}   # pid -> name from the most recent sample
        # pid -> lstart of the generation we last saw. A recycled PID must not be
        # treated as the process we stopped, so release_all checks against this.
        self.gen: dict[int, str] = {}
        # PIDs the guard actually SIGSTOP'd. Kept on the sampler so the guard and
        # the release path agree on exactly what was paused.
        self.stopped_pids: set[int] = set()

    def sample(self) -> dict[int, float]:
        now = time.monotonic()
        demands: dict[int, float] = {}
        for pid, (cpu, name, uid, lstart) in snapshot(self.scope).items():
            if not controllable(pid, name, uid) or name in self.exclude:
                continue
            # A recycled PID restarts its CPU clock; carrying the old cumulative
            # time across generations would read a huge negative delta and mask
            # whatever the new process is actually doing.
            if pid in self.prev and self.gen.get(pid) == lstart:
                dt = now - self.prev[pid][0]
                demands[pid] = max(0.0, (cpu - self.prev[pid][1]) / dt) if dt > 0 else 0.0
            else:
                demands[pid] = 0.0
            self.prev[pid] = (now, cpu)
            self.last[pid] = name
            self.gen[pid] = lstart
        return demands

    def names(self, pids) -> list[str]:
        snap = snapshot(self.scope)
        return [snap[p][1] for p in pids if p in snap]

    def release_all(self) -> None:
        """SIGCONT every process we stopped, but only its original generation.

        Blindly SIGCONT-ing a tracked PID can wake an unrelated process that
        inherited the number after the original exited. `stopped_pids` is the
        authoritative set of what the guard actually paused.
        """
        for pid in self.stopped_pids:
            if self._same_generation(pid):
                try:
                    os.kill(pid, signal.SIGCONT)
                except Exception:
                    pass
        self.stopped_pids.clear()

    def _same_generation(self, pid: int) -> bool:
        """Is the live PID still the process we stopped?"""
        if not pid_alive(pid):
            return False
        snap = snapshot({pid})
        return pid in snap and snap[pid][3] == self.gen.get(pid)


def compute_duty(demands: dict[int, float], budget_cores: float,
                 prev: dict[int, float] | None = None,
                 floor_cores: float = 0.2) -> dict[int, float]:
    """Fraction of the tick each pid may run, driving measured load to budget.

    Integral control on measured load: over budget -> every throttled process's
    duty is multiplied by budget/measured; under budget -> duty creeps back up.
    Pure function, so it is testable without touching processes.
    """
    prev = prev or {}
    total = sum(demands.values())
    duty: dict[int, float] = {}

    if total <= budget_cores:
        for pid in demands:
            duty[pid] = min(1.0, prev.get(pid, 1.0) + 0.05)   # relax gradually
        return duty

    # Processes under the floor are not worth stopping - but only if they fit
    # inside the budget. A hundred daemons at 0.1 cores each are 10 cores, and
    # exempting them would make the budget unreachable.
    small = {pid: d for pid, d in demands.items() if d < floor_cores}
    spare = sum(small.values())
    if spare <= budget_cores:
        left = budget_cores - spare
        heavy_total = total - spare
        for pid, d in demands.items():
            if pid in small:
                duty[pid] = 1.0
            else:
                duty[pid] = max(0.0, min(1.0, prev.get(pid, 1.0) * left / heavy_total))
    else:
        for pid, d in demands.items():
            duty[pid] = max(0.0, min(1.0, prev.get(pid, 1.0) * budget_cores / total))
    return duty


# --------------------------------------------------------------- the guard
class Guard:
    def __init__(self, budget_cores: float, tick: float = 1.0,
                 charger_watts: float | None = None, w_per_core: float = W_PER_CORE,
                 idle_watts: float = IDLE_W, auto: bool = False,
                 floor_cores: float = 0.2, battery_wh: float | None = None,
                 fb_window: float = 180.0, scope: set | None = None,
                 root_pid: int | None = None, exclude: set | None = None,
                 sensor: str = "auto", low_power: str = "off"):
        self.budget_cores = budget_cores
        self.max_cores = n_cores()
        self.adapter_rating: float | None = None   # what the plugged-in charger reports
        self.tick = tick
        self.charger_watts = charger_watts
        self.w_per_core = w_per_core
        self.idle_watts = idle_watts
        self.auto = auto
        self.floor = floor_cores
        # The Wh the battery can actually hold, not the nominal figure. --auto
        # converts a SOC percentage drop into watts, so a worn cell makes that
        # conversion wrong; measure it from DesignCapacity/NominalChargeCapacity
        # when the node exposes them, otherwise fall back to NOMINAL_BATTERY_WH.
        self.battery_wh = battery_wh if battery_wh is not None else measure_battery_wh()
        sb = smart_battery_reading()
        self.battery_health: float | None = sb.get("health_percent") if sb else None
        self.fb_window = fb_window
        self.scope = scope
        self.root_pid = root_pid
        self.exclude = exclude
        self.duty: dict[int, float] = {}
        self.stopped: set[int] = set()
        self.paused = False
        self.history: list[tuple[float, int, float]] = []
        self.sensor = sensor
        self.stats = StatsSensor() if sensor in ("auto", "stats") else None
        self.use_sudo = sensor in ("auto", "sudo")
        self.use_smc = sensor in ("auto", "smc")
        self.last_feed: dict | None = None
        self.prev_feed: tuple | None = None      # (dc_in W, cores) last sample
        # Live watts per process, measured by the kernel rather than inferred
        # from core counts. None until the first sample lands.
        self.energy = EnergySampler(scope=scope)
        self.measured_watts: dict[int, float] = {}
        # The DC-in plateau the SMC reports under load - the adapter's real
        # ceiling, which is lower than its nameplate (82.9 W observed here
        # against a 100 W brick).
        self.observed_ceiling: float | None = None
        # Low Power Mode is the only throttle that reaches the GPU, the display
        # and the Neural Engine. "off" never touches it, "on" forces it, and
        # "auto" turns it on when the battery is still draining after the CPU
        # budget has hit its floor.
        self.low_power = low_power
        self.low_power_set_by_us = False
        self.low_power_blocked = False   # sudo not available: say so once, not every tick
        self.policy = charge_policy()

    def ceiling_watts(self) -> float:
        """The ceiling to act on.

        An explicit --charger-watts wins. Otherwise the best evidence of what the
        adapter can actually deliver: the DC-in plateau the SMC has seen under
        load (this 100 W brick stops rising at ~83 W), then the rating the
        battery node reports, then CHARGER_W. A plateau is only counted when the
        machine was doing real work - an idle 30 W reading is not a ceiling.
        """
        if self.charger_watts:
            return self.charger_watts
        if self.observed_ceiling:
            if self.adapter_rating:
                return min(self.adapter_rating, self.observed_ceiling)
            return self.observed_ceiling
        return self.adapter_rating or CHARGER_W

    def est_watts(self, cores: float) -> float:
        """Ballpark system draw; the --auto loop uses real battery drift."""
        return self.idle_watts + self.w_per_core * cores

    def budget_from_watts(self, watts: float) -> float:
        """Core budget whose estimated draw is exactly `watts`."""
        return watts_to_cores(watts, self.idle_watts, self.w_per_core)

    def budget_watts(self) -> float:
        """The watt ceiling the current core budget corresponds to."""
        return self.est_watts(self.budget_cores)

    def over_cap(self, cores: float) -> float:
        """How far the estimated draw exceeds the cap (positive = draining)."""
        return self.est_watts(cores) - self.ceiling_watts()

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            json.dump({"budget_cores": round(self.budget_cores, 2),
                       "ceiling_watts": self.ceiling_watts(),
                       "learned_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      open(CONFIG_PATH, "w"))
        except Exception:
            pass

    def apply_sensor(self, fallback: str, cores: float, feed: dict | None = None) -> str:
        """Steer the budget on measured watts when a sensor is reachable.

        `dc_in` is the power the adapter is actually delivering; when the
        machine wants more than that, the battery supplies the gap and
        `battery` goes negative. Either one is a direct trigger to throttle -
        far faster than waiting for state of charge to move a whole percent.
        """
        if feed is None:
            feed = sensor_feed(self.stats, self.use_sudo, self.use_smc)
        if not feed:
            return fallback
        self.last_feed = feed
        dc = feed.get("dc_in")
        batt = feed.get("battery")

        # Consecutive real samples let the model calibrate itself: the change
        # in watts divided by the change in cores is the true watts-per-core.
        if rating := feed.get("adapter_rating"):
            self.adapter_rating = float(rating)
        # A plateau is only evidence of the ceiling while the machine is working.
        plateau = feed.get("observed_ceiling")
        if plateau and cores >= CEILING_EVIDENCE_CORES:
            self.observed_ceiling = max(self.observed_ceiling or 0.0, plateau)
        if dc is not None and self.prev_feed is not None:
            p_dc, p_cores = self.prev_feed
            dcores = cores - p_cores
            ddc = dc - p_dc
            if abs(dcores) >= 1.0 and abs(ddc) >= 5.0:
                w = ddc / dcores
                if 1.0 <= w <= 12.0:
                    self.w_per_core = 0.7 * self.w_per_core + 0.3 * w
                self.idle_watts = max(5.0, dc - self.w_per_core * cores)
        self.prev_feed = (dc, cores)

        ceiling = self.ceiling_watts()
        ceiling_cores = max(2.0, self.budget_from_watts(ceiling))

        # The battery is the reference: it can only give power if the adapter
        # cannot cover the demand, so a negative battery power is a direct
        # measurement of overdraw - independent of which charger is plugged in.
        if batt is not None and batt < BATTERY_MARGIN:
            self.budget_cores = max(2.0, min(self.budget_cores, ceiling_cores)
                                    - (-batt) / self.w_per_core)
            dc_txt = f"{dc:.0f} W" if dc is not None else "adapter n/a"
            return (f"{feed['name']}: battery giving {-batt:.1f} W, {dc_txt} of "
                    f"{ceiling:.0f} W -> budget {self.budget_cores:.1f} cores")
        if dc is not None and dc >= 0.92 * ceiling:
            self.budget_cores = min(self.budget_cores, ceiling_cores)
            return (f"{feed['name']}: adapter pegged at {dc:.0f} W of {ceiling:.0f} W, "
                    f"budget held at {self.budget_cores:.1f} cores")
        self.budget_cores = min(ceiling_cores, self.budget_cores + 1.0)
        dc_txt = f"{dc:.0f} W in" if dc is not None else "no DC-in reading"
        batt_txt = f"{batt:+.1f} W" if batt is not None else "battery n/a"
        return (f"{feed['name']}: {dc_txt}, battery {batt_txt}, ceiling "
                f"{ceiling:.0f} W = {ceiling_cores:.1f} cores ({self.w_per_core:.1f} W/core)")

    def consider_low_power(self, battery_watts: float | None, cores: float) -> str:
        """The second throttle: Low Power Mode.

        SIGSTOP can only pause CPU work. When the battery is still draining after
        the CPU budget has been pushed to its floor, whatever is left is GPU,
        display, memory rails and background daemons - and Low Power Mode is the
        only switch that reaches those. "auto" engages it only in that situation
        and disengages it as soon as the battery stops draining, so it never
        leaves the machine slower than it needs to be.
        """
        if self.low_power == "off":
            return ""
        state = lowpower.is_enabled()
        if state is None:
            return "low power mode state unreadable"
        at_floor = self.budget_cores <= 2.5
        draining = battery_watts is not None and battery_watts < BATTERY_MARGIN
        want = (self.low_power == "on") or (self.low_power == "auto" and draining and at_floor)
        if want and not state:
            if lowpower.set_enabled(True):
                self.low_power_set_by_us = True
                if battery_watts is not None:
                    why = f"the battery is still giving {-battery_watts:.1f} W"
                else:
                    why = "no watt sensor is reachable to prove headroom"
                return (f"Low Power Mode on - CPU budget at the "
                        f"{self.budget_cores:.1f} core floor and {why} "
                        f"(GPU/display are not reachable by SIGSTOP)")
            if not self.low_power_blocked:
                self.low_power_blocked = True
                return ("Low Power Mode wanted but not permitted: " + lowpower.sudo_hint())
            return "Low Power Mode still unavailable (no passwordless sudo)"
        if not want and state and self.low_power_set_by_us:
            if lowpower.set_enabled(False):
                self.low_power_set_by_us = False
                return "Low Power Mode released - the battery stopped draining"
        return ""

    def restore_low_power(self) -> None:
        """Fail-safe: never leave Low Power Mode on that we turned on."""
        if self.low_power_set_by_us:
            lowpower.set_enabled(False)
            self.low_power_set_by_us = False

    def steer(self, now: float, soc: int | None, cores: float) -> str:
        """AIMD, once per feedback window: battery losing charge means the
        budget is above what the adapter can deliver."""
        if soc is None:
            return "no soc"
        self.history.append((now, soc, cores))
        if not self.auto:
            return "feed-forward"
        t0, soc0 = self.history[0][0], self.history[0][1]
        dt = now - t0
        if dt < self.fb_window:
            return "learning"
        dsoc = soc - soc0
        self.history = [(now, soc, cores)]          # reset the window
        if dsoc < 0:
            drain = self.battery_wh * (-dsoc / 100.0) / (dt / 3600.0)
            cut = drain / self.w_per_core
            self.budget_cores = max(2.0, self.budget_cores - cut)
            return (f"lost {dsoc}% in {dt / 60:.0f} min ({drain:.0f} W over the "
                    f"adapter) -> budget {self.budget_cores:.1f} cores")
        if dsoc > 0:
            self.budget_cores = min(self.max_cores, self.budget_cores + 1.0)
            return f"headroom (+{dsoc}%) -> budget raised to {self.budget_cores:.1f} cores"
        return "steady, no drain"

    def _apply(self, throttle: dict[int, float], sampler: LoadSampler) -> None:
        # Release processes no longer in the throttle set - but only if the PID is
        # still the process we stopped. A recycled PID would otherwise wake an
        # unrelated program that happens to have inherited the number.
        for pid in self.stopped - set(throttle):
            if sampler._same_generation(pid):
                try:
                    os.kill(pid, signal.SIGCONT)
                except Exception:
                    pass
            self.stopped.discard(pid)
            sampler.stopped_pids.discard(pid)
        for pid in throttle:
            try:
                os.kill(pid, signal.SIGCONT)
            except Exception:
                pass
        elapsed = 0.0
        for et, pid in sorted((f * self.tick, pid) for pid, f in throttle.items()):
            time.sleep(max(0.0, et - elapsed))
            elapsed = et
            try:
                os.kill(pid, signal.SIGSTOP)
                self.stopped.add(pid)
                sampler.stopped_pids.add(pid)
            except Exception:
                pass
        time.sleep(max(0.0, self.tick - elapsed))

    def run(self, duration: float | None = None, quiet: bool = False,
            stop_when=None, ui: bool = False, debug: bool = False) -> None:
        sampler = LoadSampler(scope=self.scope, exclude=self.exclude)
        sampler.sample()
        end = None if duration is None else time.time() + duration
        board = Dashboard(self) if (ui and HAS_CURSES and sys.stdout.isatty()) else None
        if board:
            board.start()
        else:
            print(f"power_guard: ceiling {self.ceiling_watts():.0f} W, "
                  f"budget {self.budget_cores:.1f} cores of {self.max_cores}, "
                  f"tick {self.tick}s  (Ctrl-C to release)", flush=True)
        try:
            while True:
                if stop_when is not None and stop_when():
                    break
                if self.root_pid is not None:
                    try:
                        if pid_alive(self.root_pid):
                            sampler.scope = descendants(self.root_pid)
                        else:
                            break
                    except Exception:
                        break
                t0 = time.time()
                st = power_state()
                if st["source"] != "AC":
                    sampler.release_all()
                    self.stopped.clear()
                    if not quiet:
                        log("on battery - guard idle (nothing to protect)")
                    time.sleep(self.tick)
                    if end and time.time() > end:
                        break
                    continue

                demands = sampler.sample()
                cores = sum(demands.values())
                self.measured_watts = self.energy.sample()
                note = self.steer(t0, st["soc"], cores)
                note = self.apply_sensor(note, cores)
                lpm_note = self.consider_low_power((self.last_feed or {}).get("battery"), cores)
                if lpm_note:
                    note = f"{note} | {lpm_note}"
                if self.paused:
                    self.duty = {}
                else:
                    # the cap can be lowered mid-run with --charger-watts, so
                    # re-derive it every tick and never exceed it
                    cap_cores = min(self.budget_cores,
                                    self.budget_from_watts(self.ceiling_watts()))
                    self.duty = compute_duty(demands, cap_cores, self.duty, self.floor)
                throttle = {p: f for p, f in self.duty.items() if f < 0.999}

                snap = {
                    "clock": time.strftime("%H:%M:%S"),
                    "soc": st["soc"], "source": st["source"], "charging": st["charging"],
                    "cores": cores, "watts": self.est_watts(cores),
                    "budget": self.budget_cores, "throttled": len(throttle), "note": note,
                    "cap": self.ceiling_watts(), "over": self.over_cap(cores),
                    "feed": self.last_feed,
                    "budget_watts": self.budget_watts(),
                    "health": self.battery_health,
                    "battery_wh": self.battery_wh,
                    "temperature": st.get("temperature_c"),
                    "top": [(pid, sampler.last.get(pid, "?"), d, self.duty.get(pid, 1.0))
                            for pid, d in sorted(demands.items(), key=lambda kv: -kv[1])[:6]],
                    # Real measured watts per process, plus the part of the system
                    # load no process accounts for (GPU, display, memory rails).
                    "measured": self.measured_watts,
                    "measured_total": sum(self.measured_watts.values()),
                    "unaccounted": ((self.last_feed or {}).get("system_load") or 0.0)
                                   - sum(self.measured_watts.values()),
                    "charge_limit": self.policy.get("limit"),
                    "charge_note": plain_language(self.policy, st["soc"], st["charging"]),
                    "low_power": lowpower.is_enabled(),
                }
                if debug:
                    print(f"  total {cores:5.2f} | cap {self.budget_cores:.1f} | "
                          + " | ".join(f"{name[:18]}={d:.2f}c duty{duty:.2f}"
                                       for pid, name, d, duty in snap["top"][:8]), flush=True)
                if board:
                    board.render(snap)
                elif not quiet:
                    print(f"{snap['clock']} soc={st['soc']}% cpu={cores:5.2f} "
                          f"cores est~{self.est_watts(cores):4.0f} W "
                          f"budget={self.budget_cores:.1f} throttled={len(throttle)}  {note}",
                          flush=True)
                if self.auto:
                    self._save()
                self._apply(throttle, sampler)
                if board:
                    board.poll_keys()
                    if board.quit:
                        break
                if end and time.time() > end:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            if board:
                board.stop()
            sampler.release_all()
            self.restore_low_power()
            log("power_guard: released all stopped processes")


# -------------------------------------------------------------- sub-commands
def cmd_status(a: argparse.Namespace) -> None:
    st = power_state()
    sampler = LoadSampler()
    sampler.sample()
    energy = EnergySampler()
    energy.sample()
    time.sleep(2.0)
    demands = sampler.sample()
    measured = energy.sample()
    cores = sum(demands.values())
    top = sorted(demands.items(), key=lambda kv: -kv[1])[:8]
    est = a.idle_watts + a.w_per_core * cores
    feed = sensor_feed(StatsSensor(), a.sensor in ("auto", "sudo"), a.sensor in ("auto", "smc"))
    # No --charger-watts means the ceiling is whatever the adapter reports.
    cap = a.charger_watts or (feed or {}).get("adapter_rating") or CHARGER_W
    over = est - cap
    policy = charge_policy()
    print(f"power source : {st['source']}, soc {st['soc']}%, charging={st['charging']}")
    if st.get("health_percent") is not None:
        temp = st.get("temperature_c")
        temp_txt = f"{temp:.1f} C" if temp is not None else "n/a"
        print(f"battery      : {st['health_percent']:.0f}% health, cell {temp_txt}, "
              f"{measure_battery_wh():.1f} Wh usable")
    if policy.get("limit"):
        print(f"charge limit : {policy['limit']}% ({policy.get('reason')}) - "
              f"{plain_language(policy, st['soc'], st['charging'])}")
    lpm = lowpower.is_enabled()
    print(f"low power    : {'on' if lpm else 'off' if lpm is not None else 'unknown'}")
    print(f"cores        : {n_cores()} (base {a.idle_watts:.0f} W, {a.w_per_core} W per loaded core)")
    print(f"CPU in use   : {cores:.2f} cores -> est system draw ~{est:.0f} W")
    if feed:
        load = feed.get("system_load")
        measured_total = sum(measured.values())
        line = f"measured     : sensor {feed['name']}"
        if load is not None:
            line += f", system load {load:.1f} W"
        line += f", {measured_total:.1f} W attributable to processes"
        if load is not None and load - measured_total > 3:
            line += f" ({load - measured_total:.1f} W GPU/display/memory the CPU budget cannot touch)"
        print(line)
    if st["source"] == "AC":
        if over > 0:
            target = max(2.0, (cap - a.idle_watts) / a.w_per_core)
            print(f"charger cap  : {cap} W, OVER by {over:.0f} W - the battery "
                  f"is feeding the gap. Cap CPU to {target:.1f} cores.")
        else:
            print(f"charger cap  : {cap} W, {abs(over):.0f} W headroom")
    else:
        print("running on battery - guard would not engage")
    print("top CPU consumers:")
    shown = 0
    for pid, c in top:
        if c > 0.05:
            watts = measured.get(pid)
            watts_txt = f"{watts:5.1f} W" if watts is not None else "  n/a"
            print(f"  pid {pid:>6}  {c:6.2f} cores  {watts_txt}  {sampler.names([pid])[0]}")
            shown += 1
    if not shown:
        # Nothing crossed the display threshold: the machine is idle, so there is
        # no load to throttle. This is not a failure to see processes - the sampler
        # still tracks every one; they are just all under 0.05 cores right now.
        print("  (no process above 0.05 cores - the machine is idle right now)")
    workloads = detect()
    if workloads:
        print("workloads running:")
        for w in workloads[:6]:
            print(f"  {w['project']:<22} {w['tool']:<14} {len(w['pids'])} process(es)")


def _saved_budget() -> float | None:
    try:
        return json.load(open(CONFIG_PATH)).get("budget_cores")
    except Exception:
        return None


def cmd_guard(a: argparse.Namespace) -> None:
    probe = sensor_feed(StatsSensor(), a.sensor in ("auto", "sudo"), a.sensor in ("auto", "smc"))
    rating = (probe or {}).get("adapter_rating")
    cap = a.budget_watts or a.charger_watts or rating or CHARGER_W
    learned = _saved_budget() if a.auto else None
    if a.budget_cores is not None:
        budget, why = a.budget_cores, "pinned"
    elif learned is not None:
        budget, why = learned, "learned"
    else:
        budget, why = watts_to_cores(cap, a.idle_watts, a.w_per_core), "from cap"
    max_cores = n_cores()
    cap_cores = watts_to_cores(cap, a.idle_watts, a.w_per_core)
    print(f"power_guard: ceiling {cap:.0f} W -> budget "
          f"{min(budget, max_cores):.1f} cores ({why}); the battery sensor decides "
          f"when to throttle", flush=True)
    if budget > cap_cores:
        print(f"note: {budget:.1f} cores is ~{a.idle_watts + a.w_per_core * budget:.0f} W, above "
              f"the {cap:.0f} W cap. The throttle will hold at {cap_cores:.1f} cores; raise "
              f"--budget-watts or --charger-watts to allow more.", flush=True)
    Guard(budget_cores=budget, tick=a.tick, charger_watts=a.charger_watts,
          w_per_core=a.w_per_core, auto=a.auto, floor_cores=a.floor,
          battery_wh=a.battery_wh, fb_window=a.fb_window,
          exclude=set(a.exclude), sensor=a.sensor, low_power=a.low_power).run(
        duration=a.duration, quiet=a.quiet,
        ui=(not a.no_ui and (a.ui or sys.stdout.isatty())), debug=a.debug)


def cmd_wrap(a: argparse.Namespace) -> None:
    """Run one command (and its children) with its CPU capped."""
    child = subprocess.Popen(a.command)
    print(f"wrap: pid {child.pid} capped to {a.cores} cores "
          f"(charger {a.charger_watts} W) - Ctrl-C to stop", flush=True)
    g = Guard(budget_cores=a.cores, tick=a.tick, charger_watts=a.charger_watts,
              w_per_core=a.w_per_core, idle_watts=a.idle_watts, root_pid=child.pid)
    try:
        g.run(stop_when=lambda: child.poll() is not None)
    except KeyboardInterrupt:
        child.terminate()
    print(f"wrap: child exited with code {child.wait()}")


def cmd_power(a: argparse.Namespace) -> None:
    """Report which power telemetry this machine will actually give us."""
    st = power_state()
    print(f"power source  : {st['source']}, soc {st['soc']}%, charging={st['charging']}")
    if st.get("health_percent") is not None or st.get("temperature_c") is not None:
        health = st.get("health_percent")
        temp = st.get("temperature_c")
        print(f"battery       : health {health:.0f}%" if health is not None else "battery       : health n/a")
        print(f"  cell temp   : {temp:.1f} C" if temp is not None else "  cell temp   : n/a")
        print(f"  usable      : {measure_battery_wh():.1f} Wh")
    print(f"ceiling used  : {a.charger_watts or 'adapter rating, else 85'} W "
          f"(idle {a.idle_watts:.0f} W + {a.w_per_core} W per loaded core)")
    feed = sensor_feed(StatsSensor(), getattr(a, "sensor", "auto") in ("auto", "sudo"), getattr(a, "sensor", "auto") in ("auto", "smc"))
    if feed:
        print(f"live sensor   : {feed['name']}")
        if feed.get("dc_in") is not None:
            print(f"  DC in       : {feed['dc_in']:.1f} W")
        if feed.get("battery") is not None:
            b = feed["battery"]
            print(f"  battery     : {b:+.1f} W ({'charging' if b >= 0 else 'draining'})")
        if feed.get("adapter_rating"):
            print(f"  adapter rated: {feed['adapter_rating']:.0f} W")
    else:
        print("live sensor   : none reachable. Stats is not installed or not "
              "running, and sudo powermetrics needs a password. The guard then "
              "steers on battery state of charge drift - a real measurement of "
              "overdraw, just slower.")


def cmd_charge(a: argparse.Namespace) -> None:
    """Report the charge policy this Mac supports, and optionally hold the charge.

    macOS' own Charge Limit (System Settings, Apple Silicon, macOS 26.4+) already
    stops charging at a set SoC while the adapter keeps powering the machine. It is
    readable here without root. Going below 80% needs a write to an SMC charge key,
    which requires root and a key that must exist on the hardware - `--hold` checks
    both before doing anything.

    The important distinction: stopping charging does not stop the battery from
    *supplying* power. Only the load cap does that.
    """
    policy = charge_policy()
    st = power_state()
    smc = policy.get("smc") or {}
    print(f"state        : {st['source']}, soc {st['soc']}%, charging={st['charging']}")
    if policy.get("limit"):
        print(f"macOS charge limit: {policy['limit']}% "
              f"({policy.get('reason')}) - {plain_language(policy, st['soc'], st['charging'])}")
    else:
        print("macOS charge limit: none configured "
              "(System Settings > Battery > Charge Limit)")
    if smc:
        keys = ", ".join(f"{k}({v['size']}B)" for k, v in smc["keys"].items()) or "none"
        print(f"SMC charge keys : {keys}")
        if smc.get("charge_disable"):
            print("                a charge-disable key exists, so a root helper "
                  "could hold the charge below macOS' 80% floor")
        elif smc.get("adapter_cloak"):
            print("                only an adapter-cloak key exists. Writing it "
                  "disconnects the adapter and the battery DISCHARGES - the opposite "
                  "of what this tool is for. Not touched.")
        else:
            print("                no charge-control key on this machine")
    if a.hold:
        if not smc.get("charge_disable"):
            print("\ncannot hold: no charge-disable SMC key on this hardware. "
                  "Set the native limit in System Settings instead.")
            return
        key = "CHTE" if "CHTE" in smc["keys"] else "CH0C"
        r = subprocess.run(["sudo", "-n", sys.executable, "smc.py", "write", key, "1"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("holding charge needs root without a password prompt. Try:\n"
                  "    sudo python3 smc.py write CHTE 1")
            return
        print(f"holding charge via {key}=1; Ctrl-C restores charging.")
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            subprocess.run(["sudo", "-n", sys.executable, "smc.py", "write", key, "0"])
            print("charging restored")


def cmd_gate(a: argparse.Namespace) -> None:
    """Run a heavy command, queued so only one heavy job runs while the battery drains.

    `power_guard.py gate -- cargo build` is gentler than SIGSTOP-ing the compiler:
    it tells the toolchain to use fewer workers and makes concurrent heavy jobs wait
    for a slot. Nothing is killed, and a job on AC power with headroom is passed
    straight through.
    """
    argv = list(a.command)
    if not argv:
        print("gate: no command given", file=sys.stderr)
        sys.exit(2)
    info = classify(" ".join(argv))
    st = power_state()
    feed = sensor_feed(StatsSensor(), a.sensor in ("auto", "sudo"), a.sensor in ("auto", "smc"))
    draining = (feed or {}).get("battery") is not None and feed["battery"] < BATTERY_MARGIN
    under_pressure = st["source"] != "AC" or draining or a.always

    if info is None or not info["heavy"] or not under_pressure:
        print(f"gate: {info['tool'] if info else 'command'} passed through "
              f"({'on battery' if st['source'] != 'AC' else 'adapter has headroom'})",
              flush=True)
        child = subprocess.Popen(argv)
        sys.exit(child.wait())

    gate = Gate(slots=a.slots)
    while True:
        ok, position = gate.claim(" ".join(argv))
        if ok:
            break
        print(f"gate: {position} heavy job(s) ahead of you, waiting "
              f"({', '.join(gate.active())})", file=sys.stderr)
        time.sleep(a.poll)

    argv, env, note = cap_workers(argv, a.workers)
    if note:
        print(f"gate: {note}", flush=True)
    try:
        child = subprocess.Popen(argv, env={**os.environ, **env})
        while child.poll() is None:
            time.sleep(1.0)
        code = child.returncode
    finally:
        gate.release()
    print(f"gate: {info['tool']} finished (exit {code})")
    sys.exit(code)


def cmd_calibrate(a: argparse.Namespace) -> None:
    """Measure the load level at which the battery stops draining."""
    n = n_cores()
    g = Guard(budget_cores=n, charger_watts=a.charger_watts, w_per_core=a.w_per_core,
              idle_watts=a.idle_watts)
    cap = a.charger_watts or CHARGER_W      # adapter rating if not given explicitly
    src = "n = 0\nwhile True:\n    for i in range(300000):\n        n += i * i\n"

    def measure(minutes: float, label: str, load: int) -> float:
        workers = [subprocess.Popen([sys.executable, "-c", src]) for _ in range(load)]
        sampler = LoadSampler()
        sampler.sample()
        t0 = time.time()
        soc0 = power_state()["soc"]
        cores, samples = 0.0, 0
        while time.time() - t0 < minutes * 60:
            time.sleep(1.0)
            cores += sum(sampler.sample().values())
            samples += 1
        soc1 = power_state()["soc"]
        for w in workers:
            w.terminate()
        dt = (time.time() - t0) / 3600.0
        drain = g.battery_wh * (soc0 - soc1) / 100.0 / dt if dt > 0 else 0.0
        print(f"{label:>12}: {cores / max(samples, 1):5.1f} cores busy, "
              f"soc {soc0}% -> {soc1}%, drain {drain:6.1f} W "
              f"(system draw ~{cap + drain:.0f} W)", flush=True)
        return drain

    cap = a.charger_watts or CHARGER_W      # adapter rating if not given explicitly
    print(f"calibrating against a {cap} W charger, {g.battery_wh} Wh battery "
          f"({2 * a.minutes:.0f} min). Plug in and leave the machine otherwise quiet.")
    d_idle = measure(a.minutes, "idle", 0)
    d_full = measure(a.minutes, "full load", n)
    if d_full <= 0:
        print("the charger covers full load - no cap needed.")
        return
    knee = max(2.0, n - (d_full - d_idle) / g.w_per_core)
    print(f"\nlearned cap: run at most {knee:.1f} of {n} cores "
          f"(full load overdraws by {d_full:.0f} W = {d_full / g.w_per_core:.1f} cores)")
    print(f"save it:     python3 power_guard.py guard --budget-cores {knee:.1f} --auto")


def main() -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--charger-watts", type=float, default=None,
                    help="watt ceiling to act on. Omit it and the guard uses the "
                         "rating the connected adapter reports (Stats ACwatts)")
    common.add_argument("--w-per-core", type=float, default=W_PER_CORE)
    common.add_argument("--idle-watts", type=float, default=IDLE_W,
                        help="base draw of an idle machine (default 15 W)")
    common.add_argument("--sensor", choices=("auto", "smc", "stats", "sudo", "soc"), default="auto",
                        help="watt source: sudo powermetrics if passwordless, then the "
                             "AppleSMC user client (live, no root), then the Stats app "
                             "cache, then battery state of charge only")

    p = argparse.ArgumentParser(description="Keep a plugged-in MacBook off the battery "
                                "by capping aggregate CPU load.")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("status", parents=[common], help="power state + who is eating the CPU")
    sp.add_argument("--budget-cores", type=float, default=None)
    sp.set_defaults(fn=cmd_status)

    sg = sub.add_parser("guard", parents=[common], help="run the throttle loop")
    sg.add_argument("--budget-cores", type=float, default=None,
                    help="pin the budget in cores (otherwise it is derived from "
                         "--budget-watts, or the learned value when --auto)")
    sg.add_argument("--budget-watts", type=float, default=None,
                    help="act when the estimated draw reaches this many watts "
                         "(defaults to --charger-watts)")
    sg.add_argument("--auto", action="store_true",
                    help="steer the budget off measured battery drain")
    sg.add_argument("--tick", type=float, default=1.0)
    sg.add_argument("--floor", type=float, default=0.2,
                    help="processes below this core demand are never stopped")
    sg.add_argument("--battery-wh", type=float, default=None,
                    help="usable battery watt-hours. Omit it and the guard measures "
                         "the pack's real capacity from the IOKit node")
    sg.add_argument("--fb-window", type=float, default=180.0,
                    help="seconds of SOC history --auto uses before correcting the budget")
    sg.add_argument("--duration", type=float, default=None)
    sg.add_argument("--exclude", action="append", default=[],
                    help="process name to never stop (repeatable)")
    sg.add_argument("--quiet", action="store_true", help="no per-tick output")
    sg.add_argument("--ui", action="store_true", help="live dashboard (default in a terminal)")
    sg.add_argument("--no-ui", action="store_true", help="plain one-line-per-tick log")
    sg.add_argument("--debug", action="store_true",
                    help="print per-process demand and duty each tick")
    sg.add_argument("--low-power", choices=("off", "auto", "on"), default="off",
                    help="use Low Power Mode as a second throttle. 'auto' turns it "
                         "on only when the battery is still draining with the CPU "
                         "budget at its floor, and turns it back off afterwards. "
                         "Needs the scoped sudoers rule in scripts/")
    sg.set_defaults(fn=cmd_guard)

    sw = sub.add_parser("wrap", parents=[common], help="run one command capped to N cores")
    sw.add_argument("--cores", type=float, default=8.0)
    sw.add_argument("--tick", type=float, default=1.0)
    sw.add_argument("command", nargs="+")
    sw.set_defaults(fn=cmd_wrap)

    spw = sub.add_parser("power", parents=[common], help="show what power telemetry is available")
    spw.set_defaults(fn=cmd_power)

    scg = sub.add_parser("charge", parents=[common],
                         help="what the machine's charge limit is and what could be controlled")
    scg.add_argument("--hold", action="store_true",
                     help="hold the charge via the SMC charge-disable key (needs root, "
                          "and the key must exist on this Mac)")
    scg.set_defaults(fn=cmd_charge)

    sga = sub.add_parser("gate", parents=[common],
                         help="run a heavy command, queued and worker-capped")
    sga.add_argument("--slots", type=int, default=1, help="heavy jobs allowed at once")
    sga.add_argument("--workers", type=int, default=4,
                     help="worker/job cap applied to the toolchain")
    sga.add_argument("--poll", type=float, default=5.0, help="seconds between queue checks")
    sga.add_argument("--always", action="store_true",
                     help="queue even when the adapter has headroom")
    sga.add_argument("command", nargs="+")
    sga.set_defaults(fn=cmd_gate)

    sc = sub.add_parser("calibrate", parents=[common], help="measure the load knee empirically")
    sc.add_argument("--minutes", type=float, default=3.0)
    sc.set_defaults(fn=cmd_calibrate)

    args = p.parse_args()
    if args.cmd is None:                      # double-click / bare run: guard --auto
        args = p.parse_args(["guard", "--auto"])
    args.fn(args)


if __name__ == "__main__":
    main()
