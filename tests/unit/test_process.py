"""Pin the one module allowed to spawn processes.

Everything here runs the interpreter already running the tests, so no C++ toolchain is
involved. The timeout test really does wait a second: a kill that does not happen is the
failure mode worth paying for.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import cast

import pytest

from cpp_analysis_mcp import process
from cpp_analysis_mcp.process import SANITIZER_ENV_VARS, RunResult, hygienic_env, run

PYTHON = sys.executable

UNRELATED_VAR = "CPP_ANALYSIS_MCP_UNRELATED"

# pinned literally: reading these from the implementation would let a name silently
# drop out of the stripped set with every test below shrinking to match
EVERY_SANITIZER_VAR = ("ASAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "UBSAN_OPTIONS")


def test_stderr_is_merged_into_stdout() -> None:
    """A sanitizer writes its report to stderr; splitting the streams loses the interleaving."""
    result = run([PYTHON, "-c", "print('out'); import sys; sys.stderr.write('err')"], timeout_s=30)

    assert result.exit_code == 0
    assert not result.timed_out
    assert "out" in result.output
    assert "err" in result.output


def test_a_command_that_hangs_is_killed_and_says_so() -> None:
    started = time.monotonic()

    result = run([PYTHON, "-c", "import time; time.sleep(60)"], timeout_s=1)

    assert result.exit_code is None
    assert result.timed_out
    assert "[killed after 1s timeout]" in result.output
    # the kill has to land: reporting a timeout while still waiting out the full 60 seconds
    # would pass every assertion above
    assert time.monotonic() - started < 30


class UndeadProc:
    """A process whose pipe never closes: what a survivor of the tree kill looks like."""

    def __init__(self) -> None:
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        # bytes on purpose: text=True notwithstanding, that is what a timed-out
        # communicate() actually attaches on POSIX
        raise subprocess.TimeoutExpired(cmd="undead", timeout=timeout or 0, output=b"partial")

    def kill(self) -> None:
        self.killed = True


def test_a_kill_that_fails_still_returns_with_what_was_read() -> None:
    """taskkill cannot touch an elevated child, and a grandchild that made its own session
    escapes the POSIX group; either survivor holds the pipe open. Waiting on it with no
    bound would turn one timed-out analysis into a request that never comes back."""
    proc = UndeadProc()

    output = process._drained(cast("subprocess.Popen[str]", proc), timeout_s=1)

    assert proc.killed
    assert "partial" in output
    assert "may have survived" in output
    result = run([PYTHON, "-c", "raise SystemExit(66)"], timeout_s=30)

    assert result.exit_code == 66
    assert not result.timed_out


def test_timed_out_means_no_exit_code_at_all() -> None:
    """A signal death is a real outcome: -9 is killed, not timed out."""
    assert not RunResult(exit_code=0, output="").timed_out
    assert not RunResult(exit_code=66, output="").timed_out
    assert not RunResult(exit_code=-9, output="").timed_out
    assert RunResult(exit_code=None, output="").timed_out


def test_the_stripped_set_is_pinned() -> None:
    """The implementation must strip exactly these four; a shrunk tuple is a hygiene hole."""
    assert SANITIZER_ENV_VARS == EVERY_SANITIZER_VAR


def test_hygienic_env_drops_every_sanitizer_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's shell must not reach the child and change what it reports."""
    for name in EVERY_SANITIZER_VAR:
        monkeypatch.setenv(name, "verbosity=1")
    monkeypatch.setenv(UNRELATED_VAR, "keep me")

    env = hygienic_env({})

    assert [name for name in EVERY_SANITIZER_VAR if name in env] == []
    assert env[UNRELATED_VAR] == "keep me"


def test_overrides_are_the_only_way_in(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in EVERY_SANITIZER_VAR:
        monkeypatch.setenv(name, "verbosity=1")

    env = hygienic_env({"TSAN_OPTIONS": "history_size=7"})

    assert env["TSAN_OPTIONS"] == "history_size=7"
    assert "ASAN_OPTIONS" not in env
    assert "LSAN_OPTIONS" not in env
    assert "UBSAN_OPTIONS" not in env
