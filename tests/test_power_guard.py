"""Unit tests for the CPU duty-cycle scheduler in power_guard.py.

Pure control logic only - no process signalling, so these are safe in CI.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from power_guard import Guard, compute_duty, power_state  # noqa: E402


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
