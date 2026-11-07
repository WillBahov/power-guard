# Power Guard — stop the battery draining while plugged in

`power_guard.py` keeps a plugged-in MacBook Pro off its battery by capping
**aggregate CPU load**. The adapter is rated 100 W but only sustains about
**83 W** under real load - the SMC reports the adapter pegged there, well below its 100 W nameplate - and a fully loaded M4 Pro plus the GPU and display
wants more, so macOS quietly borrows the difference from the battery. There is no `pmset` setting, no System Settings toggle and no API for
"run on the adapter only" — the only lever software has is the load itself.

The tool measures per-process CPU demand from `ps` CPU-time deltas (in
core-equivalents, the unit power actually scales in) and duty-cycles the busy
processes with `SIGSTOP`/`SIGCONT` so total CPU converges on a budget.

The guard acts at **85 W by default** - the measured knee where the battery starts
contributing. With a 20 W base draw and 7.5 W per loaded core that ceiling is 8.7
cores: `(85 - 20) / 7.5`. The core budget is derived from
the watt cap, so `--charger-watts` / `--budget-watts` decide when the throttle
engages.

* **Stdlib only** — `ps`, `pmset`, `sysctl`, `ctypes`, `os.kill`. No pip install, no root.
* **Live watts without root** — the AppleSMC user client (`smc.py`) reads DC-in, total system load and battery watts from an ordinary process, so the guard no longer needs the Stats app or `sudo powermetrics`.
* **Python 3.9+**, macOS only (the battery read uses `pmset`).
* Never touches system processes: essential daemons (`WindowServer`, `powerd`,
  `launchd`, …), accessibility agents, and any root-owned process are skipped by
  a safety policy modelled on OpenTamer's.
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

# 2. run the guard - the budget is derived from the 85 W cap and steered
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

`guard` runs a live dashboard whenever stdout is a terminal (use `--no-ui` for
the plain one-line-per-tick log, which is what the LaunchAgent uses). Each
information type sits in its own box — header, battery drain, power, load and
top processes — so nothing runs into the next, and colour separates them:
the drain box is red (green while charging), POWER/LOAD cyan, TOP PROCESSES
magenta, the header blue. The battery draw is the headline: a full-width box
with a sparkline of the last 24 ticks, and each process's proportional slice
of that drain in its own row:

```
┌─ POWER GUARD  15:20:46  AC ────────────┐
│ mode auto · holding back 3 process(es) │
└────────────────────────────────────────┘
┌─ BATTERY DRAIN ────────────────────────┐
│ ▁▂▅█▄█  12.4 W draining                │
│ soc 77% · health 100%                  │
│ cell 31.7 C · usable 74.2 Wh           │
└────────────────────────────────────────┘
┌─ POWER ───────────┐ ┌─ LOAD ────────────┐
│ adapter 94 W rated│ │ CPU 1.29 of 14    │
│ DC in   71.2 W    │ │ ██░░░░░│░░░░░░░░░ │
│ ceiling 60 W(6.0c)│ │ est draw 30 W     │
│ OVER by 11 W      │ └───────────────────┘
└───────────────────┘
┌─ TOP PROCESSES ──────────────────────────────────┐
│ node             9.7 W ██████████ full  7.0W bat │
│ com.apple.WebK   4.6 W █████▒▒▒▒▒ 60%  3.3W bat  │
│ python3          3.0 W ███▒▒▒▒▒▒▒ full  2.2W bat │
└──────────────────────────────────────────────────┘
 lost -1% in 3 min (14 W over the adapter) -> budget 5.0 cores
 TREND ▄▅▁▇▂▃▂   last 6 ticks
```

`health`, `cell` and `usable` come from the IOKit battery node — no root, no
Stats, no third-party app. The `│` in the load bar is the budget; the drain
sparkline is the last 24 ticks. Keys are read without blocking the control loop:

| Key | Effect |
|---|---|
| `+` / `-` | Raise or lower the core budget by 1 (2 cores minimum) |
| `a` | Toggle `--auto` battery feedback |
| `p` | Pause / resume the throttle (budget is kept) |
| `q` | Quit — releases every stopped process |

The dashboard is stdlib `curses`, and the rendering helpers (`gauge`, `spark`,
`watt_bar`, `head`) are pure functions covered by `tests/test_tui.py`; boxes
fall back to ASCII if the terminal can't encode box-drawing characters.

---

## The seven commands

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

### `power` — which telemetry this Mac will actually give you

```bash
python3 power_guard.py power
```

Lists every source the tool can reach and what it returned: `sudo powermetrics` if
sudo is passwordless, the AppleSMC user client, the Stats app cache, the IOKit
battery node. Purely diagnostic — it stops nothing. Run it once to find out why
`--auto` is or is not seeing real watts.

