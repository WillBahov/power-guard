"""Low Power Mode as a graceful throttle.

`SIGSTOP` is abrupt: it hiccups anything with a real-time deadline, and it cannot
touch the GPU, the display or the Neural Engine. macOS' Low Power Mode is the
system-wide version of the same idea - it trims CPU frequency, display brightness
and background work. It is the only lever that reaches the parts of the draw that
core counting cannot see.

Reading it needs nothing. Setting it needs root, so the repo ships
`scripts/lowpower-no-password.sh`, which installs a *narrowly scoped* sudoers rule
allowing only the two exact pmset invocations - the same trick Battery Hog uses -
rather than asking the user to give the tool blanket sudo.
"""
from __future__ import annotations

import re
import subprocess

SET_CMD = ["sudo", "-n", "pmset", "-a", "lowpowermode"]


def _parse_pmset(text: str) -> bool | None:
    """True/False from `pmset -g` output; None when the key is absent."""
    for pattern in (r"low\s?power\s?mode\D*(\d+)", r"lowpowermode\D*(\d+)",
                    r"powermode\D*(\d+)"):
        m = re.search(pattern, text)
        if m:
            return m.group(1) == "1"
    return None


def is_enabled() -> bool | None:
    try:
        out = subprocess.run(["pmset", "-g"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return None
    return _parse_pmset(out)


def set_enabled(on: bool) -> bool:
    """Turn Low Power Mode on/off. Returns False if sudo is not passwordless."""
    try:
        r = subprocess.run(SET_CMD + ["1" if on else "0"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return False
    return r.returncode == 0 and "password" not in r.stderr.lower()


def sudo_hint() -> str:
    return ("Low Power Mode needs root without a prompt. Install the scoped rule:\n"
            "    sudo bash scripts/lowpower-no-password.sh\n"
            "It only allows `pmset -a lowpowermode 0` and `... 1` for your user. "
            "Undo with: sudo rm /etc/sudoers.d/power-guard")
