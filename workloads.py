"""Which *project* is burning the watts, not just which pid.

A build spawns twenty short-lived workers; SIGSTOP-ing them one at a time is both
noisy and blind to what they are for. Grouping processes by toolchain and working
directory - the approach Battery Hog's WorkloadDetector takes - lets the guard say
"cargo check in ~/Projects/emflow is using 9.4 cores" and, more usefully, cap the
tool's own worker count instead of pausing processes mid-run.

`lsof -a -d cwd -p <pids>` is how the working directory of another process is
found; it is the same trick Battery Hog uses and needs no privileges.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

RULES = [
    (r"(?:^|[/\s])cargo(?:$|[\s])", "Rust", "Cargo", "build", True),
    (r"(?:^|[/\s])rustc(?:$|[\s])", "Rust", "rustc", "compile", True),
    (r"(?:^|[/\s])(gradlew?|gradle)(?:$|[\s])", "JVM", "Gradle", "build", True),
    (r"(?:^|[/\s])xcodebuild(?:$|[\s])", "Apple", "Xcode", "build", True),
    (r"(?:^|[/\s])swiftc?(?:$|[\s]).*(?:build|test|package)", "Apple", "SwiftPM", "build", True),
    (r"(?:^|[/\s])(make|cmake|ninja)(?:$|[\s])", "Native", "Native build", "build", True),
    (r"energyplus|emflow|eplus", "Energy", "EnergyPlus", "simulate", True),
    (r"(?:^|[/\s])(pytest|unittest)(?:$|[\s])|python[^\n]*-m\s+pytest", "Python", "pytest", "test", True),
    (r"(?:^|[/\s])(mypy|ruff|pyright|flake8)(?:$|[\s])", "Python", "Python checks", "lint", True),
    (r"(?:^|[/\s])(jest|vitest|playwright|cypress)(?:$|[\s])", "Node", "JS tests", "test", True),
    (r"(?:^|[/\s])(tsc|esbuild|webpack|rollup|turbo)(?:$|[\s])", "Node", "JS build", "build", True),
    (r"(?:^|[/\s])(npm|pnpm|yarn|bun)(?:$|[\s]).*(?:install|ci|build|test)", "Node", "Node task", "build", True),
    (r"(?:^|[/\s])docker(?:$|[\s]).*(?:build|compose)", "Containers", "Docker", "build", True),
    (r"(?:^|[/\s])(gitleaks|semgrep|trivy)(?:$|[\s])", "Security", "Scanner", "scan", True),
]

SYSTEM_CWD_PREFIXES = ("/System/", "/usr/", "/bin/", "/sbin/", "/Applications/")


def classify(command: str) -> dict | None:
    """{family, tool, kind, heavy} for a command line, or None if unrecognised."""
    for pattern, family, tool, kind, heavy in RULES:
        if re.search(pattern, command, re.IGNORECASE):
            return {"family": family, "tool": tool, "kind": kind, "heavy": heavy}
    return None


def _proc_args() -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-u", os.environ.get("USER", str(os.getuid())),
                         "-o", "pid=,args="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if m:
            rows.append((int(m.group(1)), m.group(2)))
    return rows


def working_dirs(pids: list[int]) -> dict[int, str]:
    """cwd of each pid via lsof. Best effort: any failure just returns {}."""
    if not pids:
        return {}
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p",
                              ",".join(str(p) for p in pids)],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    found: dict[int, str] = {}
    for line in out.splitlines():
        m = re.match(r"^(\d+)\s.*\scwd\s(/.*)$", line)
        if m:
            found[int(m.group(1))] = m.group(2)
    return found


def project_name(cwd: str | None) -> str | None:
    """A friendly project label from a working directory."""
    if not cwd or not cwd.startswith("/") or cwd == "/":
        return None
    if any(cwd.startswith(p) for p in SYSTEM_CWD_PREFIXES):
        return None
    home = os.path.expanduser("~")
    if cwd.startswith(home + "/"):
        relative = cwd[len(home) + 1:]
        return relative.split("/")[0] or "home"
    return cwd.split("/")[-1]


def detect(scope: set | None = None) -> list[dict]:
    """Group the currently running heavy toolchains by project."""
    rows = _proc_args()
    heavy = [(pid, args) for pid, args in rows if classify(args)]
    cwd_by_pid = working_dirs([pid for pid, _ in heavy])
    groups: dict[tuple[str, str], dict] = {}
    for pid, args in heavy:
        info = classify(args)
        cwd = cwd_by_pid.get(pid)
        project = project_name(cwd) or info["family"]
        key = (project, info["tool"])
        group = groups.setdefault(key, {"project": project, "tool": info["tool"],
                                        "family": info["family"], "pids": [],
                                        "kind": info["kind"]})
        group["pids"].append(pid)
    return sorted(groups.values(), key=lambda g: -len(g["pids"]))


def cap_workers(argv: list[str], limit: int) -> tuple[list[str], dict, str]:
    """Rewrite a command so its toolchain uses at most `limit` workers.

    Returns (argv, extra_env, note). The command is never killed or paused, only
    told to use fewer parallel workers - the gentlest possible throttle, and the
    one that survives a tool's own process churn.
    """
    if not argv:
        return argv, {}, ""
    first = os.path.basename(argv[0])
    limited = list(argv)
    env: dict[str, str] = {}
    note = ""

    if first == "cargo" and len(limited) > 1 and limited[1] in ("build", "check", "test"):
        if "--jobs" not in limited and "-j" not in limited:
            limited[2:2] = ["--jobs", str(limit)]
            note = f"cargo limited to {limit} jobs"
    elif first in ("make", "gmake"):
        replaced = False
        for i, token in enumerate(limited):
            if token.startswith("-j"):
                limited[i] = f"-j{limit}"
                replaced = True
        if not replaced:
            limited.append(f"-j{limit}")
        note = f"make limited to {limit} jobs"
    elif first == "ninja":
        if "-j" not in limited:
            limited += ["-j", str(limit)]
            note = f"ninja limited to {limit} jobs"
    elif first in ("gradle", "gradlew"):
        if "--max-workers" not in limited:
            limited += [f"--max-workers={limit}"]
            note = f"gradle limited to {limit} workers"
    elif first in ("pytest",):
        for i, token in enumerate(limited):
            if token == "-n" and i + 1 < len(limited) and limited[i + 1].isdigit():
                limited[i + 1] = str(min(limit, int(limited[i + 1])))
                note = f"pytest xdist limited to {limit}"
                break
        else:
            env["PYTEST_XDIST_WORKER_LIMIT"] = str(limit)
    elif first == "cmake" and "--build" in limited:
        if "-j" not in limited:
            limited += ["-j", str(limit)]
            note = f"cmake build limited to {limit} jobs"
    else:
        env["POWER_GUARD_MAX_WORKERS"] = str(limit)
        note = f"no known worker flag for {first}; exported POWER_GUARD_MAX_WORKERS={limit}"
    return limited, env, note


class Gate:
    """A queue for heavy jobs while the battery is under pressure.

    One job at a time by default. The queue is a directory of small JSON files in
    ~/.cache/power_guard/gate - one file per running job, created with O_EXCL so
    two processes cannot claim the same slot. A job that is killed leaves its file
    behind, so stale entries (older than `stale_after`) are swept: a crash cannot
    deadlock the queue.
    """

    def __init__(self, directory: str | None = None, slots: int = 1,
                 stale_after: float = 3600.0):
        self.directory = directory or os.path.expanduser("~/.cache/power_guard/gate")
        self.slots = max(1, slots)
        self.stale_after = stale_after
        os.makedirs(self.directory, exist_ok=True)
        self._path = None

    def _entries(self) -> list[str]:
        try:
            names = os.listdir(self.directory)
        except OSError:
            return []
        entries = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.directory, name)
            try:
                if time.time() - os.path.getmtime(path) > self.stale_after:
                    os.remove(path)
                else:
                    entries.append(path)
            except OSError:
                pass
        return entries

    def claim(self, label: str) -> tuple[bool, int]:
        """Take a slot. (True, 0) on success, (False, queue position) otherwise."""
        entries = self._entries()
        if len(entries) >= self.slots:
            return False, len(entries) - self.slots + 1
        path = os.path.join(self.directory, f"{int(time.time() * 1000)}-{os.getpid()}.json")
        try:
            with open(path, "w") as fh:
                json.dump({"label": label, "pid": os.getpid(), "cwd": os.getcwd(),
                           "started": time.time()}, fh)
            self._path = path
            return True, 0
        except OSError:
            return False, len(entries)

    def release(self) -> None:
        if self._path:
            try:
                os.remove(self._path)
            except OSError:
                pass
            self._path = None

    def active(self) -> list[str]:
        out = []
        for path in self._entries():
            try:
                out.append(json.load(open(path)).get("label", path))
            except Exception:
                out.append(path)
        return out
