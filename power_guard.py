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

Stdlib only: ps, sysctl, pmset, os.kill. No root, no pip install, no
powermetrics (which needs root anyway).

Usage:
    python3 power_guard.py status
    python3 power_guard.py guard --budget-cores 8
    python3 power_guard.py guard --auto --charger-watts 100
    python3 power_guard.py wrap --cores 6 python3 main.py model.json
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

from tui import Dashboard, HAS_CURSES

CONFIG_PATH = os.path.expanduser("~/.config/power_guard.json")

# Never stop these: they freeze the display or the system.
PROTECTED = {
    "WindowServer", "loginwindow", "powerd", "coreservicesd", "launchd",
    "airportd", "bluetoothd", "runningboardd", "mDNSResponder", "syspolicyd",
    "trustd", "coreauthd", "kernel_task", "kernel", "Spotlight", "Dock",
    "ControlCenter", "usernoted", "sharingd", "power_guard.py",
}

# Measured on this machine (2026-11-03, `calibrate`-style run):
# ~12 cores of CPU load drained 1% of a 71 Wh battery in 8 min = ~8.5 W over the
# 100 W adapter, i.e. ~108 W total. That puts ~7.5 W per loaded core and ~15 W of
# base load. Only used to translate watts into cores; --auto corrects it from
# actual battery drift.
W_PER_CORE = 7.5
IDLE_W = 15.0
CHARGER_W = 75.0        # measured continuous delivery of the adapter


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
    """{source, soc, charging} from pmset. No root needed."""
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    src = "AC" if "AC Power" in out else "Battery"
    soc = charging = None
    for line in out.splitlines():
        if "InternalBattery" in line:
            m = re.search(r"(\d+)%", line)
            soc = int(m.group(1)) if m else None
            charging = "charging" in line and "not charging" not in line
    return {"source": src, "soc": soc, "charging": charging}


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


def snapshot(scope: set | None = None) -> dict[int, tuple[float, str]]:
    """pid -> (cumulative CPU seconds, name) for the current user."""
    out = subprocess.run(["ps", "-u", os.environ.get("USER", str(os.getuid())),
                         "-o", "pid=,cputime=,comm="],
                         capture_output=True, text=True).stdout
    procs: dict[int, tuple[float, str]] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(?P<pid>\d+)\s+(?P<cpu>\d+(?::\d+)+(?:\.\d+)?)\s+(?P<comm>.*)$", line)
        if not m:
            continue
        pid = int(m.group("pid"))
        comm = m.group("comm")
        name = os.path.basename(comm.split(".app/Contents/MacOS/")[-1])
        procs[pid] = (_cpu_seconds(m.group("cpu")), name)
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

    def sample(self) -> dict[int, float]:
        now = time.monotonic()
        demands: dict[int, float] = {}
        for pid, (cpu, name) in snapshot(self.scope).items():
            if pid == os.getpid() or pid == 1 or name in self.exclude:
                continue
            if pid in self.prev:
                dt = now - self.prev[pid][0]
                demands[pid] = max(0.0, (cpu - self.prev[pid][1]) / dt) if dt > 0 else 0.0
            else:
                demands[pid] = 0.0
            self.prev[pid] = (now, cpu)
            self.last[pid] = name
        return demands

    def names(self, pids) -> list[str]:
        snap = snapshot(self.scope)
        return [snap[p][1] for p in pids if p in snap]

    def release_all(self) -> None:
        for pid in list(self.prev):
            try:
                os.kill(pid, signal.SIGCONT)
            except Exception:
                pass


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
    for pid, d in demands.items():
        p = prev.get(pid, 1.0)
        if total <= budget_cores:
            duty[pid] = min(1.0, p + 0.05)              # relax gradually
        elif d < floor_cores:
            duty[pid] = 1.0                             # idle processes are not worth stopping
        else:
            duty[pid] = max(0.0, min(1.0, p * budget_cores / total))
    return duty


