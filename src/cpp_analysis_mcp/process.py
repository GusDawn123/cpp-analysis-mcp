"""The single place subprocesses get launched: timeouts, output capture, environment
hygiene. Every layer calls through here, never subprocess directly (a test enforces it).
scripts/fixtures.py duplicates this on purpose, so a kill-path fix belongs in both.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# a sanitized run inherits these from the developer's shell and then reports something else
SANITIZER_ENV_VARS = ("ASAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "UBSAN_OPTIONS")

# how long the cleanup after a timeout may itself take: the tree kill and the final read
# of the pipe are each bound by this, because neither is guaranteed to finish on its own
KILL_GRACE_S = 10

# what a shell exits with when the command was not found. Borrowed deliberately: a caller
# reading exit codes should not have to learn a private convention for the one failure that
# already has a universal one.
NOT_FOUND_EXIT = 127


@dataclass(frozen=True, slots=True)
class RunResult:
    """What one child process produced."""

    # None means the run timed out -- never a real exit code
    exit_code: int | None
    # stderr merged into stdout: sanitizers report on stderr and the interleaving matters
    output: str

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None


def run(
    cmd: Sequence[str],
    *,
    timeout_s: int,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> RunResult:
    """Run a command to completion or to its timeout, stderr merged into stdout. A missing
    tool returns exit 127 with the OS's own message rather than raising -- callers already
    treat tool failure as an ordinary result to report.
    """
    try:
        # New session so a hung sanitizer takes its symbolizer child down with it.
        proc = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=cwd,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as missing:
        # NotADirectoryError: a PATH entry that is a file, which POSIX reports this way.
        # PermissionError: present but not executable, which is the same problem to a caller.
        return RunResult(exit_code=NOT_FOUND_EXIT, output=str(missing))
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        return RunResult(exit_code=None, output=_drained(proc, timeout_s))
    return RunResult(exit_code=proc.returncode, output=output)


def _kill_tree(pid: int) -> None:
    """Take down a timed-out process and its children: killpg on POSIX (start_new_session
    made the child its own group leader), taskkill /F /T on Windows, an already-gone tree
    ignored on both. sys.platform, not os.name -- only that spelling type-checks on both.
    """
    if sys.platform == "win32":
        # Full path: a bare "taskkill" can resolve through the process's current
        # directory, which this server does not control. Bounded, so this cleanup
        # can't hang the way the thing it's cleaning up did.
        taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(
                [str(taskkill), "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=KILL_GRACE_S,
            )
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _drained(proc: subprocess.Popen[str], timeout_s: int) -> str:
    """Collect what a killed process left in its pipe, without waiting forever for it. The
    tree kill isn't guaranteed -- an elevated child or a new-session grandchild survives and
    keeps the pipe open -- so this read is itself bound and the root killed if it expires.
    """
    try:
        output, _ = proc.communicate(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired as undead:
        proc.kill()
        return (
            _salvaged(undead) + f"\n[killed after {timeout_s}s timeout; parts of its"
            " process tree may have survived]\n"
        )
    return (output or "") + f"\n[killed after {timeout_s}s timeout]\n"


def _salvaged(undead: subprocess.TimeoutExpired) -> str:
    """Read the partial output off a timed-out communicate(), whatever shape it took:
    text=True notwithstanding, communicate() attaches bytes on POSIX and nothing at all
    on Windows -- only run() re-raises with str.
    """
    if isinstance(undead.output, bytes):
        return undead.output.decode(errors="replace")
    return undead.output if isinstance(undead.output, str) else ""


class Runner(Protocol):
    """run's call shape, named so anything that spawns can be handed a fake instead --
    living here so every layer that takes a runner takes the same one.
    """

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult: ...


def hygienic_env(overrides: Mapping[str, str]) -> dict[str, str]:
    """Copy the environment with every sanitizer option stripped, then apply overrides.
    Stricter than scripts/fixtures.py: nothing survives from the user's shell, and every
    pin arrives through overrides.
    """
    env = dict(os.environ)
    for name in SANITIZER_ENV_VARS:
        env.pop(name, None)
    env.update(overrides)
    return env
