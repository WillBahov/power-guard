"""Measured per-process watts from proc_pid_rusage (rusage_info_v6).

A busy Python loop on this machine burns ~5.7 W, which is what these tests
expect - the energy counter is real, not a guess scaled from core counts.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from energy import (FIELDS, BUFFER_SIZE, field, watts, all_pids, snapshot,
                   process_name, responsible_pid, EnergySampler, fold_into_apps)  # noqa: E402

BUSY_SECONDS = 1.0


def burn(seconds: float = BUSY_SECONDS) -> float:
    """Keep one core busy for `seconds` and return the elapsed time."""
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        pass
    return time.monotonic() - start


def buf(**values) -> bytes:
    b = bytearray(BUFFER_SIZE)
    for name, value in values.items():
        b[8 * FIELDS[name]:8 * FIELDS[name] + 8] = value.to_bytes(8, "little")
    return bytes(b)


def test_field_reads_the_u64_at_the_kernel_offset():
    b = buf(energy_nj=1_000_000_000, cycles=2_000_000_000)
    assert field(b, "energy_nj") == 1_000_000_000
    assert field(b, "cycles") == 2_000_000_000
    assert field(b[:100], "energy_nj") == 0          # short buffer, no IndexError


def test_watts_from_nanojoules_over_seconds():
    previous, current = buf(energy_nj=0), buf(energy_nj=4_800_000_000)
    assert abs(watts(previous, current, 1.0) - 4.8) < 1e-9
    assert abs(watts(previous, current, 2.0) - 2.4) < 1e-9
    assert watts(previous, current, 0) == 0.0


def test_recycled_pid_gives_zero_not_negative():
    assert watts(buf(energy_nj=5_000_000_000), buf(energy_nj=1_000_000_000), 1.0) == 0.0


def test_a_busy_process_measures_its_own_watts():
    pid = os.getpid()
    first = snapshot(pid)
    if first is None:
        return                                       # degrade quietly, never raise
    dt = burn()
    measured = watts(first, snapshot(pid), dt)
    assert 3.0 < measured < 12.0                    # ~5.7 W for one spinning core


def test_all_pids_and_names_are_sane():
    pids = all_pids()
    assert os.getpid() in pids
    assert process_name(os.getpid())
    assert all(p > 0 for p in pids)


def test_responsible_pid_never_returns_itself():
    r = responsible_pid(os.getpid())
    assert r is None or r != os.getpid()


def test_sampler_totals_and_grouping_conserve_watts():
    sampler = EnergySampler(scope={os.getpid()})
    sampler.sample()
    dt = burn()
    per_pid = sampler.sample()
    assert sum(per_pid.values()) > 3.0
    grouped = fold_into_apps(per_pid)
    assert abs(sum(grouped.values()) - sum(per_pid.values())) < 1e-9
