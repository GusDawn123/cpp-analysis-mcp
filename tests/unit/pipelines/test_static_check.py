"""Drive the whole static_check chain with no compiler, no clang-tidy and no child process.

The only thing faked is the subprocess boundary: the capability gate, the command each check
composes, the environment it goes out under and the parsing are all the real code, and the
fake answers each spawn with text a real tool once printed. That is why the replies are
committed goldens rather than invented strings -- a chain that parsed a hand-written
approximation would keep passing on the day it stopped understanding the real thing.

Every expectation is written down rather than read from the code under test: the argv each
check composes, the category and line each golden holds, the words gcc uses to refuse
-Wthread-safety. Assertions are on what the fake recorded and on what came back, never on how
many times something was called.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from helpers import GOLDEN_DIR, bug_line

from cpp_analysis_mcp.models import Analysis, AnalysisReport, BuildFailure, CapabilityStatus
from cpp_analysis_mcp.pipelines.static_check import CHECK_TIMEOUT_S, check_file, check_snippet
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.toolchains.base import Toolchain

# ---------------------------------------------------------------- pinned expectations

CLANG_PATH = "/usr/bin/clang++"
CLANG_WARNING_FLAGS = ("-Wthread-safety",)
LINUX_COMPILE_EXTRAS = ("-pthread",)

TIDY_NAME = "clang-tidy"

# none of these may reach a spawn from the developer's shell
EVERY_SANITIZER_VAR = ("ASAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "UBSAN_OPTIONS")

# what a poisoned shell would have set them to: enough to change what a run reports
POISON = "verbosity=1:halt_on_error=0:detect_leaks=0"

SOURCE_STEM = "unguarded_write"

# the goldens the fake replays, and what was read out of each by eye
THREAD_SAFETY_GOLDEN = "thread_safety_unguarded_write.darwin-clang.txt"
CLANG_TIDY_GOLDEN = "clang_tidy_nullptr_zero.linux-clang.txt"

THREAD_SAFETY_CATEGORY = "thread-safety-analysis"
THREAD_SAFETY_MESSAGE = (
    "writing variable 'counter' requires holding mutex 'counter_mutex' exclusively"
)
THREAD_SAFETY_LINE = 21

TIDY_CATEGORY = "modernize-use-nullptr"
TIDY_MESSAGE = "use nullptr"
TIDY_LINE = 5

# the explicit request the nullptr fixture needs: tidy's defaults are silent on it
EXPLICIT_CHECKS = "-*,modernize-use-nullptr"

# the words the probe wrote down when a gcc toolchain was asked for -Wthread-safety;
# spelled out here rather than imported, so a reworded refusal is visible in the diff
GCC_DENIED_REASON = (
    "gcc has no equivalent of clang's -Wthread-safety, not a weaker version. "
    "Installing clang enables this check while the build stays on gcc."
)

# the probe's own words, as capabilities.py phrases them for a working -Wthread-safety
VERIFIED_BY = (
    "compiled a planted write to a guarded_by variable with no lock held; "
    "-Wthread-safety reported it"
)

CLEAN = RunResult(exit_code=0, output="")

# clang-tidy on code that does not compile: it files the errors under a check of its own
BROKEN_CODE = RunResult(
    exit_code=1,
    output=(
        "/w/snippet.cpp:2:5: error: use of undeclared identifier 'counter' "
        "[clang-diagnostic-error]\n"
        "/w/snippet.cpp:4:1: error: expected '}' [clang-diagnostic-error]\n"
        "2 errors generated.\n"
        "Error while processing /w/snippet.cpp.\n"
        "Found compiler error(s).\n"
    ),
)

# a driver failure: nonzero, and nothing in it a parser can turn into a finding
DRIVER_FAILED = RunResult(
    exit_code=1,
    output="error: no such file or directory: '/w/gone.cpp'\nNo such file or directory\n",
)


def golden(name: str) -> str:
    """Read a captured run; the pipeline meets it as a fake process's output."""
    path: Path = GOLDEN_DIR / name
    assert path.is_file(), f"missing golden {path}"
    return path.read_text(encoding="utf-8")


def timed_out(output: str = "") -> RunResult:
    """What process.run hands back when it had to kill the tool."""
    return RunResult(
        exit_code=None, output=output + f"\n[killed after {CHECK_TIMEOUT_S}s timeout]\n"
    )


# --------------------------------------------------------------------------- the fakes


@dataclass(frozen=True, slots=True)
class Spawn:
    """One call that reached the subprocess boundary, recorded whole."""

    cmd: list[str]
    timeout_s: int
    env: dict[str, str] | None
    cwd: Path | None


