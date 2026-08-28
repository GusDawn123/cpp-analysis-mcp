"""Probe the prober with no compiler anywhere: every runner here is a fake.

A capability counts as available only when a planted bug came back reported, so most of
what these tests script is a probe that compiles, runs, and then says nothing -- the false
all-clear the whole module exists to prevent. The platform and toolchain tables are built
by hand from the real ones, the same way tests/unit/platforms/test_platforms.py does, so
Linux's link errors and macOS's arm64 denial are both exercised from one machine.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp import capabilities, profiler
from cpp_analysis_mcp.capabilities import (
    PROBE_STEM,
    discover_toolchains,
    fingerprint,
    probe_all,
)
from cpp_analysis_mcp.platforms import darwin, linux
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store.models import SANITIZER_FOR, Analysis
from cpp_analysis_mcp.toolchains import clang, gcc
from cpp_analysis_mcp.toolchains.base import Toolchain

# vm.mmap_rnd_bits as Ubuntu 24.04 ships it, the value linux.py's table was measured at
MEASURED_RND_BITS = "32"

# first lines of --version, copied from the real thing
APPLE_CLANG = "Apple clang version 17.0.0 (clang-1700.0.13.3)\nTarget: arm64-apple-darwin24.6.0\n"
UBUNTU_CLANG = "Ubuntu clang version 18.1.3 (1ubuntu1)\nTarget: x86_64-pc-linux-gnu\n"
UBUNTU_GCC = "g++ (Ubuntu 13.2.0-23ubuntu4) 13.2.0\nCopyright (C) 2023 Free Software Foundation\n"

MISSING_TSAN_RUNTIME = "/usr/bin/ld: cannot find -ltsan: No such file or directory"

SECCOMP_CHECK_FAILED = (
    "FATAL: ThreadSanitizer CHECK failed: "
    "/llvm/lib/tsan/rtl/tsan_platform_linux.cpp:429 "
    '"((personality(old_personality | ADDR_NO_RANDOMIZE))) != ((-1))" (0x0, 0x0)'
)

# what gcc says about the flag it does not have; it quotes the flag name, so a probe that
# reads the marker without checking the exit code would call this a working analysis
UNRECOGNIZED_WARNING_FLAG = "cc1plus: error: unrecognized command-line option '-Wthread-safety'"

TIMED_OUT = RunResult(exit_code=None, output="\n[killed after 30s timeout]\n")

# what each detector prints when it catches the bug the probe planted for it
CAUGHT = {
    Analysis.TSAN: "WARNING: ThreadSanitizer: data race (pid=1234)",
    Analysis.ASAN: "==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000",
    Analysis.LSAN: "==1==ERROR: LeakSanitizer: detected memory leaks",
    Analysis.UBSAN: "probe.cpp:5:11: runtime error: signed integer overflow",
    Analysis.THREAD_SAFETY: (
        "probe.cpp:14:5: warning: writing variable 'guarded' requires holding mutex 'm' "
        "exclusively [-Wthread-safety-analysis]"
    ),
    Analysis.CLANG_TIDY: "probe.cpp:2:14: warning: use nullptr [modernize-use-nullptr]",
    # perf's own table, delimited the way the report command asks for it. The planted hot
    # function has to be named in it: that is the whole detection.
    Analysis.PROFILE: (
        "# Samples: 640  of event 'cpu/cycles/P'\n"
        "# Children;    Self;Symbol;Source:Line\n"
        " 99.06% ; 99.06% ;[.] probe_hot_spot();probe_profile.cpp:4;-      -\n"
        " 0.49%  ; 0.49%  ;[.] probe_cold_spot();probe_profile.cpp:10;-      -\n"
    ),
}

# a detector that reports is not obliged to exit 0: TSan exits 66 under the pinned options
DETECTION_EXIT = {
    Analysis.TSAN: 66,
    Analysis.ASAN: 1,
    Analysis.LSAN: 23,
    Analysis.UBSAN: 0,
    Analysis.THREAD_SAFETY: 0,
    Analysis.CLANG_TIDY: 0,
    # nothing went wrong: a profile that ran is a profile that succeeded
    Analysis.PROFILE: 0,
}

Reply = Callable[[list[str]], RunResult]


# ------------------------------------------------------------------------------- the fakes


@dataclass
class FakeRunner:
    """Answer scripted RunResults and record every command, so nothing has to be compiled."""

    reply: Reply
    calls: list[list[str]] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        recorded = list(cmd)
        self.calls.append(recorded)
        return self.reply(recorded)

    def commands_for(self, analysis: Analysis) -> list[list[str]]:
        return [cmd for cmd in self.calls if probe_analysis(cmd) is analysis]


def probe_analysis(cmd: Sequence[str]) -> Analysis | None:
    """Read which probe a command belongs to off the scratch file it names.

    perf is asked for by name rather than by the file it works on: its report step names
    only the trace and its own flags, none of which carry the probe's stem, and perf is
    reached for by exactly one analysis.
    """
    if cmd and Path(cmd[0]).name == profiler.PERF:
        return Analysis.PROFILE
    for arg in cmd:
        stem = Path(arg).name.removesuffix(".cpp")
        if stem.startswith(PROBE_STEM):
            return Analysis(stem.removeprefix(PROBE_STEM))
    return None


def is_run(cmd: Sequence[str]) -> bool:
    """A sanitizer probe runs its binary as a bare one-element command."""
    return len(cmd) == 1


def is_detection(cmd: Sequence[str], analysis: Analysis) -> bool:
    """Say whether this is the step whose output has to carry the marker.

    Profiling is the one probe with three steps rather than two, and the marker belongs to
    the last of them: recording produces a binary trace that says nothing readable, so only
    the report can name the function that was planted.
    """
    if analysis is Analysis.PROFILE:
        return len(cmd) > 1 and cmd[1] == "report"
    return is_run(cmd) or analysis not in SANITIZER_FOR


def catches_every_planted_bug(cmd: list[str]) -> RunResult:
    """The happy path: everything builds and every detector reports what was planted for it."""
    analysis = probe_analysis(cmd)
    if analysis is None or not is_detection(cmd, analysis):
        return RunResult(exit_code=0, output="")
    return RunResult(exit_code=DETECTION_EXIT[analysis], output=CAUGHT[analysis])


def run_returns(analysis: Analysis, result: RunResult) -> Reply:
    """Script one probe's run step; every other probe stays on the happy path."""

    def reply(cmd: list[str]) -> RunResult:
        if probe_analysis(cmd) is analysis and is_run(cmd):
            return result
        return catches_every_planted_bug(cmd)

    return reply


