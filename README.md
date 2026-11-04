# Power Guard — stop the battery draining while plugged in

`power_guard.py` keeps a plugged-in MacBook Pro off its battery by capping
**aggregate CPU load**. The adapter is rated 100 W but only sustains about
**75 W** under real load, and a fully loaded M4 Pro plus the GPU and display
wants more, so macOS quietly borrows the difference from the battery. There is no `pmset` setting, no System Settings toggle and no API for
"run on the adapter only" — the only lever software has is the load itself.

The tool measures per-process CPU demand from `ps` CPU-time deltas (in
core-equivalents, the unit power actually scales in) and duty-cycles the busy
processes with `SIGSTOP`/`SIGCONT` so total CPU converges on a budget.

The guard acts at **75 W by default**. With a 15 W base draw and 7.5 W per loaded
core that ceiling is 8 cores: `(75 - 15) / 7.5`. The core budget is derived from
the watt cap, so `--charger-watts` / `--budget-watts` decide when the throttle
engages.

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

# 2. run the guard - the budget is derived from the 75 W cap and steered
#    off the actual battery trend
./power_guard.py guard --auto
```

A different ceiling is a one-flag change:

```bash
./power_guard.py guard --auto --budget-watts 60   # act at 60 W -> 6 core budget
./power_guard.py guard --budget-cores 10         # pin cores instead of watts
```

Ctrl-C (or `q` in the dashboard) releases every stopped process. That is the
whole thing — the rest of this file is the dashboard, tuning, daemon setup and
limits.

---

## The dashboard

`guard` runs a live dashboard whenever stdout is a terminal (the status line
turns red when the estimate crosses the ceiling) (use `--no-ui` for
the plain one-line-per-tick log, which is what the LaunchAgent uses). It shows:

```
 POWER GUARD  ·  14 cores  ·  15:20:46  ·  AC
 adapter 75 W   ceiling 60 W   budget 6.0 cores   mode auto   tick 1.0s   under the 60 W ceiling, 12 W headroom
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
| `--charger-watts W` | all | `75` | The watt ceiling the guard acts at |
| `--sensor` | `guard`, `power` | `auto` | `stats`, `sudo`, `soc` or auto-detect |
| `--budget-watts W` | `guard` | same as `--charger-watts` | Engage when the estimated draw hits W |
| `--budget-cores N` | `guard`, `status` | derived from the watt cap | Pin the budget in cores |
| `--idle-watts W` | all | `15` | Base draw of an otherwise idle machine |
| `--w-per-core W` | all | `7.5` | Watts per busy core (calibration constant) |
| `--auto` | `guard` | off | Steer the budget off measured battery drain |
| `--fb-window S` | `guard` | `180` | Seconds of SOC history before a correction is made |
| `--tick S` | `guard`, `wrap` | `1.0` | Sampling/stop-continue period in seconds |
| `--floor N` | `guard` | `0.2` | Processes below this demand are never stopped |
| `--exclude NAME` | `guard` | — | Never stop this process (repeatable) |
| `--battery-wh Wh` | `guard`, `calibrate` | `71` | Battery capacity, for converting SOC drift to watts |
| `--duration S` | `guard` | run forever | Exit after N seconds |
| `--debug` | `guard` | off | Print each process's demand and duty every tick |
| `--quiet` | `guard` | off | Suppress the per-tick line |
| `--cores N` | `wrap` | `8` | Cap for the wrapped command |

`--w-per-core` and `--battery-wh` only matter for the *displayed* watt estimate
and for `calibrate`'s knee calculation. `--auto` steers on real SOC drift, so it
does not depend on the constant being right.

---

## How to pick a budget

The default budget is whatever the cap implies: 75 W minus a 15 W base draw, over
7.5 W per core = 8 cores on a 14-core M4 Pro. If the GPU or display is what is
really draining the battery, drop the ceiling (`--budget-watts 60`) or let
`--auto` find it. Then:

1. Run your real heavy workload.
2. `./power_guard.py calibrate --minutes 5` while that workload runs.
3. Use the printed knee, or just let `--auto` find it — it will trim the budget
   until SOC stops falling.

With 1% SOC resolution on a 71 Wh cell, one 3-minute window resolves about 14 W,
i.e. under 2 cores. Expect `--auto` to converge over a few windows (5–10 min).

---

## Where the watt measurement comes from

macOS does not expose adapter or wall power through a public API, but the
**Stats** app (`eu.exelban.Stats`) already samples the SMC every ~50 seconds
and writes the readings into its own LevelDB:

```
~/Library/Application Support/Stats/lldb
  {"adapterPower": 62.4, "batteryPower": -11.2, "ACwatts": 94, "level": 0.76, ...}
```

`adapterPower` is the "DC in" number you see in Stats, `batteryPower` is
positive while the battery charges and negative while it feeds the machine, and
`ACwatts` is what the adapter claims to be able to deliver. It is the user's own
file, so reading it needs no privileges. `./power_guard.py power` prints the
current reading and tells you which source is live.

The guard uses the best source it can find:

| Source | What it gives | Needs |
|---|---|---|
| Stats (`--sensor stats`, default) | DC-in watts, battery watts, adapter rating, ~50 s cadence | Stats installed and running |
| `sudo powermetrics` (`--sensor sudo`) | instantaneous watts | passwordless sudo for that command |
| Battery SOC drift (`--sensor soc`) | real overdraw, but only 1% resolution | nothing |

With a live feed the guard does two things beyond the simple cap:

* **Direct trigger.** If DC-in exceeds `--charger-watts`, or the battery is
  discharging at all, the budget is cut by that many watts worth of cores.
* **Self-calibration.** Consecutive samples give Δwatts / Δcores, which is the
  true watts-per-core for this machine, so the `7.5 W per core` constant drifts
  toward reality while the guard runs.

If Stats is not installed, everything still works - the guard falls back to the
CPU estimate plus battery drift.

---

## Known limits

* **CPU only.** GPU and the Neural Engine cannot be measured without root
  (`powermetrics` is root-only), and `SIGSTOP` on a GPU-bound process does not
  stop its GPU work. If your drain is GPU/ANE, cap it with that app's own thread
  or device settings, and use the CPU budget only to leave headroom.
* **Stopping is abrupt.** Duty-cycling stops processes for part of each tick.
  Apps with hard real-time deadlines (audio capture, video calls, latency
  sensitive simulations) can hiccup. Raise `--floor`, use `--exclude`, or use
  `wrap` so only the job you chose is throttled. The floor is a courtesy, not a
  guarantee: small processes are spared only while their combined load fits
  inside the budget; beyond that everything is throttled proportionally.
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

* 12 busy loops under the default 75 W ceiling: load fell from ~14.6 cores to
  2–3 cores and stayed inside the budget. A 3-core budget with 4 loops settled
  at 1.7–2.9 cores after an initial transient.
* `wrap --cores 3` on an 8-process multiprocessing job: held at 2.99–3.40 cores,
  exited cleanly when the job finished.
* `calibrate --minutes 5` at full CPU load: 0 W drain — even a 100 W adapter covered
  it, which is why the guard's `--auto` loop, not the static constant, is the
  part you should trust.
