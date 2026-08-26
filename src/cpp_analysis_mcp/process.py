"""The single place subprocesses get launched. Owns timeouts, output capture and environment
hygiene. Every layer that touches a host tool goes through here instead of calling subprocess
directly, so the rules about what may run and for how long are enforced in one file rather than
restated at each call site; a test walks the package and fails any other module that imports
subprocess.

scripts/fixtures.py carries its own copy of this logic on purpose: it predates the package,
runs bare in CI, and imports nothing from src/. The duplication is the price of that, so a
fix to the kill path belongs in both.
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
    """Run a command to completion or to its timeout, stderr merged into stdout.

    A tool that is not installed comes back as exit 127 carrying the OS's own words, not as
    an exception. Every caller here already treats "the tool failed" as an ordinary thing to
    observe and report -- a capability probe exists precisely to find out that a tool is
    missing -- and a raise would turn that answer into a crash one layer above, taking the
    whole startup down with it because one optional tool was absent.
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
    """Take down a timed-out process and its children, in each OS's own words.

    POSIX: start_new_session made the child its own group leader, so its pid is the pgid;
    it may also exit on its own between the timeout and the kill. Windows has no process
    groups to signal (start_new_session is inert there), so taskkill /T walks the child's
    process tree instead -- same semantics, and a tree already gone exits nonzero, which
    is the ProcessLookupError case and is ignored the same way.

    The branch is on sys.platform rather than os.name because mypy narrows the former:
    killpg and SIGKILL do not exist in Windows stubs, and only this spelling lets each
    OS type-check the code it will actually run.
    """
    if sys.platform == "win32":
        # by full path: bare "taskkill" is resolved through a search that can reach the
        # process's current directory, and this server is launched from directories it
        # does not control. Bounded, because the cleanup after a timeout must not be
        # able to hang the way the thing it is cleaning up did.
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
    """Collect what a killed process left in its pipe, refusing to wait forever for it.

    The tree kill is not guaranteed: taskkill cannot touch an elevated child, and a
    grandchild that started its own session escapes the POSIX group. A survivor keeps
    the pipe's write end open, and a communicate() with no timeout then blocks on it --
    one timed-out analysis becoming a request that never returns. So the read is bound,
    the root is killed directly when it expires, and whatever output was gathered before
    giving up still comes back, with the note saying what happened.
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
    """Read the partial output off a timed-out communicate(), whatever shape it took.

    text=True notwithstanding, communicate() attaches what it had read as bytes on
    POSIX and attaches nothing at all on Windows -- only run() re-raises with str.
    """
    if isinstance(undead.output, bytes):
        return undead.output.decode(errors="replace")
    return undead.output if isinstance(undead.output, str) else ""


class Runner(Protocol):
    """run's call shape, named so anything that spawns can be handed a fake instead.

    Lives here rather than beside its callers so every layer that takes a runner takes the
    same one, and a test can inject something that never reaches a subprocess.
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

    Stricter than scripts/fixtures.py, which drops two of the four and pins two of its own:
    here nothing survives from the user's shell and every pin arrives through overrides.
    """
    env = dict(os.environ)
    for name in SANITIZER_ENV_VARS:
        env.pop(name, None)
    env.update(overrides)
    return env