def compile_returns(analysis: Analysis, result: RunResult) -> Reply:
    """Script one probe's compile step; every other probe stays on the happy path."""

    def reply(cmd: list[str]) -> RunResult:
        if probe_analysis(cmd) is analysis and not is_run(cmd):
            return result
        return catches_every_planted_bug(cmd)

    return reply


def version_replies(versions: Mapping[str, RunResult]) -> Reply:
    """Answer `<compiler> --version` per compiler name."""

    def reply(cmd: list[str]) -> RunResult:
        return versions[Path(cmd[0]).name]

    return reply


def a_clang() -> Toolchain:
    return clang.toolchain(Path("/usr/bin/clang++"), "Apple clang version 17.0.0")


def a_gcc() -> Toolchain:
    return gcc.toolchain(Path("/usr/bin/g++"), "g++ (Debian 13.2.0-25) 13.2.0")


def a_darwin() -> Platform:
    """Build macOS's arm64 tables without asking what machine this is."""
    return Platform(
        name="darwin",
        denied={Analysis.LSAN: darwin.NO_LEAK_SANITIZER, Analysis.PROFILE: darwin.NO_PROFILER},
        limitations={Analysis.TSAN: darwin.TSAN_LIMITATIONS},
        install_hints=darwin.INSTALL_HINTS,
    )


def a_linux(rnd_bits: str = MEASURED_RND_BITS) -> Platform:
    """Build Linux's tables without reading any host, at a known mmap_rnd_bits."""
    return Platform(
        name="linux",
        compile_extras=linux.COMPILE_EXTRAS,
        failure_signatures=linux.failure_signatures(rnd_bits),
        install_hints=linux.INSTALL_HINTS,
        env_facts={linux.MMAP_RND_BITS_FACT: rnd_bits},
    )


# ------------------------------------------------------------------- a caught bug, or nothing


def test_a_capability_is_available_because_a_planted_bug_was_caught() -> None:
    runner = FakeRunner(catches_every_planted_bug)

    statuses = probe_all(a_clang(), a_darwin(), runner=runner)
    tsan = statuses[Analysis.TSAN]

    assert tsan.available
    assert tsan.reason is None
    assert tsan.verified_by
    # macOS runs TSan but sees no deadlocks, and an available status has to say so
    assert any("deadlock" in note for note in tsan.limitations)


def test_every_analysis_comes_back_with_a_status() -> None:
    statuses = probe_all(a_clang(), a_darwin(), runner=FakeRunner(catches_every_planted_bug))

    assert set(statuses) == set(Analysis)


