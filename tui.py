"""Live terminal dashboard for power_guard.

Stdlib curses only. The rendering helpers are pure functions that return
(strings, colour-key) tuples, so they are unit-testable without a terminal
(see tests/test_tui.py); only Dashboard needs a real tty.

Each information type sits in its own box, and the battery drain is the
headline: a full-width box with a sparkline of the last 24 ticks, drawn in
red (green while charging, yellow for a small drain):

    ┌─ POWER GUARD  15:20:46  AC ────────────────┐
    │ mode auto · holding back 3 process(es)     │
    └────────────────────────────────────────────┘
    ┌─ BATTERY DRAIN ────────────────────────────┐
    │ ▂▄▆█▅▃▂▄  12.4 W draining                  │
    │ soc 77% · health 100% · cell 31.7 C        │
    │ usable 74.2 Wh                             │
    └────────────────────────────────────────────┘
    ┌─ POWER ───────────────┐ ┌─ LOAD ────────────┐
    │ adapter 94 W rated    │ │ CPU 1.29 of 14    │
    │ DC in   71.2 W        │ │ ██░░│░░░  est 30W │
    │ ceiling 60 W (6.0c)   │ └───────────────────┘
    │ OVER by 11 W          │
    └───────────────────────┘
    ┌─ TOP PROCESSES ────────────────────────────┐
    │ node            9.7 W ██████████  full     │
    │ WebKit.WebCo    4.6 W ▓▓▓▒░░░░░   60% duty │
    └────────────────────────────────────────────┘

Colour is per line: header blue, drain red/yellow/green, POWER and LOAD cyan,
TOP PROCESSES magenta. Dashboard falls back to ASCII (+ - |) when the terminal
can't encode the box-drawing characters.
"""
from __future__ import annotations

try:
    import curses
except ImportError:            # stdlib on macOS; guard so --no-ui still works
    curses = None

HAS_CURSES = curses is not None
SPARK = "▁▂▃▄▅▆▇█"
BLOCK, EMPTY, MARK = "█", "░", "│"
SHADE = "░▒▓█"                      # per-process watt bar (4 levels)
BOX_W = 42                          # width of a full-width box (incl. borders)
WIDE_W = 52                         # TOP PROCESSES box: room for watts + bar + duty
HALF = 21                           # width of each side-by-side box (incl. borders)

# Colour pairs used by Dashboard: 1 red, 2 green, 3 yellow, 4 blue,
# 5 cyan, 6 magenta. Kept as constants so render() and tests agree.
C_RED, C_GREEN, C_YELLOW, C_BLUE, C_CYAN, C_MAGENTA = 1, 2, 3, 4, 5, 6


def gauge(cores: float, budget: float, max_cores: float, width: int = 36) -> str:
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


def watt_bar(watts: float, max_watts: float, width: int = 10) -> str:
    """A process's share of the draw as a 4-shade bar scaled to max_watts."""
    if watts <= 0 or max_watts <= 0:
        return EMPTY * width
    filled = min(width, max(1, int(round(watts / max_watts * width))))
    return SHADE[-1] * filled + SHADE[1] * (width - filled)


def fmt_rows(top, watts_per_core: float = 20.0) -> list[str]:
    """pid / name / cores demanded / est watts / duty, as fixed-width lines."""
    out = [f"  {'pid':>7}  {'process':<22} {'demand':>7} {'est W':>7} {'duty':>6}"]
    out.append("  " + "-" * 52)
    for pid, name, demand, duty in top:
        out.append(f"  {pid:>7}  {name[:22]:<22} {demand:>7.2f} "
                   f"{demand * watts_per_core:>7.1f} {duty * 100:>5.0f}%")
    return out


def _val(value, fmt: str, unit: str = "") -> str:
    """Format a number that may be None as 'n/a' rather than raising.

    A missing unit is left out entirely so n/a never reads as 'n/a%'.
    """
    if value is None:
        return "n/a"
    return f"{fmt % value}{unit}"


def _box(title: str, body: list[str], w: int = BOX_W) -> list[str]:
    """Wrap body lines in a ┌─ title ─┐ / └───┘ box, total width w."""
    inner = w - 2                     # cells between the two │ columns
    top = "┌─ " + title + " " + "─" * max(0, inner - len(title) - 3) + "┐"
    text_w = inner - 2                # cells left for the line itself
    mid = ["│ " + line[:text_w].ljust(text_w) + " │" for line in body]
    bottom = "└" + "─" * (w - 2) + "┘"
    return [top] + mid + [bottom]


