"""Rendering helpers for the dashboard - pure functions, no terminal needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from power_guard import Guard                       # noqa: E402
from tui import (SPARK, fmt_rows, gauge, head, head_lines, spark,
                 watt_bar)                          # noqa: E402


def test_gauge_marks_load_and_budget():
    g = gauge(7.0, 7.0, 14, width=20)
    assert len(g) == 20
    assert g.count("█") == 10
    assert "│" in g


def test_gauge_clamps_over_max_cores():
    assert gauge(99.0, 14.0, 14, width=20).count("█") == 20


def test_gauge_zero_load_is_empty():
    g = gauge(0.0, 14.0, 14, width=20)
    assert "█" not in g


def test_spark_flat_series():
    assert spark([5, 5, 5]) == SPARK[3] * 3


def test_spark_endpoints():
    s = spark([1.0, 9.0])
    assert s[0] == SPARK[0] and s[-1] == SPARK[-1]


def test_spark_empty():
    assert spark([]) == ""


def test_watt_bar_scales_to_biggest_consumer():
    assert watt_bar(10.0, 10.0) == "█" * 10          # the biggest gets a full bar
    assert watt_bar(5.0, 10.0).count("█") == 5
    assert watt_bar(0.0, 10.0) == "░" * 10


def test_rows_sort_and_truncate_names():
    rows = fmt_rows([(1, "a" * 30, 2.0, 0.5), (2, "python3", 1.0, 1.0)])
    assert len(rows) == 4                           # header + rule + one line per process
    assert "python3" in rows[3]
    assert len(rows[0]) == len(rows[3])            # column alignment holds


BASE = {"clock": "12:00:00", "source": "AC", "cores": 4.0, "watts": 45.0,
        "budget": 8.0, "budget_watts": 75.0, "cap": 75.0, "over": -30.0,
        "soc": 78, "charging": False, "throttled": 0, "note": "steady"}


def test_head_reports_headroom_and_overdraw():
    g = Guard(budget_cores=8.0, charger_watts=75.0)
    body = "\n".join(head_lines(g, BASE))
    assert "ceiling 75 W" in body
    assert "30 W headroom" in body
    drain = dict(BASE, cores=13.0, watts=112.0, over=37.0, throttled=3,
                 note="throttling")
    assert "OVER by 37 W" in "\n".join(head_lines(g, drain))
    assert "PAUSED" not in body


def test_head_boxes_are_labelled():
    """Every value sits inside a named box, and missing values read n/a."""
    g = Guard(budget_cores=8.0, charger_watts=75.0)
    lines = head(g, dict(BASE, throttled=2, note="steady, no drain"))
    body = "\n".join(ln for ln, _ in lines)
    for label in ("POWER GUARD", "BATTERY DRAIN", "POWER", "LOAD", "TOP PROCESSES"):
        assert label in body
    # No sensor means the power values are explicit, never blank.
    assert "DC in   n/a" in body
    assert "health n/a" in body
    assert "n/a%" not in body


def test_head_boxes_align():
    """Every box line of one box has the same width - no ragged borders."""
    g = Guard(budget_cores=8.0, charger_watts=75.0)
    lines = head(g, dict(BASE, top=[(1, "node", 4.0, 1.0)]))
    box_lines = [ln for ln, _ in lines if "┌" in ln or "│" in ln or "└" in ln]
    widths = {len(ln) for ln in box_lines if "┌" in ln or "└" in ln}
    assert widths == {42, 43, 52}                   # full, paired and wide boxes


def test_battery_drain_is_highlighted():
    """A real drain gets the red 'drain' colour key; charging is green."""
    g = Guard(budget_cores=8.0)
    draining = head(g, dict(BASE, feed={"dc_in": 71.2, "battery": -12.4}))
    keys = {k for ln, k in draining if "DRAIN" in ln or "draining" in ln}
    assert keys == {"drain"}
    charging = head(g, dict(BASE, feed={"dc_in": 80.0, "battery": 15.0}))
    keys = {k for ln, k in charging if "charging" in ln}
    assert keys == {"ok"}
    tiny = head(g, dict(BASE, feed={"dc_in": 74.0, "battery": -2.0}))
    keys = {k for ln, k in tiny if "draining" in ln}
    assert keys == {"warn"}


def test_top_processes_show_watts_and_duty():
    g = Guard(budget_cores=8.0)
    lines = head(g, dict(BASE, top=[(1, "node", 4.0, 1.0), (2, "chrome", 2.0, 0.5)]))
    body = "\n".join(ln for ln, k in lines if k == "proc")
    assert "node" in body and "chrome" in body
    assert "30.0 W" in body                         # 4 cores * 7.5 W/core
    assert "50%" in body
    assert "full" in body


def test_head_shows_battery_health_and_temperature():
    g = Guard(budget_cores=8.0)
    body = "\n".join(head_lines(g, dict(BASE, health=91.4, temperature=33.2,
                                        battery_wh=65.1)))
    assert "health 91%" in body
    assert "cell 33.2 C" in body
    assert "usable 65.1 Wh" in body


def test_head_shows_paused():
    g = Guard(budget_cores=8.0)
    g.paused = True
    lines = head_lines(g, dict(BASE, cores=1.0, watts=20.0, note="paused"))
    assert "PAUSED" in "\n".join(lines)
