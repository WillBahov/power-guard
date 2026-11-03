# Power Guard — stop the battery draining while plugged in

`power_guard.py` keeps a plugged-in MacBook Pro off its battery by capping
**aggregate CPU load**. The 100 W adapter cannot cover a fully loaded M4 Pro
plus the GPU and display, so macOS quietly borrows the difference from the
battery. There is no `pmset` setting, no System Settings toggle and no API for
"run on the adapter only" — the only lever software has is the load itself.

The tool measures per-process CPU demand from `ps` CPU-time deltas (in
core-equivalents, the unit power actually scales in) and duty-cycles the busy
processes with `SIGSTOP`/`SIGCONT` so total CPU converges on a core budget.

* **Stdlib only** — `ps`, `pmset`, `sysctl`, `os.kill`. No pip install, no root.
* **Python 3.9+**, macOS only (the battery read uses `pmset`).
* Never touches system processes (`WindowServer`, `powerd`, `launchd`, …).
* Goes idle the moment you unplug, and releases every stopped process on exit.

---

## Quick start

### Option A - click it

Double-click **`Power Guard.command`** in Finder (in
`~/Documents/Projects/power-guard/`). Terminal opens, the guard starts in
`--auto` mode with the live dashboard, and it keeps running until you press
`q` or Ctrl-C.

`power_guard.py` itself is also executable, so `./power_guard.py` does the same
thing. If Finder refuses to run the `.command` file, make sure it is
executable (`chmod +x "Power Guard.command"`) or right-click → *Open With* →
Terminal.app.

### Option B - from the shell

```bash
cd /Users/will/Documents/Projects/power-guard

# 1. see what is drawing power right now
./power_guard.py status

# 2. cap the machine at 8 cores, self-correcting off the battery
./power_guard.py guard --budget-cores 8 --auto
```

Ctrl-C (or `q` in the dashboard) releases every stopped process. That is the
whole thing — the rest of this file is the dashboard, tuning, daemon setup and
limits.

---

## The dashboard

`guard` runs a live dashboard whenever stdout is a terminal (use `--no-ui` for
the plain one-line-per-tick log, which is what the LaunchAgent uses). It shows:

```
 POWER GUARD  ·  14 cores  ·  15:20:46  ·  AC
 charger 100 W   budget 6.0 cores   mode auto   tick 1.0s   headroom +75 W
 load  ████░░░░░░░░░░░░░░░░│░░░░░░░░░░░░░░░░░░░░░░░░
       1.29 cores busy  est ~25 W   soc 77%  False
 trend  ▄▅▁▇▂▃▂   last 6 ticks
 throttle: 3 processes held back   lost -1% in 3 min (14 W over the adapter) -> budget 5.0 cores
   pid    process                  demand   duty
   22580  node                     1.29   100%
   437964 com.apple.WebKit.WebCo   0.61    60%
```

The `│` in the load bar is the budget; the sparkline is the last 40 ticks. Keys
are read without blocking the control loop:

| Key | Effect |
|---|---|
| `+` / `-` | Raise or lower the core budget by 1 (2 cores minimum) |
| `a` | Toggle `--auto` battery feedback |
| `p` | Pause / resume the throttle (budget is kept) |
| `q` | Quit — releases every stopped process |

The dashboard is stdlib `curses`, and the rendering helpers (`gauge`, `spark`,
`fmt_rows`, `head`) are pure functions covered by `tests/test_tui.py`.

---

## The four commands

### `status` — what is drawing power now

```bash
python3 power_guard.py status
```

Prints the power source, state of charge, cores currently busy, estimated draw
against the charger rating, and the top CPU consumers. Read-only: it stops
nothing. Run this first to see whether you even have a problem.

### `guard` — the throttle loop (the main command)

```bash
python3 power_guard.py guard --budget-cores 8 --auto
```

Two modes:

* **Fixed budget** — `guard --budget-cores 8`. Predictable, no battery reading,
  nothing learned. Good when you know your knee.
* **Self-calibrating** — `guard --auto`. Reads state of charge from `pmset` and
  evaluates one feedback window (default 180 s). If SOC *fell* over that window,
  the load exceeded what the adapter can deliver, so the budget is cut by
  `drain_W / W_PER_CORE` — one correction per window, not one per tick — and the
  window resets. If SOC rose, the budget is nudged up. If it stayed flat, it
  reports "steady, no drain" and leaves the budget alone.

Per-tick line looks like:

```
14:22:05 soc=78% cpu= 8.12 cores est~ 76 W budget=8.0 throttled=3  steady, no drain
```

The learned budget is saved to `~/.config/power_guard.json`, so a subsequent
`guard --auto` starts from it. An explicit `--budget-cores` always overrides the
saved value.

Run it in the background:

```bash
nohup python3 power_guard.py guard --auto --budget-cores 8 --quiet \
      > /tmp/powerguard.log &
```

Stop it with:

```bash
pkill -f power_guard.py      # SIGTERMs it; the guard SIGCONTs everything first
```

Or auto-start at login with the LaunchAgent in `scripts/powerguard.plist`:

```bash
cp scripts/powerguard.plist ~/Library/LaunchAgents/com.powerguard.plist
launchctl load   ~/Library/LaunchAgents/com.powerguard.plist
launchctl status com.powerguard      # is it running?
tail -f /tmp/powerguard.log                 # its output
launchctl unload ~/Library/LaunchAgents/com.powerguard.plist   # stop + remove
```

Edit the plist's `ProgramArguments` to change the core budget. The plist has an
absolute path to the tool baked in — fix it if the repo moves.

