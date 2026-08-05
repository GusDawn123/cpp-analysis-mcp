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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# a sanitized run inherits these from the developer's shell and then reports something else
SANITIZER_ENV_VARS = ("ASAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "UBSAN_OPTIONS")


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
    """Run a command to completion or to its timeout, stderr merged into stdout."""
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
    try:
        output, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # start_new_session made the child its own group leader, so its pid is the pgid;
        # it may also exit on its own between the timeout and the kill.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        return RunResult(
            exit_code=None,
            output=(output or "") + f"\n[killed after {timeout_s}s timeout]\n",
        )
    return RunResult(exit_code=proc.returncode, output=output)


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