@dataclass
class ScriptedRunner:
    """Answer the scripted results in call order and record every call it was handed."""

    script: list[RunResult]
    spawns: list[Spawn] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        assert len(self.spawns) < len(self.script), (
            f"spawn {len(self.spawns) + 1} was never scripted: {list(cmd)}"
        )
        self.spawns.append(
            Spawn(
                cmd=list(cmd),
                timeout_s=timeout_s,
                env=dict(env) if env is not None else None,
                cwd=cwd,
            )
        )
        return self.script[len(self.spawns) - 1]

    @property
    def checked(self) -> Spawn:
        """The check's spawn: the only one, since neither analysis links or runs anything."""
        assert len(self.spawns) == 1, f"expected exactly one spawn, got {self.spawns}"
        return self.spawns[0]

    def env_of(self, spawn: Spawn) -> dict[str, str]:
        env = spawn.env
        assert env is not None, "a check must be given an environment, not the inherited one"
        return env


@dataclass
class RefusingRunner:
    """A runner that fails the test rather than spawn anything."""

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        raise AssertionError(f"nothing may be spawned once the gate says no, but got {list(cmd)}")


# ------------------------------------------------------------------------- the inputs


def a_clang() -> Toolchain:
    """A clang nobody had to find: fields written out, detect() never called."""
    return Toolchain(
        family="clang",
        compiler=Path(CLANG_PATH),
        version="Apple clang version 21.0.0 (clang-2100.1.1.101)",
        warning_flags=CLANG_WARNING_FLAGS,
    )


def a_linux() -> Platform:
    """Linux's compile flag, exercised from macOS because the platform is an argument."""
    return Platform(name="linux", compile_extras=LINUX_COMPILE_EXTRAS)


def a_working_status() -> CapabilityStatus:
    """A probe that caught its planted bug, with nothing this OS has to caveat."""
    return CapabilityStatus(available=True, verified_by=VERIFIED_BY)


def a_denied_status() -> CapabilityStatus:
    """gcc's refusal, which the probe answered without spawning a compiler."""
    return CapabilityStatus(available=False, reason=GCC_DENIED_REASON)


def statuses(status: CapabilityStatus) -> dict[Analysis, CapabilityStatus]:
    """The same status under every analysis, the sanitizer ones included.

    TSAN and friends are in here on purpose: asking this pipeline for one must fail on the
    check lookup, not on a capability the test forgot to write down.
    """
    return dict.fromkeys(Analysis, status)


def a_source(tmp_path: Path) -> Path:
    source = tmp_path / f"{SOURCE_STEM}.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")
    return source


def build_dir(tmp_path: Path) -> Path:
    return tmp_path / "build"


def install_tidy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put a clang-tidy where only this platform's tool directories can see it.

    PATH is blinded first, so a real clang-tidy on the developer's machine cannot be the one
    the pipeline finds and the recorded argv is this file's own path either way.
    """
    monkeypatch.setattr(shutil, "which", lambda name: None)
    tidy = tmp_path / TIDY_NAME
    tidy.write_text("#!/bin/sh\n", encoding="utf-8")
    # executable, so the stand-in models a tool the pipeline could actually spawn
    tidy.chmod(0o755)
    return tidy


def a_platform_with(tidy_dir: Path) -> Platform:
    return Platform(name="linux", compile_extras=LINUX_COMPILE_EXTRAS, extra_tool_dirs=(tidy_dir,))


def check(
    tmp_path: Path,
    runner: ScriptedRunner | RefusingRunner,
    *,
    analysis: Analysis = Analysis.THREAD_SAFETY,
    platform: Platform | None = None,
    capabilities: Mapping[Analysis, CapabilityStatus] | None = None,
    checks: str | None = None,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Run the pipeline against the fake, with the arguments the tests vary."""
    return check_file(
        a_source(tmp_path),
        analysis,
        toolchain=a_clang(),
        platform=a_linux() if platform is None else platform,
        capabilities=statuses(a_working_status()) if capabilities is None else capabilities,
        checks=checks,
        runner=runner,
    )


def reported(result: AnalysisReport | BuildFailure | CapabilityStatus) -> AnalysisReport:
    assert isinstance(result, AnalysisReport), f"expected an AnalysisReport, got {result}"
    return result


def failed(result: AnalysisReport | BuildFailure | CapabilityStatus) -> BuildFailure:
    assert isinstance(result, BuildFailure), f"expected a BuildFailure, got {result}"
    return result


# ------------------------------------------------------------------ the thread-safety chain