### `charge` — why the battery is parked, and what a tool could control

```bash
python3 power_guard.py charge
python3 power_guard.py charge --hold      # attempt an SMC charge-disable (root)
```

macOS 26.4+ has a native Charge Limit, readable without root through
`pmset -g battlimit`. On this machine it is set to 80% and the battery is
genuinely parked, not draining. `charge` decodes `NotChargingReason` into plain
language and reports which SMC charge keys exist, so you know whether a
"hold the charge" actuator is even possible here.

Worth being blunt about: stopping *charging* is not the same as stopping
*discharge*. When demand exceeds what the adapter can deliver, the cell still
feeds the gap, and only a load cap prevents that.

### `gate` — run a heavy build without blowing the budget

```bash
python3 power_guard.py gate --slots 1 --workers 4 -- cargo build
```

Queues heavy jobs (one at a time by default) and rewrites the toolchain's own
parallelism flag instead of pausing processes: `cargo --jobs`, `make -j`,
`ninja -j`, `gradle --max-workers`, `cmake --build -j`. For anything else it
exports `POWER_GUARD_MAX_WORKERS` and leaves the command alone. The queue lives
in `~/.cache/power_guard/gate` as one JSON file per running job, created with
`O_EXCL` so two processes cannot take the same slot; stale entries are swept, so
a crashed job cannot deadlock the queue.

`status` also groups the running heavy toolchains by project, so you can see
which build is the one eating the adapter before deciding what to cap.

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
| `--charger-watts W` | all | adapter rating, else `85` | The watt ceiling the guard acts at |
| `--sensor` | `guard`, `power` | `auto` | `sudo`, `smc`, `stats`, `soc`, or auto-detect |
| `--budget-watts W` | `guard` | same as `--charger-watts` | Engage when the estimated draw hits W |
| `--budget-cores N` | `guard`, `status` | derived from the watt cap | Pin the budget in cores |
| `--idle-watts W` | all | `20` | Base draw of an otherwise idle machine |
| `--w-per-core W` | all | `7.5` | Watts per busy core (calibration constant) |
| `--auto` | `guard` | off | Steer the budget off measured battery drain |
| `--fb-window S` | `guard` | `180` | Seconds of SOC history before a correction is made |
| `--tick S` | `guard`, `wrap` | `1.0` | Sampling/stop-continue period in seconds |
| `--floor N` | `guard` | `0.2` | Processes below this demand are never stopped |
| `--exclude NAME` | `guard` | — | Never stop this process (repeatable) |
| `--battery-wh Wh` | `guard`, `calibrate` | measured from the battery node (`73.9` here) | Battery capacity, for converting SOC drift to watts |
| `--duration S` | `guard` | run forever | Exit after N seconds |
| `--debug` | `guard` | off | Print each process's demand and duty every tick |
| `--quiet` | `guard` | off | Suppress the per-tick line |
| `--cores N` | `wrap` | `8` | Cap for the wrapped command |
| `--low-power` | `guard` | `off` | `off` / `auto` / `on` — Low Power Mode as the second throttle |
| `--slots N` | `gate` | `1` | Heavy jobs allowed at the same time |
| `--workers N` | `gate` | `4` | Worker cap the toolchain is told to use |
| `--poll S` | `gate` | `5` | Seconds between queue checks |
| `--always` | `gate` | off | Queue jobs even when the adapter has headroom |
| `--hold` | `charge` | off | Try the SMC charge-disable key (needs root, and the key must exist) |

`--w-per-core` and `--battery-wh` only matter for the *displayed* watt estimate
and for `calibrate`'s knee calculation. `--auto` steers on real SOC drift, so it
does not depend on the constant being right.

---

## How to pick a budget

The default budget is whatever the cap implies: 85 W minus a 20 W base draw, over
7.5 W per core = 8.7 cores on a 14-core M4 Pro. If the GPU or display is what is
really draining the battery, drop the ceiling (`--budget-watts 60`) or let
`--auto` find it. Then:

1. Run your real heavy workload.
2. `./power_guard.py calibrate --minutes 5` while that workload runs.
3. Use the printed knee, or just let `--auto` find it — it will trim the budget
   until SOC stops falling.

With 1% SOC resolution on a 71 Wh cell, one 3-minute window resolves about 14 W,
i.e. under 2 cores. Expect `--auto` to converge over a few windows (5–10 min).

---

## How the power limit is measured

There is a widespread belief that a 100 W charger "cannot keep up" with a
MacBook Pro. The sensors on this machine say otherwise:

