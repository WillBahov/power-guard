"""Rendering helpers for the dashboard - pure functions, no terminal needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from power_guard import Guard                       # noqa: E402
from tui import SPARK, fmt_rows, gauge, head, spark  # noqa: E402


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


def test_rows_sort_and_truncate_names():
    rows = fmt_rows([(1, "a" * 30, 2.0, 0.5), (2, "python3", 1.0, 1.0)])
    assert len(rows) == 4                      # header + rule + one line per process
    assert "python3" in rows[3]
    assert len(rows[0]) == len(rows[3])        # column alignment holds


def test_head_reports_headroom_and_overdraw():
    g = Guard(budget_cores=8.0, charger_watts=75.0)
    ok = head(g, {"clock": "12:00:00", "source": "AC", "cores": 4.0,
                  "watts": 45.0, "budget": 8.0, "budget_watts": 75.0, "cap": 75.0,
                  "over": -30.0, "tick": 1.0, "soc": 78, "charging": False})
    drain = head(g, {"clock": "12:00:00", "source": "AC", "cores": 13.0,
                     "watts": 112.0, "budget": 8.0, "budget_watts": 75.0, "cap": 75.0,
                     "over": 37.0, "tick": 1.0, "soc": 78, "charging": False})
    assert "ceiling 75 W" in ok[1]
    assert "30 W headroom" in ok[1]
    assert "OVER the 75 W ceiling by 37 W - throttling" in drain[1]
    assert "PAUSED" not in ok[1]


def test_head_shows_paused():
    g = Guard(budget_cores=8.0)
    g.paused = True
    lines = head(g, {"clock": "12:00:00", "source": "AC", "cores": 1.0,
                     "watts": 20.0, "budget": 8.0, "tick": 1.0,
                     "soc": 78, "charging": False})
    assert "PAUSED" in lines[1]