# --------------------------------------------------------------- the guard
class Guard:
    def __init__(self, budget_cores: float, tick: float = 1.0,
                 charger_watts: float = CHARGER_W, w_per_core: float = W_PER_CORE,
                 idle_watts: float = IDLE_W, auto: bool = False,
                 floor_cores: float = 0.2, battery_wh: float = 71.0,
                 fb_window: float = 180.0, scope: set | None = None,
                 root_pid: int | None = None, exclude: set | None = None,
                 live_power_feed: bool = False):
        self.budget_cores = budget_cores
        self.max_cores = n_cores()
        self.tick = tick
        self.charger_watts = charger_watts
        self.w_per_core = w_per_core
        self.idle_watts = idle_watts
        self.auto = auto
        self.floor = floor_cores
        self.battery_wh = battery_wh
        self.fb_window = fb_window
        self.scope = scope
        self.root_pid = root_pid
        self.exclude = exclude
        self.duty: dict[int, float] = {}
        self.stopped: set[int] = set()
        self.paused = False
        self.history: list[tuple[float, int, float]] = []
        self.power_feed = live_power_feed
        self.last_watts: float | None = None

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
        return self.est_watts(cores) - self.charger_watts

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            json.dump({"budget_cores": round(self.budget_cores, 2),
                       "charger_watts": self.charger_watts,
                       "learned_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      open(CONFIG_PATH, "w"))
        except Exception:
            pass

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

    def _apply(self, throttle: dict[int, float]) -> None:
        for pid in self.stopped - set(throttle):
            try:
                os.kill(pid, signal.SIGCONT)
            except Exception:
                pass
            self.stopped.discard(pid)
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
            except Exception:
                pass
        time.sleep(max(0.0, self.tick - elapsed))

    def run(self, duration: float | None = None, quiet: bool = False,
            stop_when=None, ui: bool = False) -> None:
        sampler = LoadSampler(scope=self.scope, exclude=self.exclude)
        sampler.sample()
        end = None if duration is None else time.time() + duration
        board = Dashboard(self) if (ui and HAS_CURSES and sys.stdout.isatty()) else None
        if board:
            board.start()
        else:
            print(f"power_guard: charger {self.charger_watts} W, budget {self.budget_cores:.1f} "
                  f"cores of {self.max_cores}, tick {self.tick}s  (Ctrl-C to release)", flush=True)
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
                note = self.steer(t0, st["soc"], cores)
                if self.power_feed:
                    watts = live_power()
                    if watts:
                        self.last_watts = watts.get("battery power")
                        over = self.over_cap(-self.last_watts) if self.last_watts is not None else 0.0
                        # the adapter is genuinely short: shave the budget now
                        if over > 0:
                            self.budget_cores = max(2.0, self.budget_cores - over / self.w_per_core)
                            note = f"measured {self.last_watts:.1f} W from battery -> budget {self.budget_cores:.1f} cores"
                        else:
                            note = "measured battery power %.1f W, no overdraw" % (self.last_watts or 0.0)
                if self.paused:
                    self.duty = {}
                else:
                    # the cap can be lowered mid-run with --charger-watts, so
                    # re-derive it every tick and never exceed it
                    cap_cores = min(self.budget_cores, self.budget_from_watts(self.charger_watts))
                    self.duty = compute_duty(demands, cap_cores, self.duty, self.floor)
                throttle = {p: f for p, f in self.duty.items() if f < 0.999}

                snap = {
                    "clock": time.strftime("%H:%M:%S"),
                    "soc": st["soc"], "source": st["source"], "charging": st["charging"],
                    "cores": cores, "watts": self.est_watts(cores),
                    "budget": self.budget_cores, "throttled": len(throttle), "note": note,
                    "cap": self.charger_watts, "over": self.over_cap(cores),
                    "measured_watts": self.last_watts,
                    "budget_watts": self.budget_watts(),
                    "top": [(pid, sampler.last.get(pid, "?"), d, self.duty.get(pid, 1.0))
                            for pid, d in sorted(demands.items(), key=lambda kv: -kv[1])[:6]],
                }
                if board:
                    board.render(snap)
                elif not quiet:
                    print(f"{snap['clock']} soc={st['soc']}% cpu={cores:5.2f} "
                          f"cores est~{self.est_watts(cores):4.0f} W "
                          f"budget={self.budget_cores:.1f} throttled={len(throttle)}  {note}",
                          flush=True)
                if self.auto:
                    self._save()
                self._apply(throttle)
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
            log("power_guard: released all stopped processes")


# -------------------------------------------------------------- sub-commands
def cmd_status(a: argparse.Namespace) -> None:
    st = power_state()
    sampler = LoadSampler()
    sampler.sample()
    time.sleep(2.0)
    demands = sampler.sample()
    cores = sum(demands.values())
    top = sorted(demands.items(), key=lambda kv: -kv[1])[:8]
    est = a.idle_watts + a.w_per_core * cores
    over = est - a.charger_watts
    print(f"power source : {st['source']}, soc {st['soc']}%, charging={st['charging']}")
    print(f"cores        : {n_cores()} (base {a.idle_watts:.0f} W, {a.w_per_core} W per loaded core)")
    print(f"CPU in use   : {cores:.2f} cores -> est system draw ~{est:.0f} W")
    if st["source"] == "AC":
        if over > 0:
            target = max(2.0, (a.charger_watts - a.idle_watts) / a.w_per_core)
            print(f"charger cap  : {a.charger_watts} W, OVER by {over:.0f} W - the battery "
                  f"is feeding the gap. Cap CPU to {target:.1f} cores.")
        else:
            print(f"charger cap  : {a.charger_watts} W, {abs(over):.0f} W headroom")
    else:
        print("running on battery - guard would not engage")
    print("top CPU consumers:")
    for pid, c in top:
        if c > 0.05:
            print(f"  pid {pid:>6}  {c:6.2f} cores  {sampler.names([pid])[0]}")


def _saved_budget() -> float | None:
    try:
        return json.load(open(CONFIG_PATH)).get("budget_cores")
    except Exception:
        return None


def cmd_guard(a: argparse.Namespace) -> None:
    cap = a.budget_watts if a.budget_watts is not None else a.charger_watts
    learned = _saved_budget() if a.auto else None
    if a.budget_cores is not None:
        budget, why = a.budget_cores, "pinned"
    elif learned is not None:
        budget, why = learned, "learned"
    else:
        budget, why = watts_to_cores(cap, a.idle_watts, a.w_per_core), "from cap"
    max_cores = n_cores()
    cap_cores = watts_to_cores(cap, a.idle_watts, a.w_per_core)
    print(f"power_guard: act at {cap:.0f} W -> budget {min(budget, max_cores):.1f} cores "
          f"({why})", flush=True)
    if budget > cap_cores:
        print(f"note: {budget:.1f} cores is ~{a.idle_watts + a.w_per_core * budget:.0f} W, above "
              f"the {cap:.0f} W cap. The throttle will hold at {cap_cores:.1f} cores; raise "
              f"--budget-watts or --charger-watts to allow more.", flush=True)
    Guard(budget_cores=budget, tick=a.tick, charger_watts=a.charger_watts,
          w_per_core=a.w_per_core, auto=a.auto, floor_cores=a.floor,
          battery_wh=a.battery_wh, fb_window=a.fb_window,
          exclude=set(a.exclude), live_power_feed=a.power).run(
        duration=a.duration, quiet=a.quiet,
        ui=(not a.no_ui and (a.ui or sys.stdout.isatty())))


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
    print(f"power source : {st['source']}, soc {st['soc']}%, charging={st['charging']}")
    print(f"estimated CPU draw (idle {a.idle_watts:.0f} W + {a.w_per_core} W/core) "
          f"against a {a.charger_watts:.0f} W ceiling")
    watts = live_power()
    if watts:
        print("powermetrics  : available -", ", ".join(f"{k} {v:.1f} W" for k, v in watts.items()))
        print("the guard can close the loop on real watts with --power")
    else:
        print("powermetrics  : not readable without a password, so the guard uses "
              "the CPU estimate plus the battery drift (which is a real measurement "
              "of overdraw, just slower)")


def cmd_calibrate(a: argparse.Namespace) -> None:
    """Measure the load level at which the battery stops draining."""
    n = n_cores()
    g = Guard(budget_cores=n, charger_watts=a.charger_watts, w_per_core=a.w_per_core,
              idle_watts=a.idle_watts)
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
              f"(system draw ~{a.charger_watts + drain:.0f} W)", flush=True)
        return drain

    print(f"calibrating against a {a.charger_watts} W charger, {g.battery_wh} Wh battery "
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
    common.add_argument("--charger-watts", type=float, default=CHARGER_W,
                    help="continuous W the adapter actually delivers (default 75)")
    common.add_argument("--w-per-core", type=float, default=W_PER_CORE)
    common.add_argument("--idle-watts", type=float, default=IDLE_W,
                        help="base draw of an idle machine (default 15 W)")

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
    sg.add_argument("--battery-wh", type=float, default=71.0)
    sg.add_argument("--fb-window", type=float, default=180.0,
                    help="seconds of SOC history --auto uses before correcting the budget")
    sg.add_argument("--duration", type=float, default=None)
    sg.add_argument("--exclude", action="append", default=[],
                    help="process name to never stop (repeatable)")
    sg.add_argument("--quiet", action="store_true", help="no per-tick output")
    sg.add_argument("--ui", action="store_true", help="live dashboard (default in a terminal)")
    sg.add_argument("--no-ui", action="store_true", help="plain one-line-per-tick log")
    sg.add_argument("--power", action="store_true",
                    help="close the loop on real watts from powermetrics (needs sudo)")
    sg.set_defaults(fn=cmd_guard)

    sw = sub.add_parser("wrap", parents=[common], help="run one command capped to N cores")
    sw.add_argument("--cores", type=float, default=8.0)
    sw.add_argument("--tick", type=float, default=1.0)
    sw.add_argument("command", nargs="+")
    sw.set_defaults(fn=cmd_wrap)

    spw = sub.add_parser("power", parents=[common], help="show what power telemetry is available")
    spw.set_defaults(fn=cmd_power)

    sc = sub.add_parser("calibrate", parents=[common], help="measure the load knee empirically")
    sc.add_argument("--minutes", type=float, default=3.0)
    sc.set_defaults(fn=cmd_calibrate)

    args = p.parse_args()
    if args.cmd is None:                      # double-click / bare run: guard --auto
        args = p.parse_args(["guard", "--auto"])
    args.fn(args)


if __name__ == "__main__":
    main()