def _pair(left: list[str], right: list[str]) -> list[str]:
    """Glue two equal-width box line-lists side by side."""
    n = max(len(left), len(right))
    out = []
    for i in range(n):
        l = left[i] if i < len(left) else " " * HALF
        r = right[i] if i < len(right) else " " * HALF
        out.append(l + " " + r)
    return out


def head(guard, snap) -> list[tuple[str, str]]:
    """The boxed dashboard: a flat list of (line, colour-key) tuples.

    Colour keys: 'head', 'drain', 'ok', 'warn', 'info', 'proc', 'plain'.
    Dashboard maps them to curses pairs; tests assert on the keys directly.
    """
    adapter = snap.get("cap") or guard.ceiling_watts()
    budget_watts = snap.get("budget_watts") or guard.budget_watts()
    ceiling = min(adapter, budget_watts)
    feed = snap.get("feed") or {}
    dc_in = feed.get("dc_in")
    battery = feed.get("battery")

    state = "PAUSED" if guard.paused else ("auto" if guard.auto else "fixed")
    lines: list[tuple[str, str]] = []

    # ── header box -----------------------------------------------------------
    head_body = [f"mode {state} · holding back {snap['throttled']} process(es)"]
    title = f"POWER GUARD  {snap['clock']}  {snap['source']}"
    for ln in _box(title, head_body):
        lines.append((ln, "head"))

    # ── battery drain box (the headline) -----------------------------------
    if battery is not None:
        direction = "charging" if battery >= 0 else "draining"
        big = f"{abs(battery):.1f} W {direction}"
        key = "ok" if battery >= 0 else ("drain" if abs(battery) > 5 else "warn")
    else:
        big = "battery feed not available"
        key = "warn"
    charge_note = snap.get("charge_note") or ""
    drain_body = [
        f"{spark(snap.get('drain_trace') or [], width=24)}  {big}",
        f"soc {_val(snap['soc'], '%d', '%')} · health {_val(snap.get('health'), '%.0f', '%')}",
        f"cell {_val(snap.get('temperature'), '%.1f', ' C')} · "
        f"usable {_val(snap.get('battery_wh'), '%.1f', ' Wh')}",
    ]
    if charge_note:
        drain_body.append(charge_note)
    for ln in _box("BATTERY DRAIN", drain_body):
        lines.append((ln, key))

    # ── POWER + LOAD boxes side by side ------------------------------------
    if dc_in is not None:
        gap = (f"OVER by {dc_in - ceiling:.0f} W" if dc_in > ceiling
               else f"{ceiling - dc_in:.0f} W headroom")
    else:
        over = snap["watts"] - ceiling
        gap = (f"OVER by {over:.0f} W" if over > 0 else f"{-over:.0f} W headroom")
    power_body = [
        f"adapter {_val(feed.get('adapter_rating'), '%.0f', ' W')} rated",
        f"DC in   {dc_in:.1f} W" if dc_in is not None else "DC in   n/a",
        f"ceiling {ceiling:.0f} W ({snap['budget']:.1f}c)",
        gap,
    ]
    load_body = [
        f"CPU {_val(snap['cores'], '%.2f')} of {guard.max_cores}",
        gauge(snap["cores"], snap["budget"], guard.max_cores, width=HALF - 4),
        f"est draw {_val(snap['watts'], '%.0f')} W",
    ]
    for ln in _pair(_box("POWER", power_body, HALF), _box("LOAD", load_body, HALF)):
        lines.append((ln, "info"))

    # ── TOP PROCESSES box: who is actually using the watts ------------------
    wpc = guard.w_per_core
    rows = []
    top = snap.get("top", [])
    total_cores = sum(d for _, _, d, _ in top)
    battery_share = max(0.0, -(battery or 0))       # W the battery is supplying
    measured = snap.get("measured") or {}
    for pid, name, demand, duty in top:
        # Prefer the kernel's measured watts for this process; fall back to the
        # cores x W/core estimate when the rusage counters are unavailable.
        watts = measured.get(pid)
        if watts is None:
            watts = demand * wpc
        bar = watt_bar(demand, max((d for _, _, d, _ in top), default=0.0))
        duty_txt = "full" if duty >= 0.999 else f"{duty*100:.0f}%"
        bat_txt = ""
        if battery_share > 0 and total_cores > 0:
            bat_txt = f" {demand / total_cores * battery_share:>4.1f}W bat"
        flag = "!" if pid in measured else ""
        rows.append(f"{name[:14]:<14} {watts:>5.1f} W{flag} {bar} {duty_txt}{bat_txt}")
    for ln in _box("TOP PROCESSES", rows if rows else ["no processes above the floor"], WIDE_W):
        lines.append((ln, "proc"))

    # ── the part the CPU budget cannot reach ---------------------------------
    unaccounted = snap.get("unaccounted")
    if unaccounted is not None and unaccounted > 3:
        lines.append((f" {unaccounted:.1f} W of the load belongs to no process "
                      f"(GPU/display/memory) - SIGSTOP cannot touch it; "
                      f"Low Power Mode {'is on' if snap.get('low_power') else 'can'}",
                      "warn"))

    # ── note line -------------------------------------------------------------
    key = "drain" if "over" in snap["note"].lower() else "plain"
    lines.append((f" {snap['note']}", key))
    return lines