def test_a_sanitizer_that_stays_silent_on_the_planted_bug_is_not_available() -> None:
    """The false all-clear: built, ran, exited 0, reported nothing. A runtime that quiet
    calls buggy code clean exactly as it calls clean code clean."""
    runner = FakeRunner(run_returns(Analysis.TSAN, RunResult(exit_code=0, output="")))

    tsan = probe_all(a_clang(), a_darwin(), runner=runner)[Analysis.TSAN]

    assert not tsan.available
    assert tsan.reason is not None
    assert "planted" in tsan.reason
    assert tsan.verified_by is None


def test_a_run_that_hangs_says_which_step_ran_out_of_time() -> None:
    runner = FakeRunner(run_returns(Analysis.ASAN, TIMED_OUT))

    asan = probe_all(a_clang(), a_darwin(), runner=runner)[Analysis.ASAN]

    assert not asan.available
    assert asan.reason is not None
    assert "timed out" in asan.reason
    assert "run" in asan.reason


def test_a_compile_that_hangs_says_so_too() -> None:
    runner = FakeRunner(compile_returns(Analysis.UBSAN, TIMED_OUT))

    ubsan = probe_all(a_clang(), a_darwin(), runner=runner)[Analysis.UBSAN]

    assert not ubsan.available
    assert ubsan.reason is not None
    assert "timed out" in ubsan.reason
    assert "compile" in ubsan.reason


def test_a_missing_runtime_library_is_reported_as_the_package_to_install() -> None:
    runner = FakeRunner(
        compile_returns(Analysis.TSAN, RunResult(exit_code=1, output=MISSING_TSAN_RUNTIME))
    )

    tsan = probe_all(a_clang(), a_linux(), runner=runner)[Analysis.TSAN]

    assert not tsan.available
    assert tsan.suggestion is not None
    assert "libtsan0" in tsan.suggestion
    # and a binary that failed to link was never run
    assert len(runner.commands_for(Analysis.TSAN)) == 1
    assert not any(is_run(cmd) for cmd in runner.commands_for(Analysis.TSAN))


def test_a_sandbox_crash_is_read_as_a_sandbox_problem() -> None:
    runner = FakeRunner(
        run_returns(Analysis.TSAN, RunResult(exit_code=1, output=SECCOMP_CHECK_FAILED))
    )

    tsan = probe_all(a_clang(), a_linux(), runner=runner)[Analysis.TSAN]

    assert not tsan.available
    assert tsan.reason is not None
    assert "seccomp" in tsan.reason
    assert tsan.suggestion is not None
    assert "seccomp=unconfined" in tsan.suggestion


def test_an_unrecognized_crash_still_quotes_what_the_tool_said() -> None:
    """No signature matched, so the honest answer is the tool's own first lines."""
    crash = "Segmentation fault (core dumped)\n"
    runner = FakeRunner(run_returns(Analysis.ASAN, RunResult(exit_code=-11, output=crash)))

    asan = probe_all(a_clang(), a_linux(), runner=runner)[Analysis.ASAN]

    assert not asan.available
    assert asan.reason is not None
    assert "Segmentation fault" in asan.reason


def test_a_compile_error_is_never_read_as_a_detection() -> None:
    """The compile-time check finds its own flag name quoted in the error that rejects it,
    so a marker in output the compiler failed on must not count as the analysis working."""
    runner = FakeRunner(
        compile_returns(
            Analysis.THREAD_SAFETY, RunResult(exit_code=1, output=UNRECOGNIZED_WARNING_FLAG)
        )
    )

    status = probe_all(a_clang(), a_linux(), runner=runner)[Analysis.THREAD_SAFETY]

    assert not status.available
    assert status.reason is not None


def test_nothing_is_ever_unavailable_without_saying_why() -> None:
    runner = FakeRunner(lambda cmd: RunResult(exit_code=1, output=""))

    statuses = probe_all(a_clang(), a_linux(), runner=runner)

    assert [analysis for analysis, status in statuses.items() if status.available] == []
    for analysis, status in statuses.items():
        assert status.reason, f"{analysis} went silent"


# ------------------------------------------------------------ answered without spawning anything


def test_gcc_is_told_no_on_thread_safety_without_running_a_compiler() -> None:
    """gcc has no -Wthread-safety at all, so probing for it would only waste a compile."""
    runner = FakeRunner(catches_every_planted_bug)

    status = probe_all(a_gcc(), a_linux(), runner=runner)[Analysis.THREAD_SAFETY]

    assert not status.available
    assert status.reason == gcc.NO_THREAD_SAFETY
    assert runner.commands_for(Analysis.THREAD_SAFETY) == []
    assert not any("-fsyntax-only" in arg for cmd in runner.calls for arg in cmd)