### `wrap` — cap one job, leave everything else alone

```bash
python3 power_guard.py wrap --cores 6 \
    python3 -m emflow.cli --adapter sample --input examples/sample.json --out out
```

Only that command and its descendants are throttled. The guard exits when the
child does. Useful for a heavy EnergyPlus run that you know is hammering all 14
cores while you keep working in other apps.

### `calibrate` — measure the knee on *your* machine

```bash
python3 power_guard.py calibrate --minutes 5
```

Sample the battery at idle, then at full load, and print the learned cap plus
the exact command to use it. Run it while the machine is otherwise quiet and
plugged in. If it reports "the charger covers full load — no cap needed", your
drain is GPU/display, not CPU (see limits below) — set a manual budget to leave
headroom instead.

---

## Flags

| Flag | Applies to | Default | Meaning |
|---|---|---|---|
| `--ui` / `--no-ui` | `guard` | dashboard on in a terminal, off otherwise | Live dashboard vs plain log |
| `--budget-cores N` | `guard`, `status` | `8`, or the saved value with `--auto` | Target total CPU in core-equivalents |
| `--auto` | `guard` | off | Steer the budget off measured battery drain |
| `--fb-window S` | `guard` | `180` | Seconds of SOC history before a correction is made |
| `--tick S` | `guard`, `wrap` | `1.0` | Sampling/stop-continue period in seconds |
| `--floor N` | `guard` | `0.2` | Processes below this demand are never stopped |
| `--exclude NAME` | `guard` | — | Never stop this process (repeatable) |
| `--charger-watts W` | all | `100` | Adapter rating used in the estimate |
| `--w-per-core W` | all | `7.5` | Watts per busy core (calibration constant) |
| `--battery-wh Wh` | `guard`, `calibrate` | `71` | Battery capacity, for converting SOC drift to watts |
| `--duration S` | `guard` | run forever | Exit after N seconds |
| `--quiet` | `guard` | off | Suppress the per-tick line |
| `--cores N` | `wrap` | `8` | Cap for the wrapped command |

`--w-per-core` and `--battery-wh` only matter for the *displayed* watt estimate
and for `calibrate`'s knee calculation. `--auto` steers on real SOC drift, so it
does not depend on the constant being right.

---

## How to pick a budget

Start with 8 on a 14-core M4 Pro. That leaves ~25 W of headroom for the GPU,
display and background daemons. Then:

1. Run your real heavy workload.
2. `python3 power_guard.py calibrate --minutes 5` while that workload runs.
3. Use the printed knee, or just let `--auto` find it — it will trim the budget
   until SOC stops falling.

With 1% SOC resolution on a 71 Wh cell, one 3-minute window resolves about 14 W,
i.e. under 2 cores. Expect `--auto` to converge over a few windows (5–10 min).

---

## Known limits

* **CPU only.** GPU and the Neural Engine cannot be measured without root
  (`powermetrics` is root-only), and `SIGSTOP` on a GPU-bound process does not
  stop its GPU work. If your drain is GPU/ANE, cap it with that app's own thread
  or device settings, and use the CPU budget only to leave headroom.
* **Stopping is abrupt.** Duty-cycling stops processes for part of each tick.
  Apps with hard real-time deadlines (audio capture, video calls, latency
  sensitive simulations) can hiccup. Raise `--floor`, use `--exclude`, or use
  `wrap` so only the job you chose is throttled.
* **System processes are never stopped.** `WindowServer`, `loginwindow`,
  `powerd`, `coreservicesd`, `launchd`, `airportd`, `kernel_task` and friends
  are in `PROTECTED`.
* **Battery charge limits are fine.** macOS often parks the battery at 80%
  ("not charging"). Overdraw still shows up as a slow SOC decline, which is
  exactly what `--auto` steers against.
* **On battery the guard does nothing** — there is nothing to protect, and
  throttling would shorten your runtime for no reason. It releases all stopped
  processes and idles until you plug back in.
* **PID churn.** Processes that repeatedly fork new workers (some Python
  multiprocessing pools) can slip past the budget, since a freshly restarted
  worker starts with full duty. Prefer `wrap` for those.

---

## Files

| Path | What it is |
|---|---|
| `power_guard.py` | The tool — stdlib only, executable |
| `tui.py` | The curses dashboard (render helpers are pure and tested) |
| `Power Guard.command` | Double-click to start the guard in Terminal |
| `power-guard` | Same thing, for a name on `PATH` |
| `scripts/powerguard.plist` | LaunchAgent template for auto-start at login |
| `tests/test_power_guard.py` | Pytest suite (scheduler convergence, SOC→watts math, protected list) |
| `docs/POWER_GUARD.md` | Design notes: measurements, control theory, verification |
| `~/.config/power_guard.json` | Learned budget, written by `--auto` |

---

## Tests

```bash
python3 -m pytest tests/ -q        # 17 tests: scheduler, watts math, dashboard rendering
```

No machine state is required — the suite runs the scheduler against a simulated
load, so it verifies the controller converges rather than ringing.

---

## Verified on this machine

* 14 busy loops under `guard --budget-cores 6`: load fell from 13.5 cores to
  5.3–6.9 cores and held there for 20 ticks.
* `wrap --cores 3` on an 8-process multiprocessing job: held at 2.99–3.40 cores,
  exited cleanly when the job finished.
* `calibrate --minutes 5` at full CPU load: 0 W drain — the 100 W adapter covered
  it, which is why the guard's `--auto` loop, not the static constant, is the
  part you should trust.
