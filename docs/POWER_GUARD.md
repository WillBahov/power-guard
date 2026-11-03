# Power guard: stop the battery draining while plugged in

## Why this happens

The adapter is rated 100 W but sustains about 75 W in practice. When the machine
wants more than that, macOS does not clamp the load - the battery quietly
supplies the difference. That is why the guard defaults to `--charger-watts 75`
and derives its core budget from the cap: `(75 - 15) / 7.5 = 8 cores`. There is no
`pmset` setting, no System Settings toggle and no API that says "run off the
adapter only". The only lever a program has is the load itself.

Measured on this machine (M4 Pro, 14 cores, 71 Wh battery, 2026-11-03):

| condition | CPU busy | battery |
|---|---|---|
| quiet, background only | 1-3 cores | steady |
| 14 busy loops, nothing else | 13-14 cores | steady at 75% for 5 min |
| 14 busy loops + normal background apps | ~15 cores | 78% -> 77% in 8 min |

1% of 71 Wh over 8 minutes is **8.5 W**. So the CPU alone at full load sits
just under the adapter's nominal 100 W, but above the ~75 W it can
hold continuously; it is the CPU *plus* display, GPU/ANE and the usual
background daemons that cross the line. That has two consequences:

* capping aggregate CPU is the only lever software has, and it is effective -
  the overdraw shows up as a SOC decline that the guard can drive to zero;
* the watts-per-core constant (`W_PER_CORE = 7.5`, `IDLE_W = 15`, `CHARGER_W = 75`) is a
  calibration, not a measurement of the GPU. `--auto` does not care: it steers
  on the actual battery trend whatever is causing the draw.

## What the tool does

`power_guard.py` measures per-process CPU demand from `ps` CPU-time
deltas (core-equivalents, the unit power actually scales in) and duty-cycles
the busy processes with `SIGSTOP`/`SIGCONT` so aggregate CPU converges on a
core budget.

The scheduler is an **integral controller**: every tick, each throttled
process's duty is multiplied by `budget / measured_load`. Over budget the duty
shrinks; under budget it relaxes back toward 1.0. Measured load settles at the
budget instead of ringing around it - `tests/test_power_guard.py` verifies this
against a simulated 30-tick run.

Modes:

* **Fixed budget** - `guard --budget-cores 8`. Predictable, no battery reading.
* **Self-calibrating** - `guard --auto`. Reads battery state of charge from
  `pmset` and evaluates one feedback window (default 180 s, `--battery-wh 71`
  for the 14" MBP). If SOC fell over that window, the load is above what the
  adapter can deliver, so the budget is cut by `drain_W / 7.5` cores - one
  correction per window, not one per tick - and the window resets. If SOC rose,
  the budget is nudged up. The learned budget is saved to
  `~/.config/power_guard.json`.

  With 1% SOC resolution on a 71 Wh cell, one window of 3 minutes resolves about
  14 W, i.e. under 2 cores. The loop converges over a few windows.

The guard idles when the machine is on battery (nothing to protect) and
SIGCONTs every process it stopped on exit or Ctrl-C. Stdlib only: `ps`, `pmset`,
`sysctl`, `os.kill`. No root, no pip install.

## Usage

```bash
# what is drawing power right now
python3 power_guard.py status

# hard cap the machine at 8 cores
python3 power_guard.py guard --auto              # budget derived from the 75 W cap

# self-calibrating, running in the background
nohup python3 power_guard.py guard --auto --budget-cores 12 \
      > /tmp/powerguard.log &

# run one heavy job capped, leaving everything else alone
python3 power_guard.py wrap --cores 6 python3 -m emflow.cli --adapter sample --input examples/test_3story.json --out out

# measure the knee on this machine (10 min, needs a otherwise-quiet machine)
python3 power_guard.py calibrate --minutes 5
```

`scripts/powerguard.plist` is a LaunchAgent that starts the guard at login:

```bash
cp scripts/powerguard.plist ~/Library/LaunchAgents/com.powerguard.plist
launchctl load ~/Library/LaunchAgents/com.powerguard.plist
```

## Verified behaviour

* 14 busy loops under `guard --budget-cores 6`: load fell from 13.5 cores to
  5.3-6.9 cores and held there for 20 ticks.
* `guard --auto` with a forced 14-core load: after one 3-minute window it
  detected a 14 W overdraw and trimmed the budget; with no overdraw it reports
  "steady, no drain" and leaves the budget alone.
* `wrap --cores 3` on an 8-process multiprocessing job: held at 2.99-3.40
  cores; exited cleanly when the job finished.
* `guard --auto`: reports "steady, no drain" when SOC is flat, and pulls the
  budget down when the battery is losing charge.

## Limits worth knowing

* **CPU only.** GPU and the Neural Engine are not measurable without root
  (`powermetrics` is root-only), so a GPU-bound job is not capped by the CPU
  budget. Cap it with its own thread/device settings.
* **Stopping is abrupt.** Duty-cycling stops processes for part of each tick.
  Apps with hard real-time deadlines (audio capture, video calls) can hiccup.
  Raise `--floor` (default 0.2 cores) or use `--exclude Name` to leave things
  untouched.
* **System processes are never touched** - `WindowServer`, `powerd`, `launchd`
  and friends are in `PROTECTED`.
* **Battery charge limits.** macOS often parks the battery at 80% ("not
  charging"). That is fine: overdraw still shows up as a slow SOC decline, which
  is exactly what `--auto` steers against.
* Unplugging mid-run releases the throttle; the guard does nothing on battery.