def test_macos_arm64_is_told_no_on_leaks_without_running_anything() -> None:
    runner = FakeRunner(catches_every_planted_bug)

    lsan = probe_all(a_clang(), a_darwin(), runner=runner)[Analysis.LSAN]

    assert not lsan.available
    assert lsan.reason == darwin.NO_LEAK_SANITIZER.reason
    assert lsan.suggestion == darwin.NO_LEAK_SANITIZER.suggestion
    assert runner.commands_for(Analysis.LSAN) == []
    assert not any("-fsanitize=leak" in arg for cmd in runner.calls for arg in cmd)


def test_a_clang_tidy_that_is_not_installed_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    # no brew llvm directory either, so there is nowhere left to look
    darwin_like = Platform(name="darwin", install_hints=darwin.INSTALL_HINTS)
    runner = FakeRunner(catches_every_planted_bug)

    status = probe_all(a_clang(), darwin_like, runner=runner)[Analysis.CLANG_TIDY]

    assert not status.available
    assert status.reason is not None
    assert "clang-tidy" in status.reason
    assert status.suggestion == "brew install llvm"
    assert runner.commands_for(Analysis.CLANG_TIDY) == []


def test_clang_tidy_is_looked_for_where_brew_leaves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """brew keeps llvm off PATH, so a which-miss is not the end of the search."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    tidy = tmp_path / "clang-tidy"
    tidy.write_text("#!/bin/sh\n", encoding="utf-8")
    darwin_like = Platform(name="darwin", extra_tool_dirs=(tmp_path,))
    runner = FakeRunner(catches_every_planted_bug)

    status = probe_all(a_clang(), darwin_like, runner=runner)[Analysis.CLANG_TIDY]

    assert status.available
    assert runner.commands_for(Analysis.CLANG_TIDY)[0][0] == str(tidy)


# ------------------------------------------------------------------------------------ cache


def test_a_second_probe_reads_the_cache_instead_of_recompiling(tmp_path: Path) -> None:
    runner = FakeRunner(catches_every_planted_bug)
    toolchain, host = a_clang(), a_darwin()

    first = probe_all(toolchain, host, cache_dir=tmp_path, runner=runner)
    spawned = len(runner.calls)
    second = probe_all(toolchain, host, cache_dir=tmp_path, runner=runner)

    assert spawned > 0
    assert len(runner.calls) == spawned
    assert second == first


def test_a_changed_host_fact_invalidates_the_cache(tmp_path: Path) -> None:
    """The ASLR width decides whether TSan can start, so a result read under one value
    says nothing about the other."""
    runner = FakeRunner(catches_every_planted_bug)

    probe_all(a_clang(), a_linux("28"), cache_dir=tmp_path, runner=runner)
    spawned = len(runner.calls)
    probe_all(a_clang(), a_linux("32"), cache_dir=tmp_path, runner=runner)

    assert len(runner.calls) > spawned


@pytest.mark.parametrize(
    "damage",
    (
        "not json",
        '{"tsan": {"available": true}}',
        '{"gremlin": {"available": true, "reason": null, "suggestion": null, '
        '"verified_by": null, "limitations": []}}',
    ),
)
def test_a_damaged_cache_file_is_reprobed_and_rewritten(tmp_path: Path, damage: str) -> None:
    runner = FakeRunner(catches_every_planted_bug)
    toolchain, host = a_clang(), a_darwin()
    probe_all(toolchain, host, cache_dir=tmp_path, runner=runner)
    cached = next(iter(tmp_path.glob("*.json")))
    cached.write_text(damage, encoding="utf-8")
    spawned = len(runner.calls)

    statuses = probe_all(toolchain, host, cache_dir=tmp_path, runner=runner)

    assert len(runner.calls) > spawned
    assert statuses[Analysis.TSAN].available
    assert set(json.loads(cached.read_text(encoding="utf-8"))) == {a.value for a in Analysis}


def test_no_cache_directory_means_probe_every_time() -> None:
    runner = FakeRunner(catches_every_planted_bug)
    toolchain, host = a_clang(), a_darwin()

    probe_all(toolchain, host, cache_dir=None, runner=runner)
    spawned = len(runner.calls)
    probe_all(toolchain, host, cache_dir=None, runner=runner)

    assert len(runner.calls) == 2 * spawned


def test_a_cache_that_cannot_be_written_still_answers_every_probe(tmp_path: Path) -> None:
    """A home directory nobody can write to is ordinary -- containers, ProtectHome, a full
    disk -- and by the time the write is tried every probe has already succeeded. Refusing to
    start there would deny a machine every analysis it can actually do, over a shortcut."""
    # a cache directory whose parent is a regular file: nothing can be created under it
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")
    runner = FakeRunner(catches_every_planted_bug)

    statuses = probe_all(a_clang(), a_darwin(), cache_dir=blocked / "cache", runner=runner)

    assert set(statuses) == set(Analysis)
    assert statuses[Analysis.TSAN].available


def test_the_cache_directory_is_created_on_demand(tmp_path: Path) -> None:
    nested = tmp_path / "cache" / "capabilities"
    runner = FakeRunner(catches_every_planted_bug)

    probe_all(a_clang(), a_darwin(), cache_dir=nested, runner=runner)

    assert list(nested.glob("*.json"))


# ------------------------------------------------------------------------------ fingerprint


def test_the_same_machine_fingerprints_the_same_way() -> None:
    assert fingerprint(a_clang(), a_darwin()) == fingerprint(a_clang(), a_darwin())


def test_a_different_host_fact_fingerprints_differently() -> None:
    assert fingerprint(a_clang(), a_linux("28")) != fingerprint(a_clang(), a_linux("32"))


def test_a_different_compiler_version_fingerprints_differently() -> None:
    older = clang.toolchain(Path("/usr/bin/clang++"), "Apple clang version 16.0.0")

    assert fingerprint(older, a_darwin()) != fingerprint(a_clang(), a_darwin())


def test_a_different_compiler_path_fingerprints_differently() -> None:
    brewed = clang.toolchain(Path("/opt/homebrew/opt/llvm/bin/clang++"), "clang version 18.1.8")
    system = clang.toolchain(Path("/usr/bin/clang++"), "clang version 18.1.8")

    assert fingerprint(brewed, a_darwin()) != fingerprint(system, a_darwin())


def test_a_new_probe_schema_invalidates_every_cached_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probes that mean something new must not be answered from what the old ones found."""
    before = fingerprint(a_clang(), a_darwin())
    monkeypatch.setattr(capabilities, "PROBE_SCHEMA_VERSION", capabilities.PROBE_SCHEMA_VERSION + 1)

    assert fingerprint(a_clang(), a_darwin()) != before