* CPU + GPU at 100 % costs roughly **70 W**.
* The 100 W adapter peaks near **96 W of DC in**, and only while fast-charging.
* Once total demand passes about **82-85 W**, the battery supplies whatever the
  adapter cannot - observed as 84 W DC in plus 30 W from the battery for a
  114 W total during a heavy render.

So the guard's real rule is not "stay under 85 W". It is:

> **Never let the battery discharge while the adapter is attached.**

The core budget is only the means to that end. `--charger-watts` (default 85 W,
or the rating the connected adapter reports) is the fallback ceiling used when
no battery sensor is reachable.

### Which sensors are readable

| Source | What it gives | Needs |
|---|---|---|
| `sudo powermetrics` | real CPU / GPU / battery watts, sub-second | root |
| AppleSMC user client (`smc.py`) | DC in, total system load, battery watts, cell temperature, adapter physically present — sub-second | nothing |
| `proc_pid_rusage` (`energy.py`) | measured watts, cores and ANE footprint per process | nothing |
| Stats app cache | DC in, battery watts, adapter rating, ~50 s cadence (fallback) | Stats running |
| IOKit `AppleSmartBattery` | charge state, live battery watts (V×I), adapter rating, why charging stopped — sub-second | nothing |
| IOKit `AppleSmartBatteryPack` | battery health (`DesignCapacity` / `NominalChargeCapacity`) and cell temperature — sub-second | nothing |
| Battery SOC drift | true overdraw, 1% resolution, minutes | nothing |

Health and temperature are **not** flat registry keys on macOS 27. They live
inside the `BatteryData` dictionary on the pack node, so reading them needs a
nested lookup (`smartbattery._battery_data_number`). Getting this wrong returns
`None` silently rather than raising, which is why `tests/test_smartbattery.py`
asserts the shape of those fields on a real laptop.

Health matters for the control loop: `--auto` converts a SOC percentage drop
into watts using the pack's capacity. A worn battery holds less than the nominal
71 Wh, so assuming new capacity underestimates the drain and the throttle
under-corrects. `Guard.battery_wh` is measured at startup from the node; pass
`--battery-wh` only if you want to override it.

**The SMC is readable without root.** Earlier versions of this README said the
opposite: `IOServiceOpen` on `AppleSMC` with user-client type 0 succeeds from an
ordinary process on macOS 27.2, and `PDTR` (DC in), `PSTR` (system load), `SBAP`
(battery watts), `SBAV`/`ID0R` (volts/amps) and `TB0T`-`TB2T` (cell temperature)
all answer. Writing does not — a write returns `kIOReturnNotPrivileged`
(`0xe00002c1`), which is why ChargeWatch and Battery-Toolkit install a root
LaunchDaemon instead of touching the keys in-process. `PowerTelemetryData` in the
IORegistry is *frozen*: identical values across samples seconds apart, so the
SMC is the fresher source.

`smc.py` stands alone if you want to see it — `python3 smc.py` prints ten live
samples, `python3 smc.py probe` dumps the charge-control key table.

To make the guard independent of Stats, run it with sudo once:

```bash
./power_guard.py guard --auto                 # SMC watts, no root at all
```

`sudo powermetrics` is now only a fallback for the GPU/ANE detail the SMC does not
expose. To use Low Power Mode as a second throttle, install a scoped sudoers rule
instead of blanket sudo:

```bash
sudo bash scripts/lowpower-no-password.sh     # allows ONLY pmset -a lowpowermode 0|1
undo: sudo rm /etc/sudoers.d/power-guard
```

---

---

## Root is optional — here is what it buys you

Nothing in the guard needs root: the SMC power keys, the battery node, the
per-process energy counters and the charge limit are all readable from an
ordinary process. Root unlocks exactly two things:

1. **`sudo powermetrics`** — the GPU and Neural Engine breakdown the SMC does not
   expose. Either run the guard under sudo (the ticket is cached, so the tool's
   inner `sudo -n` calls succeed):

   ```bash
   sudo ./power_guard.py guard --auto --sensor sudo
   ```

2. **Low Power Mode** — the only throttle that reaches the display and GPU.
   Instead of blanket sudo, install a scoped rule that allows two exact
   commands and nothing else:

   ```bash
   sudo bash scripts/lowpower-no-password.sh     # pmset -a lowpowermode 0|1 only
   undo: sudo rm /etc/sudoers.d/power-guard
   ```

   Then `guard --low-power auto` turns Low Power Mode on only when the CPU budget
   has hit its floor and the battery is still draining, and turns it back off as
   soon as the drain stops.

Root does **not** unlock a "run on the adapter only" switch. The SMC key that
would do it (`CHTE`, charge-disable) does not exist on this Mac, and the only
charge key present (`CHIE`) cloaks the adapter — writing it makes the battery
discharge. That is a hardware/firmware limit, not a permissions limit.

