"""Power telemetry that is actually reachable from an ordinary process.

Two sources, tried in order:

  1. The Stats app (eu.exelban.Stats). It already samples the adapter and the
     battery every ~50 s and stores the readings in its own LevelDB under
     ~/Library/Application Support/Stats/lldb. Its battery records carry
     `adapterPower` (DC in, watts), `batteryPower` (positive = charging,
     negative = draining) and `ACwatts` (the adapter's rating). It is the
     user's own file, so no privileges are needed.
  2. `sudo -n powermetrics`, which reports instantaneous watts but only works
     if sudo does not need a password.

If neither is available, the caller falls back to battery state-of-charge
drift, which is still a real measurement of overdraw - just much slower.
"""
from __future__ import annotations

import glob
import json
import re
import os
import subprocess
import time

STATS_DB = os.path.expanduser('~/Library/Application Support/Stats/lldb')
STALE_AFTER = 300.0          # Stats samples roughly every 50 s
RECORD = re.compile(rb'\{[^{}]*adapterPower[^{}]*\}')
POWER_CMD = ['sudo', '-n', 'powermetrics', '--samplers', 'power', '-i', '1000', '-n', '1']


def parse_power(text: str) -> dict:
    """Instantaneous watts from powermetrics output, e.g. 'Battery Power: -12.4 W'."""
    out = {}
    for line in text.splitlines():
        parts = line.split(':')
        if len(parts) == 2 and 'power' in parts[0].lower():
            value = parts[1].strip().replace('W', '').strip()
            try:
                out[parts[0].strip().lower()] = float(value)
            except ValueError:
                pass
    return out


def live_power() -> dict | None:
    """Watts from powermetrics, if sudo is passwordless. Otherwise None."""
    try:
        r = subprocess.run(POWER_CMD, capture_output=True, text=True, timeout=8)
    except Exception:
        return None
    watts = parse_power(r.stdout)
    if not watts:
        return None
    if 'battery power' in watts:
        watts['dc_in'] = watts.get('system power')
        watts['battery'] = watts['battery power']
    return watts


class StatsSensor:
    """The latest adapter/battery reading written by the Stats app."""

    def __init__(self, path: str = STATS_DB):
        self.path = path
        self.key = None
        self.cache = None

    def reading(self) -> dict | None:
        try:
            files = sorted(glob.glob(os.path.join(self.path, '*')), key=os.path.getmtime)
        except OSError:
            return None
        for f in reversed(files):
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            if (f, mtime) == self.key and self.cache is not None:
                return self.cache
            if time.time() - mtime > STALE_AFTER:
                continue
            try:
                size = os.path.getsize(f)
                with open(f, 'rb') as fh:
                    fh.seek(max(0, size - 262144))
                    data = fh.read()
            except OSError:
                continue
            records = RECORD.findall(data)
            if not records:
                continue
            try:
                rec = json.loads(records[-1].decode('utf-8', 'replace'))
            except Exception:
                continue
            self.key, self.cache = (f, mtime), rec
            return rec
        return None


def sensor_feed(stats: StatsSensor | None, use_sudo: bool) -> dict | None:
    """{name, dc_in, battery, adapter_rating, cores_allowed} from the best source."""
    if stats is not None:
        rec = stats.reading()
        if rec:
            dc = rec.get('adapterPower')
            batt = rec.get('batteryPower')
            rating = rec.get('ACwatts')
            return {
                'name': 'Stats',
                'dc_in': dc,
                'battery': batt,
                'adapter_rating': rating,
                'soc': round(rec.get('level', 0) * 100),
                'charging': rec.get('isCharging', False),
            }
    if use_sudo:
        watts = live_power()
        if watts:
            return {'name': 'powermetrics', 'dc_in': watts.get('dc_in'),
                    'battery': watts.get('battery'), 'adapter_rating': None,
                    'soc': None, 'charging': None}
    return None
