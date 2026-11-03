"""Live terminal dashboard for power_guard.

Stdlib curses only. The rendering helpers are pure functions that return
strings, so they are unit-testable without a terminal (see
tests/test_tui.py); only Dashboard needs a real tty.
"""
from __future__ import annotations

try:
    import curses
except ImportError:            # stdlib on macOS; guard so --no-ui still works
    curses = None

HAS_CURSES = curses is not None
SPARK = "▁▂▃▄▅▆▇█"
BLOCK, EMPTY, MARK = "█", "░", "│"
WIDTH = 46


def gauge(cores: float, budget: float, max_cores: float, width: int = WIDTH) -> str:
    """Load as a gauge scaled to max_cores, with the budget marked."""
    width = max(10, width)
    filled = int(round(min(cores, max_cores) / max_cores * width))
    mark = int(round(min(budget, max_cores) / max_cores * width))
    cells = [EMPTY] * width
    for i in range(filled):
        cells[i] = BLOCK
    if 0 <= mark < width:
        cells[mark] = MARK
    return "".join(cells)


def spark(values, width: int = 24) -> str:
    """Sparkline of the last N samples, scaled to their own range."""
    vals = list(values)[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARK[3] * len(vals)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in vals)


def fmt_rows(top) -> list[str]:
    """pid / name / cores demanded / duty allowed, as fixed-width lines."""
    out = [f"  {'pid':>7}  {'process':<22} {'demand':>7} {'duty':>6}"]
    out.append("  " + "-" * 45)
    for pid, name, demand, duty in top:
        out.append(f"  {pid:>7}  {name[:22]:<22} {demand:>7.2f} {duty * 100:>5.0f}%")
    return out


def head(guard, snap) -> list[str]:
    """The top block of the dashboard: what is configured and what is happening."""
    adapter = snap.get("cap", guard.charger_watts)
    ceiling = min(adapter, snap.get("budget_watts", guard.budget_watts()))
    over = snap["watts"] - ceiling
    if over > 0:
        gap = "OVER the %d W ceiling by %.0f W - throttling" % (ceiling, over)
    else:
        gap = "under the %d W ceiling, %.0f W headroom" % (ceiling, -over)
    state = "PAUSED" if guard.paused else ("auto" if guard.auto else "fixed")
    return [
        " POWER GUARD  ·  %d cores  ·  %s  ·  %s" % (guard.max_cores, snap["clock"], snap["source"]),
        " adapter %d W   ceiling %d W   budget %.1f cores   mode %s   tick %.1fs   %s"
        % (adapter, ceiling, snap["budget"], state, guard.tick, gap),
        " load  %s" % gauge(snap["cores"], snap["budget"], guard.max_cores),
        "       %.2f cores busy  est ~%.0f W   soc %s%%  %s"
        % (snap["cores"], snap["watts"], snap["soc"], snap["charging"]),
    ]


class Dashboard:
    """curses screen redrawn once per tick, keys read without blocking."""

    def __init__(self, guard):
        self.g = guard
        self.trace: list[float] = []
        self.quit = False
        self.scr = None
        self.red = self.green = 0

    def start(self) -> None:
        self.scr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        self.scr.keypad(True)
        try:
            curses.curs_set(0)
        except Exception:
            pass
        self.scr.timeout(150)            # getch returns -1 quickly: never blocks the tick
        try:
            if curses.has_colors():
                curses.init_pair(1, curses.COLOR_RED, curses.COLOR_DEFAULT)
                curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_DEFAULT)
                self.red, self.green = curses.color_pair(1), curses.color_pair(2)
        except Exception:
            pass
        self._put(0, 0, "power_guard dashboard starting...")
        self.scr.refresh()

    def stop(self) -> None:
        if self.scr is None:
            return
        try:
            curses.echo()
            curses.curs_set(1)
            self.scr.keypad(False)
        except Exception:
            pass
        try:
            curses.close()
        except Exception:
            pass

    def _put(self, row: int, col: int, text: str, attr: int = 0) -> None:
        try:
            self.scr.addstr(row, col, text, attr)
        except UnicodeEncodeError:
            self._put(row, col, text.replace(BLOCK, "#").replace(EMPTY, ".").replace(MARK, "|")
                          .replace(SPARK[0], ",").replace(SPARK[-1], "#"))
        except Exception:
            pass                        # screen too small / resized mid-render

    def render(self, snap) -> None:
        self.trace.append(snap["cores"])
        if len(self.trace) > 40:
            self.trace.pop(0)
        self.scr.erase()
        row = 0
        for line in head(self.g, snap):
            self._put(row, 0, line, self.red if "OVER the" in line else self.green)
            row += 1
        self._put(row, 0, " trend  %s   last %d ticks" % (spark(self.trace), len(self.trace)))
        row += 1
        self._put(row, 0, " throttle: %d processes held back   %s" % (snap["throttled"], snap["note"]))
        row += 2
        for line in fmt_rows(snap["top"]):
            self._put(row, 0, line)
            row += 1
        self._put(row + 1, 0, " keys:  + / -  budget    a  auto    p  pause    q  quit")
        self.scr.refresh()

    def poll_keys(self) -> None:
        ch = self.scr.getch()
        if ch in (ord("+"), ord("=")):
            self.g.budget_cores = min(self.g.max_cores, self.g.budget_cores + 1.0)
        elif ch == ord("-"):
            self.g.budget_cores = max(2.0, self.g.budget_cores - 1.0)
        elif ch == ord("a"):
            self.g.auto = not self.g.auto
        elif ch == ord("p"):
            self.g.paused = not self.g.paused        # pause = stop throttling, budget kept
        elif ch in (ord("q"), ord("Q")):
            self.quit = True