# ------------------------------------------------------------------------------- discovery


def test_apples_g_plus_plus_is_recognised_as_the_clang_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/usr/bin/g++ on macOS is a clang driver; trusting the name would mean reporting
    -Wthread-safety as unsupported on a compiler that has it."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(lambda cmd: RunResult(exit_code=0, output=APPLE_CLANG))

    found = discover_toolchains(runner=runner)

    assert len(found) == 1
    assert found[0].family == "clang"
    assert found[0].version == APPLE_CLANG.splitlines()[0]
    assert found[0].warning_flags == clang.WARNING_FLAGS


def test_two_real_compilers_are_both_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        version_replies(
            {
                "clang++": RunResult(exit_code=0, output=UBUNTU_CLANG),
                "g++": RunResult(exit_code=0, output=UBUNTU_GCC),
            }
        )
    )

    found = discover_toolchains(runner=runner)

    assert [chain.family for chain in found] == ["clang", "gcc"]
    assert found[1].unsupported_reason(Analysis.THREAD_SAFETY) is not None
    # through Path: the fake which answers with forward slashes, discovery stores a Path,
    # and on Windows that Path prints with the separator flipped
    assert [chain.compiler for chain in found] == [Path("/usr/bin/clang++"), Path("/usr/bin/g++")]


def test_a_compiler_that_is_not_installed_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "g++" else f"/usr/bin/{name}")
    runner = FakeRunner(lambda cmd: RunResult(exit_code=0, output=UBUNTU_CLANG))

    found = discover_toolchains(runner=runner)

    assert [chain.family for chain in found] == ["clang"]
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "answer",
    (
        RunResult(exit_code=None, output="[killed after 10s timeout]"),
        RunResult(exit_code=127, output="not found"),
    ),
)
def test_a_compiler_that_will_not_answer_is_skipped(
    monkeypatch: pytest.MonkeyPatch, answer: RunResult
) -> None:
    """A hung or broken compiler is not a toolchain; guessing its version would be a lie."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = FakeRunner(
        version_replies({"clang++": answer, "g++": RunResult(exit_code=0, output=UBUNTU_GCC)})
    )

    found = discover_toolchains(runner=runner)

    assert [chain.family for chain in found] == ["gcc"]