def test_a_compile_becomes_the_finding_its_golden_holds(tmp_path: Path) -> None:
    """Gate, compile, parse: what comes back is what clang actually printed."""
    runner = ScriptedRunner([RunResult(exit_code=0, output=golden(THREAD_SAFETY_GOLDEN))])

    report = reported(check(tmp_path, runner))

    assert report.analysis is Analysis.THREAD_SAFETY
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.tool == "compiler"
    assert finding.category == THREAD_SAFETY_CATEGORY
    assert finding.message == THREAD_SAFETY_MESSAGE
    assert finding.location is not None
    # the golden blames the line the fixture marks as its planted bug
    assert finding.location.line == bug_line(SOURCE_STEM) == THREAD_SAFETY_LINE
    # -Wthread-safety warns without failing the compile, and that fact travels with the report
    assert report.exit_code == 0
    assert report.timed_out is False
    assert report.verified_by == VERIFIED_BY


def test_the_compile_is_syntax_only_and_carries_this_hosts_flags(tmp_path: Path) -> None:
    """Nothing is linked: the warnings are the product, and a snippet with no main() counts."""
    runner = ScriptedRunner([CLEAN])

    check(tmp_path, runner)

    assert runner.checked.cmd == [
        CLANG_PATH,
        "-std=c++20",
        "-fsyntax-only",
        *CLANG_WARNING_FLAGS,
        *LINUX_COMPILE_EXTRAS,
        str(tmp_path / f"{SOURCE_STEM}.cpp"),
    ]
    assert runner.checked.timeout_s == CHECK_TIMEOUT_S


def test_the_check_replaces_the_shells_sanitizer_options_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hygiene means dropping the four, not starting from an empty world.

    All four are poisoned in this process first, so this proves the pipeline stripped them
    rather than that they happened to be absent. The canary is the other half: a compile that
    lost PATH would not find its own headers.
    """
    for name in EVERY_SANITIZER_VAR:
        monkeypatch.setenv(name, POISON)
    monkeypatch.setenv("CPP_ANALYSIS_TEST_CANARY", "still-here")
    runner = ScriptedRunner([CLEAN])

    check(tmp_path, runner)

    env = runner.env_of(runner.checked)
    leaked = [name for name in EVERY_SANITIZER_VAR if name in env]
    assert leaked == [], f"the shell's own options survived into the check: {leaked}"
    assert env.get("CPP_ANALYSIS_TEST_CANARY") == "still-here"


# ------------------------------------------------------------------- what stops the chain


def test_an_unavailable_capability_comes_back_untouched_and_spawns_nothing(
    tmp_path: Path,
) -> None:
    """gcc has no -Wthread-safety, and a silent compile would read as "your locking is fine"."""
    status = a_denied_status()

    result = check(tmp_path, RefusingRunner(), capabilities=statuses(status))

    # the same object, so the reason and suggestion cannot be reworded on the way through
    assert result is status


def test_a_sanitizer_analysis_is_a_caller_bug(tmp_path: Path) -> None:
    """TSan needs a binary built and run; there is no compile-time check to do instead."""
    with pytest.raises(KeyError):
        check(tmp_path, RefusingRunner(), analysis=Analysis.TSAN)


def test_a_tidy_that_vanished_since_the_probe_refuses_rather_than_reporting_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate passed on a cached answer, and the binary is not there any more.

    An uninstall between the probe and the call would otherwise reach the runner with an
    unspawnable command; refusing in the probe's own words keeps the caller told.
    """
    monkeypatch.setattr(shutil, "which", lambda name: None)
    nowhere = Platform(
        name="linux", install_hints={Analysis.CLANG_TIDY: "sudo apt install clang-tidy"}
    )

    result = check(tmp_path, RefusingRunner(), analysis=Analysis.CLANG_TIDY, platform=nowhere)

    assert isinstance(result, CapabilityStatus), f"a missing tidy still ran: {result}"
    assert result.available is False
    assert result.reason is not None
    assert TIDY_NAME in result.reason
    assert result.suggestion == "sudo apt install clang-tidy"


# ---------------------------------------------------------------------- the clang-tidy chain


def test_a_tidy_run_becomes_the_finding_its_golden_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tidy path reads tidy's own output: the bracket is a check name, not a flag."""
    tidy = install_tidy(monkeypatch, tmp_path)
    runner = ScriptedRunner([RunResult(exit_code=0, output=golden(CLANG_TIDY_GOLDEN))])

    report = reported(
        check(
            tmp_path,
            runner,
            analysis=Analysis.CLANG_TIDY,
            platform=a_platform_with(tidy.parent),
            checks=EXPLICIT_CHECKS,
        )
    )

    assert report.analysis is Analysis.CLANG_TIDY
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.tool == "clang-tidy"
    assert finding.category == TIDY_CATEGORY
    assert finding.message == TIDY_MESSAGE
    assert finding.location is not None
    assert finding.location.line == TIDY_LINE == bug_line("nullptr_zero")
    assert report.exit_code == 0


