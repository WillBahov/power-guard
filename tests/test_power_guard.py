"""Unit tests for the CPU duty-cycle scheduler in power_guard.py.

Pure control logic only - no process signalling, so these are safe in CI.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from power_guard import (Guard, LoadSampler, compute_duty, measure_battery_wh, parse_power,
                      power_state, watts_to_cores, NOMINAL_BATTERY_WH,
                      controllable, snapshot, PROTECTED, ESSENTIAL, ACCESSIBILITY,
                      _exec_name)  # noqa: E402


def measured(duty, demands):
    """Core-seconds actually allowed through in one tick."""
    return sum(d * duty.get(pid, 1.0) for pid, d in demands.items())


def test_under_budget_leaves_everything_running():
    demands = {1: 2.0, 2: 1.0}
    duty = compute_duty(demands, 8.0)
    assert all(d >= 1.0 for d in duty.values())
    assert measured(duty, demands) == 3.0


def test_over_budget_converges_to_budget():
    demands = {i: 1.0 for i in range(14)}
    duty = compute_duty(demands, 6.0)
    assert abs(measured(duty, demands) - 6.0) < 1e-9
    assert all(0.0 <= d < 1.0 for d in duty.values())


def test_integral_control_does_not_oscillate():
    """Simulate 30 ticks: measured load must settle at the budget."""
    demands = {i: 1.0 for i in range(14)}          # 14 cores of real demand
    prev, series = {}, []
    for _ in range(30):
        duty = compute_duty(demands, 6.0, prev)
        achieved = {i: demands[i] * duty[i] for i in demands}
        series.append(sum(achieved.values()))
        prev = duty
        demands = achieved                        # next tick sees what ran
    assert series[-1] < 7.0, f"load did not settle: {series[-5:]}"
    assert max(series[-10:]) < max(series[:3]), "load oscillated upward"


def test_idle_processes_are_not_stopped():
    demands = {1: 12.0, 2: 0.1}
    duty = compute_duty(demands, 6.0, floor_cores=0.2)
    assert duty[2] == 1.0
    assert duty[1] < 1.0


def test_duty_never_exceeds_one_or_goes_negative():
    demands = {1: 20.0, 2: 0.5, 3: 0.0}
    prev = {1: 0.3, 2: 0.4}
    duty = compute_duty(demands, 0.1, prev)
    assert all(0.0 <= d <= 1.0 for d in duty.values())


def test_relaxation_is_gradual():
    """After being throttled, a process under budget should not snap to 1."""
    duty = compute_duty({1: 1.0}, 8.0, prev={1: 0.5})
    assert duty[1] == 0.55


def test_power_state_reads_the_machine():
    st = power_state()
    assert st["source"] in ("AC", "Battery")
    assert st["soc"] is None or 0 <= st["soc"] <= 100


def test_auto_steering_corrects_once_per_window():
    """A drain measured over a window is applied once, not every tick, and the
    correction is sized by the actual watts rather than collapsing to the floor."""
    g = Guard(budget_cores=14.0, auto=True, fb_window=180.0, battery_wh=71.0)
    notes = []
    for t in range(601):                     # one tick per second, 10 minutes
        soc = 77 if t < 200 else 76         # battery loses 1% at t = 200 s
        notes.append(g.steer(float(t), soc, 12.0))
    corrections = [n for n in notes if "over the adapter" in n]
    assert len(corrections) == 1, f"expected one correction, got {len(corrections)}"
    # 1% of 71 Wh over 180 s = 14.2 W = ~1.9 cores, not the whole 14-core budget
    assert 11.0 < g.budget_cores < 13.5, g.budget_cores

def test_watts_to_cores_conversion():
    # 75 W cap, 15 W base, 7.5 W/core -> 8 cores, which is the default budget
    assert watts_to_cores(75.0, idle_watts=15.0) == 8.0
    assert abs(watts_to_cores(100.0, idle_watts=15.0) - 11.333) < 1e-3
    assert watts_to_cores(15.0, idle_watts=15.0) == 0.0
    assert watts_to_cores(10.0, idle_watts=15.0) == 0.0   # cannot go negative


def test_guard_budget_matches_the_watt_cap():
    g = Guard(budget_cores=0.0, charger_watts=75.0, idle_watts=15.0)
    assert g.budget_from_watts(75.0) == 8.0
    assert g.budget_watts() == 15.0            # a zero core budget = base draw only
    assert abs(g.est_watts(8.0) - 75.0) < 1e-9
    assert g.over_cap(10.0) == 15.0             # 10 cores = 90 W, 15 W over
    assert g.over_cap(4.0) == -30.0           # 4 cores = 45 W, 30 W under the cap


def test_throttle_respects_the_watt_cap():
    g = Guard(budget_cores=14.0, charger_watts=75.0)          # generous core budget
    cap = min(g.budget_cores, g.budget_from_watts(g.charger_watts))
    demands = {i: 1.0 for i in range(14)}
    duty = compute_duty(demands, cap)
    assert measured(duty, demands) <= 8.0 + 1e-9


SAMPLE_POWER = """
Power info:
\tBattery Power: -21.34 W
\tSystem Power: 96.20 W
"""


def test_parse_power_reads_watts():
    p = parse_power(SAMPLE_POWER)
    assert p["battery power"] == -21.34
    assert p["system power"] == 96.20


def test_parse_power_ignores_junk():
    assert parse_power("nothing measurable here") == {}


def test_many_small_processes_do_not_break_the_budget():
    # 50 daemons at 0.1 cores each = 5 cores; a 3 core budget must still hold
    demands = {i: 0.1 for i in range(50)}
    duty = compute_duty(demands, 3.0, floor_cores=0.2)
    assert measured(duty, demands) <= 3.0 + 1e-9


def test_small_processes_are_spared_when_they_fit():
    demands = {1: 5.0, 2: 0.1, 3: 0.1}
    duty = compute_duty(demands, 4.0, floor_cores=0.2)
    assert duty[2] == 1.0 and duty[3] == 1.0          # tiny ones never stopped
    assert measured(duty, demands) <= 4.0 + 1e-9


def test_iterating_the_controller_settles_at_the_budget():
    """The loop feeds the controller what actually got through, not the
    inherent demand, so duty compounds correctly and converges."""
    inherent = {i: 1.0 for i in range(10)}
    inherent.update({100 + i: 0.1 for i in range(20)})   # 2 cores of background
    duty, seen = {}, []
    for _ in range(30):
        got_through = {pid: d * duty.get(pid, 1.0) for pid, d in inherent.items()}
        duty = compute_duty(got_through, 6.0, duty, 0.2)
        seen.append(sum(d * duty.get(pid, 1.0) for pid, d in inherent.items()))
    # bounded sawtooth around the budget, never more than half a core off
    assert max(abs(v - 6.0) for v in seen) <= 0.5


def test_measured_overdraw_lowers_the_budget():
    g = Guard(budget_cores=12.0, charger_watts=75.0, idle_watts=15.0)
    note = g.apply_sensor("no sensor", 10.0,
                          {"name": "Stats", "dc_in": 90.0, "battery": -15.0,
                           "adapter_rating": 75.0})
    assert g.budget_cores < 12.0
    assert "battery giving 15.0 W" in note


def test_headroom_caps_the_budget_at_the_ceiling():
    g = Guard(budget_cores=14.0, charger_watts=75.0, idle_watts=15.0)
    g.apply_sensor("no sensor", 4.0, {"name": "Stats", "dc_in": 40.0, "battery": 1.0})
    assert g.budget_cores <= 8.0            # (75 - 15) / 7.5 = 8 cores


def test_ceiling_follows_the_connected_adapter():
    g = Guard(budget_cores=14.0)                       # no explicit charger watts
    assert g.ceiling_watts() == 85.0                   # measured knee for this machine
    g.adapter_rating = 94.0                            # what the adapter reports
    assert g.ceiling_watts() == 94.0
    g.charger_watts = 75.0                             # an explicit cap wins
    assert g.ceiling_watts() == 75.0
    assert g.budget_from_watts(85.0) == (85.0 - 20.0) / 7.5


def test_charging_battery_does_not_force_a_cut():
    g = Guard(budget_cores=8.0, charger_watts=100.0, idle_watts=10.0)
    g.apply_sensor("none", 6.0, {"name": "Stats", "dc_in": 60.0, "battery": 1.0,
                                 "adapter_rating": 100.0})
    assert g.budget_cores == 9.0                       # headroom, so it relaxes by 1


def test_measure_battery_wh_falls_back_without_a_battery():
    """Desktops and CI have no battery node; the nominal figure must stand in."""
    import power_guard
    original = power_guard.smart_battery_reading
    power_guard.smart_battery_reading = lambda: None
    try:
        assert measure_battery_wh() == NOMINAL_BATTERY_WH
    finally:
        power_guard.smart_battery_reading = original


def test_measure_battery_wh_scales_with_health():
    """A worn pack (NominalChargeCapacity < DesignCapacity) holds fewer Wh."""
    import power_guard
    original = power_guard.smart_battery_reading
    power_guard.smart_battery_reading = lambda: {
        "design_capacity_mah": 6000, "nominal_capacity_mah": 4800,
    }
    try:
        wh = measure_battery_wh()
        assert abs(wh - NOMINAL_BATTERY_WH * 0.8) < 0.1, f"got {wh}"
    finally:
        power_guard.smart_battery_reading = original


def test_guard_uses_measured_capacity_by_default():
    g = Guard(budget_cores=8.0)
    assert g.battery_wh > 0
    # An explicit override still wins over the measurement.
    g2 = Guard(budget_cores=8.0, battery_wh=50.0)
    assert g2.battery_wh == 50.0


# ------------------------------------------------------- safety policy (OpenTamer)
def test_essential_and_accessibility_processes_are_never_stopped():
    for name in ESSENTIAL | ACCESSIBILITY:
        assert not controllable(1234, name, 501), f"{name} must be protected"


def test_root_owned_processes_are_skipped():
    # A system daemon the guard cannot signal anyway; skipping keeps the
    # dashboard honest instead of reporting a throttle that did nothing.
    assert not controllable(4321, "some-daemon", 0)
    assert controllable(4321, "some-daemon", 501)


def test_self_and_pid_1_are_not_stopped():
    import os
    assert not controllable(os.getpid(), "power_guard.py", os.getuid())
    assert not controllable(1, "anything", 501)


def test_normal_user_process_is_controllable():
    assert controllable(9999, "python3", 501)


def test_exec_name_handles_app_paths_and_args():
    assert _exec_name("/Applications/Foo.app/Contents/MacOS/Foo --flag") == "Foo"
    assert _exec_name("node script.js") == "node"
    assert _exec_name("cloudphotod") == "cloudphotod"
    # The full name survives where ps's 15-char comm would truncate it.
    assert _exec_name("/usr/sbin/distnoted") == "distnoted"


def test_snapshot_returns_generation_and_uid_fields():
    snap = snapshot()
    assert snap, "snapshot returned no processes"
    for pid, (cpu, name, uid, lstart) in snap.items():
        assert isinstance(cpu, float) and cpu >= 0
        assert isinstance(uid, int)
        assert len(lstart.split()) == 5, f"lstart malformed: {lstart!r}"


def test_release_all_only_wakes_processes_it_actively_stopped():
    """The stopped set, not the sampled set, drives release.

    A PID the guard never SIGSTOP'd must not be SIGCONT'd on exit - that is the
    OpenTamer-style generation validator: only what we paused gets resumed.
    """
    sampler = LoadSampler()
    sampler.prev[555] = (0.0, 0.0)      # sampled but never stopped
    sampler.gen[555] = "Thu Nov  5 22:40:41 2026"
    sampler.stopped_pids = set()        # guard stopped nothing
    sampler.release_all()
    assert not sampler.stopped_pids


def test_generation_validator_rejects_recycled_pid(monkeypatch):
    import power_guard
    sampler = LoadSampler()
    sampler.gen[700] = "Thu Nov  5 22:40:41 2026"   # generation we stopped
    monkeypatch.setattr(power_guard, "pid_alive", lambda pid: True)
    # ps now reports a *different* start time for the same pid -> recycled.
    monkeypatch.setattr(power_guard, "snapshot",
                        lambda scope=None: {700: (1.0, "evil", 501, "Thu Nov  5 23:59:59 2026")})
    assert not sampler._same_generation(700)


# ── Low Power Mode as the second throttle ────────────────────────────────
import lowpower


class FakeLowPower:
    """Stand-in for pmset so the decision logic is testable without root."""

    def __init__(self, state=False, permitted=True):
        self.state = state
        self.permitted = permitted
        self.calls = 0

    def is_enabled(self):
        return self.state

    def set_enabled(self, on):
        self.calls += 1
        if not self.permitted:
            return False
        self.state = on
        return True


def test_low_power_off_never_touches_the_mode():
    fake = FakeLowPower()
    lowpower.is_enabled, lowpower.set_enabled = fake.is_enabled, fake.set_enabled
    g = Guard(budget_cores=2.0, low_power="off")
    assert g.consider_low_power(-20.0, cores=8.0) == ""
    assert fake.calls == 0


def test_auto_engages_only_when_the_cpu_budget_is_at_its_floor():
    fake = FakeLowPower()
    lowpower.is_enabled, lowpower.set_enabled = fake.is_enabled, fake.set_enabled
    g = Guard(budget_cores=6.0, low_power="auto")
    assert g.consider_low_power(-12.0, cores=8.0) == ""      # plenty of CPU headroom
    assert fake.state is False
    g.budget_cores = 2.0
    note = g.consider_low_power(-12.0, cores=8.0)
    assert "Low Power Mode on" in note and fake.state is True


def test_auto_releases_low_power_when_the_battery_stops_draining():
    fake = FakeLowPower(state=True)
    lowpower.is_enabled, lowpower.set_enabled = fake.is_enabled, fake.set_enabled
    g = Guard(budget_cores=2.0, low_power="auto")
    g.low_power_set_by_us = True
    g.consider_low_power(0.5, cores=2.0)
    assert fake.state is False
    assert g.low_power_set_by_us is False


def test_on_forces_the_mode_even_with_no_drain():
    fake = FakeLowPower()
    lowpower.is_enabled, lowpower.set_enabled = fake.is_enabled, fake.set_enabled
    g = Guard(budget_cores=8.0, low_power="on")
    assert "Low Power Mode on" in g.consider_low_power(None, cores=2.0)
    assert fake.state is True


def test_a_blocked_sudo_is_reported_once_not_every_tick():
    fake = FakeLowPower(permitted=False)
    lowpower.is_enabled, lowpower.set_enabled = fake.is_enabled, fake.set_enabled
    g = Guard(budget_cores=2.0, low_power="auto")
    first = g.consider_low_power(-12.0, cores=8.0)
    assert "sudo bash scripts/lowpower-no-password.sh" in first
    assert "still unavailable" in g.consider_low_power(-12.0, cores=8.0)