def head_lines(guard, snap) -> list[str]:
    """Backwards-compatible shim: just the text of head()."""
    return [ln for ln, _ in head(guard, snap)]


class Dashboard:
    """curses screen redrawn once per tick, keys read without blocking."""

    def __init__(self, guard):
        self.g = guard
        self.trace: list[float] = []
        self.drain_trace: list[float] = []
        self.quit = False
        self.scr = None
        self.colors: dict[str, int] = {}

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
                curses.init_pair(C_RED, curses.COLOR_RED, curses.COLOR_DEFAULT)
                curses.init_pair(C_GREEN, curses.COLOR_GREEN, curses.COLOR_DEFAULT)
                curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, curses.COLOR_DEFAULT)
                curses.init_pair(C_BLUE, curses.COLOR_BLUE, curses.COLOR_DEFAULT)
                curses.init_pair(C_CYAN, curses.COLOR_CYAN, curses.COLOR_DEFAULT)
                curses.init_pair(C_MAGENTA, curses.COLOR_MAGENTA, curses.COLOR_DEFAULT)
                self.colors = {
                    "head": curses.color_pair(C_BLUE),
                    "drain": curses.color_pair(C_RED) | curses.A_BOLD,
                    "ok": curses.color_pair(C_GREEN),
                    "warn": curses.color_pair(C_YELLOW),
                    "info": curses.color_pair(C_CYAN),
                    "proc": curses.color_pair(C_MAGENTA),
                    "plain": curses.COLOR_NORMAL,
                }
        except Exception:
            pass
        self._put(0, 0, "power_guard dashboard starting...")
        self.scr.refresh()

    def _put(self, row: int, col: int, text: str, attr: int = 0) -> None:
        try:
            self.scr.addstr(row, col, text, attr)
        except UnicodeEncodeError:
            # ASCII fallback for terminals that cannot encode box characters.
            table = str.maketrans({"┌": "+", "┐": "+", "└": "+", "┘": "+",
                                   "─": "-", "│": "|", "╔": "#", "╗": "#",
                                   "╚": "#", "╝": "#", "═": "=",
                                   BLOCK: "#", EMPTY: ".", MARK: "|",
                                   SHADE[0]: ",", SHADE[1]: ":", SHADE[2]: "#"})
            text = text.translate(table)
            for ch in SPARK:
                text = text.replace(ch, ",")
            self._put(row, col, text, attr)
        except Exception:
            pass                        # screen too small / resized mid-render

    def render(self, snap) -> None:
        self.trace.append(snap["cores"])
        if len(self.trace) > 40:
            self.trace.pop(0)
        feed = snap.get("feed") or {}
        if feed.get("battery") is not None:
            self.drain_trace.append(abs(feed["battery"]))
            if len(self.drain_trace) > 24:
                self.drain_trace.pop(0)
        rows_avail = self.scr.getmaxyx()[0]
        self.scr.erase()
        snap = dict(snap)
        snap["drain_trace"] = self.drain_trace
        row = 0
        for line, key in head(self.g, snap):
            attr = self.colors.get(key, self.colors.get("plain", 0))
            self._put(row, 0, line, attr)
            row += 1
        self._put(row, 0, f" TREND {spark(self.trace)}  last {len(self.trace)} ticks")
        row += 2
        if row >= rows_avail:
            self._put(rows_avail - 1, 0, " (widen the terminal to see everything)")
        else:
            self._put(row, 0, " keys:  + / -  budget    a  auto    p  pause    q  quit")
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