---

## Prior art

Six related projects were read end to end. What shipped here is the mechanism
that survived contact with this machine:

| Repo | What was taken | What was not |
|---|---|---|
| ChargeWatching (MIT) | `pmset -g battlimit` reading; fail-safe discipline (restore on exit, Apple's 5% hysteresis, treat adapter presence as ground truth, not `ExternalConnected`) | the SMC-write LaunchDaemon — the key it needs is absent here |
| Fluxa-Mac (MIT) | the unprivileged SMC key set (`PDTR`, `PSTR`, `SBAV`, `ID0R`), backlight watts | its IOReport path, which is hardcoded to a CLPC model this Mac does not have |
| Battery Hog (MIT) | the scoped sudoers trick for Low Power Mode; the `gate` idea of capping toolchain workers instead of pausing processes | sleep-assertion blockers, not relevant here |
| ChargeMonitor (MIT) | responsible-PID grouping, which fixes PID churn in the load sampler | its disk/wakeup scoring, which the guard does not need |
| BetterBattery (MIT) | fan RPM and lifetime charge/discharge Wh as battery-health context | nothing else — it is a monitor, not a controller |
| powerlens (AGPL-3.0) | ideas only: the `holdingAtLimit` framing and its diagnostics engine | code, because AGPL is viral and this tool is stdlib-MIT |

## Known limits

* **CPU-only throttle.** `SIGSTOP` on a GPU-bound process does not stop its GPU
  work, and the SMC reports *system* watts rather than a GPU/ANE breakdown.
  `energy.py` does give per-process ANE footprint, and the difference between SMC
  `PSTR` and the sum of process watts is the part the CPU budget cannot touch.
  Low Power Mode (`--low-power auto`) is the only switch that reaches GPU, display
  and background work — which is why it is the second throttle.
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
* **PID churn, mostly fixed.** `responsibility_get_pid_responsible_for_pid`
  folds helper processes into the app that spawned them — the same call Activity
  Monitor uses — so a pool of short-lived workers counts as one process. A worker
  restarted mid-tick still starts at full duty, so `wrap` or `gate` remains the
  better choice for a job you can plan in advance.

---

## Files

| Path | What it is |
|---|---|
| `power_guard.py` | The tool — stdlib only, executable |
| `smc.py` | AppleSMC user client: live DC-in/system/battery watts, no root; charge-key probe |
| `energy.py` | Measured per-process watts via `proc_pid_rusage`, grouped by responsible app |
| `chargepolicy.py` | Native charge limit from `pmset -g battlimit`, `NotChargingReason` decode |
| `lowpower.py` | Low Power Mode read/toggle with the scoped-sudoers fail-safe |
| `workloads.py` | Toolchain detection, worker capping, heavy-job queue |
| `scripts/lowpower-no-password.sh` | Installs the narrow sudoers rule for Low Power Mode |
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
python3 -m pytest tests/ -q        # 88 tests: scheduler, watts math, SMC decoding,
                                     # charge policy, workloads, dashboard rendering
```

No machine state is required — the suite runs the scheduler against a simulated
load, so it verifies the controller converges rather than ringing.

---

## Verified on this machine

* **SMC reads work unprivileged.** `PDTR` 50–83 W DC in, `PSTR` 36–69 W system
  load, `SBAP` ≈ 0 W with the battery parked at 80%. `PDTR ≈ PSTR + |SBAP|` held
  in every sample.
* **`CHTE` does not exist on this Mac.** Only `CHIE` (adapter cloak) is present,
  and writing it disconnects the adapter and makes the battery *discharge* — the
  opposite of the goal — so `charge --hold` refuses to act on a machine with no
  charge-disable key. Writes from an ordinary process are refused outright.
* **Per-process watts are real.** One busy Python loop measured 5.7 W through
  `ri_energy_nj`; the `7.5 W/core` constant is now a calibration starting point
  rather than the source of truth.
* **The adapter's real ceiling is below its nameplate.** Under a 14-core CPU burn
  the 100 W brick stopped rising near 83 W while the cell supplied the difference.
* 12 busy loops under the default 85 W ceiling: load fell from ~14.6 cores to
  2–3 cores and stayed inside the budget. A 3-core budget with 4 loops settled
  at 1.7–2.9 cores after an initial transient.
* `wrap --cores 3` on an 8-process multiprocessing job: held at 2.99–3.40 cores,
  exited cleanly when the job finished.
* `calibrate --minutes 5` at full CPU load: 0 W drain — even a 100 W adapter covered
  it, which is why the guard's `--auto` loop, not the static constant, is the
  part you should trust.
