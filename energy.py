"""Measured per-process watts, straight from the kernel - no root required.

`proc_pid_rusage(pid, RUSAGE_INFO_V6, buf)` hands back a struct that includes
`ri_energy_nj` (nanojoules the process has burned since it started), `ri_cycles`
and `ri_neural_footprint`. Subtracting two snapshots and dividing by the elapsed
time gives *real* watts per process. On this machine six busy Python loops read
4.8 W each - a measured number, not the `cores x 7.5 W` guess the guard has been
using.

The field offsets are the kernel's rusage_info_v6 layout: a 16-byte uuid followed
by u64s, so field i sits at 8*i. Only a handful of those fields are used here.

`responsibility_get_pid_responsible_for_pid` is the same call Activity Monitor
uses to fold helper processes into the app that launched them. It fixes the guard's
"PID churn" limitation: a multiprocessing pool that keeps spawning workers is seen
as one responsible process instead of a swarm of unrelated PIDs.
"""
from __future__ import annotations

import ctypes
import time

RUSAGE_INFO_V6 = 6
BUFFER_SIZE = 512          # rusage_info_v6 is 384 bytes; 512 is safe
PROC_ALL_PIDS = 1

# index into the u64 array (uuid occupies 0 and 1)
FIELDS = {
    "user_time": 2,
    "system_time": 3,
    "resident_size": 8,
    "instructions": 31,
    "cycles": 32,
    "runnable_time": 36,
    "energy_nj": 42,
    "parent_energy_nj": 43,
    "neural_footprint": 45,
}


def _build_libc():
    libc = ctypes.cdll.LoadLibrary("libc.dylib")
    libc.proc_listpids.argtypes = [ctypes.c_int, ctypes.c_uint64,
                                   ctypes.c_void_p, ctypes.c_int]
    libc.proc_listpids.restype = ctypes.c_int
    libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.proc_pidpath.restype = ctypes.c_int
    libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    libc.proc_pid_rusage.restype = ctypes.c_int
    libc.responsibility_get_pid_responsible_for_pid.argtypes = [
        ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    libc.responsibility_get_pid_responsible_for_pid.restype = ctypes.c_int
    return libc


_libc = None


def libc():
    global _libc
    if _libc is None:
        _libc = _libc_or_none()
    return _libc


def _libc_or_none():
    try:
        return _build_libc()
    except Exception:
        return None


def field(buf: bytes, name: str) -> int:
    """Read one u64 field out of a rusage buffer. Pure, so it is testable."""
    offset = 8 * FIELDS[name]
    if len(buf) < offset + 8:
        return 0
    return int.from_bytes(buf[offset:offset + 8], "little")


def watts(previous: bytes, current: bytes, dt: float) -> float:
    """Nanojoules between two snapshots converted to watts."""
    if dt <= 0:
        return 0.0
    delta = field(current, "energy_nj") - field(previous, "energy_nj")
    if delta < 0:
        return 0.0                      # pid recycled: the clock restarted
    return delta * 1e-9 / dt


def all_pids() -> list[int]:
    lib = libc()
    if lib is None:
        return []
    try:
        count = lib.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    except Exception:
        return []
    if count <= 0:
        return []
    buf = (ctypes.c_int * (count // 4 + 1))()
    try:
        got = lib.proc_listpids(PROC_ALL_PIDS, 0, buf, count)
    except Exception:
        return []
    return [p for p in buf[:max(0, got) // 4] if p > 0]


def snapshot(pid: int) -> bytes | None:
    lib = libc()
    if lib is None:
        return None
    buf = (ctypes.c_uint8 * BUFFER_SIZE)()
    try:
        if lib.proc_pid_rusage(pid, RUSAGE_INFO_V6, buf) != 0:
            return None
    except Exception:
        return None
    return bytes(buf)


def process_name(pid: int) -> str:
    lib = libc()
    if lib is None:
        return ""
    buf = (ctypes.c_char * 4096)()
    try:
        n = lib.proc_pidpath(pid, buf, 4096)
    except Exception:
        return ""
    if n <= 0:
        return ""
    return bytes(buf[:n]).decode("utf-8", "replace").split("/")[-1]


def responsible_pid(pid: int) -> int | None:
    """The app Activity Monitor would blame for this process."""
    lib = libc()
    if lib is None:
        return None
    out = ctypes.c_int(0)
    try:
        if lib.responsibility_get_pid_responsible_for_pid(pid, ctypes.byref(out)) != 0:
            return None
    except Exception:
        return None
    return out.value if out.value > 0 and out.value != pid else None


class EnergySampler:
    """Watts per pid over the interval between two rusage snapshots."""

    def __init__(self, scope: set | None = None):
        self.scope = scope
        self.prev: dict[int, tuple[float, bytes]] = {}
        self.names: dict[int, str] = {}
        self.cores_from_energy: dict[int, float] = {}
        self._last: dict[int, float] = {}

    def sample(self) -> dict[int, float]:
        lib = libc()
        if lib is None:
            return {}
        now = time.monotonic()
        pids = list(self.scope) if self.scope else all_pids()
        out: dict[int, float] = {}
        for pid in pids:
            snap = snapshot(pid)
            if snap is None:
                continue
            if pid in self.prev:
                dt = now - self.prev[pid][0]
                if dt > 0:
                    w = watts(self.prev[pid][1], snap, dt)
                    out[pid] = w
                    cycles = (field(snap, "cycles") - field(self.prev[pid][1], "cycles"))
                    self.cores_from_energy[pid] = (cycles / 1e9) / dt if cycles > 0 else 0.0
            self.prev[pid] = (now, snap)
            self.names[pid] = process_name(pid)
        self._last = out
        return out

    def total(self) -> float:
        """Watts accounted for by every process we can read. Anything the kernel
        does not attribute to a process (display, GPU, memory rails) is the
        difference between this and SMC `PSTR`."""
        return sum(w for w in self._last.values()) if self._last else 0.0


def fold_into_apps(per_pid: dict[int, float]) -> dict[str, float]:
    """Group per-PID watts by the app Activity Monitor would hold responsible.

    Returns {name: watts}. Helper processes (a WebKit content child, a multiprocessing
    worker) are attributed to the app that spawned them.
    """
    lib = libc()
    if lib is None:
        return {}
    grouped: dict[str, float] = {}
    for pid, watts_value in per_pid.items():
        owner = responsible_pid(pid) or pid
        name = process_name(owner) or f"pid {owner}"
        grouped[name] = grouped.get(name, 0.0) + watts_value
    return grouped
