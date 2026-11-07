"""Toolchain detection, worker capping and the heavy-job queue."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from workloads import classify, cap_workers, project_name, Gate  # noqa: E402


def test_classify_recognises_the_common_build_tools():
    assert classify("cargo build --release")["tool"] == "Cargo"
    assert classify("make -j16")["family"] == "Native"
    assert classify("pytest -n 8")["kind"] == "test"
    assert classify("energyplus -W weather.epw in.idf")["family"] == "Energy"
    assert classify("cat notes.txt") is None


def test_a_plain_read_is_not_a_workload():
    assert classify("less README.md") is None


def test_cap_workers_rewrites_parallel_flags_without_killing_anything():
    argv, env, note = cap_workers(["cargo", "build", "release"], 4)
    assert argv == ["cargo", "build", "--jobs", "4", "release"]
    assert "cargo limited to 4 jobs" in note

    argv, _, _ = cap_workers(["make", "-j32", "all"], 4)
    assert "-j4" in argv and "-j32" not in argv

    argv, _, _ = cap_workers(["cmake", "--build", "build"], 6)
    assert argv[-2:] == ["-j", "6"]

    argv, env, _ = cap_workers(["gradle", "test"], 2)
    assert "--max-workers=2" in argv

    argv, env, note = cap_workers(["some-unknown-tool", "--go"], 3)
    assert env["POWER_GUARD_MAX_WORKERS"] == "3"
    assert "no known worker flag" in note


def test_existing_flags_are_respected_not_duplicated():
    argv, _, note = cap_workers(["cargo", "check", "--jobs", "2"], 8)
    assert argv == ["cargo", "check", "--jobs", "2"]
    assert note == ""


def test_project_name_is_readible_from_a_working_directory():
    assert project_name("/Users/will/Documents/Projects/emflow/src") == "Documents"
    assert project_name("/System/Library/Frameworks") is None
    assert project_name("/") is None
    assert project_name(None) is None


def test_gate_serialises_heavy_jobs_and_release_frees_a_slot(tmp_path):
    gate = Gate(str(tmp_path), slots=1)
    ok, position = gate.claim("cargo build")
    assert ok and position == 0
    ok2, position2 = gate.claim("make all")
    assert not ok2 and position2 == 1
    gate.release()
    assert gate.claim("make all")[0]
    assert gate.active()


def test_a_crashed_job_does_not_deadlock_the_queue(tmp_path):
    gate = Gate(str(tmp_path), slots=1, stale_after=0.001)
    gate.claim("job that will die")
    time.sleep(0.01)
    assert gate._entries() == []
    assert gate.claim("next job")[0]