def test_the_asked_for_checks_reach_tidy_before_the_file_and_the_compiler_flags_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--checks is tidy's own argument; everything past -- is the compilation of the file."""
    tidy = install_tidy(monkeypatch, tmp_path)
    runner = ScriptedRunner([CLEAN])

    check(
        tmp_path,
        runner,
        analysis=Analysis.CLANG_TIDY,
        platform=a_platform_with(tidy.parent),
        checks=EXPLICIT_CHECKS,
    )

    cmd = runner.checked.cmd
    assert cmd == [
        str(tidy),
        f"--checks={EXPLICIT_CHECKS}",
        str(tmp_path / f"{SOURCE_STEM}.cpp"),
        "--",
        "-std=c++20",
        *LINUX_COMPILE_EXTRAS,
    ]
    assert cmd.index(f"--checks={EXPLICIT_CHECKS}") < cmd.index(
        str(tmp_path / "unguarded_write.cpp")
    )
    assert cmd.index("--") < cmd.index("-std=c++20")


def test_no_checks_asked_for_leaves_tidy_on_its_own_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --checks= of any shape overrules a project's committed .clang-tidy file."""
    tidy = install_tidy(monkeypatch, tmp_path)
    runner = ScriptedRunner([CLEAN])

    check(
        tmp_path,
        runner,
        analysis=Analysis.CLANG_TIDY,
        platform=a_platform_with(tidy.parent),
        checks=None,
    )

    passed = [arg for arg in runner.checked.cmd if arg.startswith("--checks")]
    assert passed == [], f"tidy was told what to check when nobody asked: {passed}"


def test_code_that_does_not_compile_comes_back_as_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tidy exits 1 on a compile error and files it under a check, so it stays structured."""
    tidy = install_tidy(monkeypatch, tmp_path)
    runner = ScriptedRunner([BROKEN_CODE])

    report = reported(
        check(
            tmp_path,
            runner,
            analysis=Analysis.CLANG_TIDY,
            platform=a_platform_with(tidy.parent),
        )
    )

    assert [finding.category for finding in report.findings] == [
        "clang-diagnostic-error",
        "clang-diagnostic-error",
    ]
    assert report.exit_code == 1


def test_a_failure_with_nothing_to_parse_is_carried_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver died before it checked anything, and its own words are the only explanation."""
    tidy = install_tidy(monkeypatch, tmp_path)
    runner = ScriptedRunner([DRIVER_FAILED])

    failure = failed(
        check(
            tmp_path,
            runner,
            analysis=Analysis.CLANG_TIDY,
            platform=a_platform_with(tidy.parent),
        )
    )

    assert failure.stage == "clang-tidy"
    assert failure.output == DRIVER_FAILED.output
    assert failure.timed_out is False


# ------------------------------------------------------------------------------ timeouts


def test_a_compile_killed_at_its_timeout_names_the_thread_safety_stage(tmp_path: Path) -> None:
    runner = ScriptedRunner([timed_out()])

    failure = failed(check(tmp_path, runner))

    assert failure.stage == "thread-safety"
    assert failure.timed_out is True


def test_a_tidy_run_killed_at_its_timeout_names_the_clang_tidy_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tidy = install_tidy(monkeypatch, tmp_path)
    runner = ScriptedRunner([timed_out()])

    failure = failed(
        check(
            tmp_path,
            runner,
            analysis=Analysis.CLANG_TIDY,
            platform=a_platform_with(tidy.parent),
        )
    )

    assert failure.stage == "clang-tidy"
    assert failure.timed_out is True


# ------------------------------------------------------------------------------ snippets


def test_a_snippet_is_written_down_before_it_is_checked(tmp_path: Path) -> None:
    """The file has to survive the call: every finding names it."""
    text = "int main() { return 0; }\n"
    runner = ScriptedRunner([RunResult(exit_code=0, output=golden(THREAD_SAFETY_GOLDEN))])

    report = reported(
        check_snippet(
            text,
            Analysis.THREAD_SAFETY,
            toolchain=a_clang(),
            platform=a_linux(),
            capabilities=statuses(a_working_status()),
            build_dir=build_dir(tmp_path),
            runner=runner,
        )
    )

    snippet = build_dir(tmp_path) / "snippet.cpp"
    assert snippet.read_text(encoding="utf-8") == text
    assert str(snippet) in runner.checked.cmd
    assert [finding.category for finding in report.findings] == [THREAD_SAFETY_CATEGORY]
