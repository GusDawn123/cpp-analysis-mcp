"""Drive the profile chain with no compiler, no perf and no child process anywhere.

Only the subprocess boundary is faked. The capability gate, the flags the build is composed
with, the two perf invocations and the parsing are all the real code, and the fake answers
the report step with output a real perf once printed.

What these pin is mostly what the chain must never do: build with a sanitizer, build at the
sanitizers' -O1, throw away a recording because the run that produced it failed, or hand back
a ranking with nothing attached that says how many measurements it rests on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cpp_analysis_mcp.models import Analysis, BuildFailure, CapabilityStatus, ProfileReport
from cpp_analysis_mcp.pipelines.profile import profile_file, profile_project
from cpp_analysis_mcp.platforms.base import Denial, Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.toolchains.base import Toolchain

# spelled through Path so the string compares equal to str(Path(...)) on Windows too
CLANG_PATH = str(Path("/usr/bin/clang++"))

SOURCE_STEM = "bench"

# the probe's own words for a working profiler, as capabilities.py phrases them
VERIFIED_BY = "compiled, ran and profiled a planted hot loop; perf reported it"
LIMITATION = "runs inside WSL distro 'Ubuntu'"

DENIED_REASON = "perf reads the Linux kernel's performance counters"

# captured from a real run, trimmed in path length; the parser's own tests cover the shapes
REPORT = """\
# Samples: 286  of event 'cpu/cycles/P'
#
# Children;    Self;Symbol           ;Source:Line          ;IPC   [IPC Coverage]
 188.57%; 89.72% ;[.] Book::AddOrder(int, int)  ;/work/bench.cpp:6   ;-      -
 100.31%; 0.73%  ;[.] main                      ;/work/bench.cpp:8   ;-      -
"""

BUILD_FAILED = "bench.cpp:4:9: error: use of undeclared identifier 'oops'"


@dataclass
class FakeRunner:
    """Answer scripted RunResults and record every command, cwd included."""

    replies: Mapping[str, RunResult]
    calls: list[list[str]] = field(default_factory=list)
    cwds: list[Path | None] = field(default_factory=list)

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
        self.cwds.append(cwd)
        return self.replies.get(step(recorded), RunResult(exit_code=0, output=""))


def step(cmd: Sequence[str]) -> str:
    """Name which of the three steps a command is, the way these tests refer to them."""
    if Path(cmd[0]).name != "perf":
        return "compile"
    return cmd[1]


def a_clang() -> Toolchain:
    return Toolchain(
        family="clang",
        compiler=Path(CLANG_PATH),
        version="Ubuntu clang version 21.1.8",
        warning_flags=("-Wthread-safety",),
    )


def a_linux() -> Platform:
    return Platform(name="linux", compile_extras=("-pthread",))


def available() -> dict[Analysis, CapabilityStatus]:
    return {
        Analysis.PROFILE: CapabilityStatus(
            available=True, verified_by=VERIFIED_BY, limitations=(LIMITATION,)
        )
    }


def denied() -> dict[Analysis, CapabilityStatus]:
    return {Analysis.PROFILE: CapabilityStatus(available=False, reason=DENIED_REASON)}


def a_source(tmp_path: Path) -> Path:
    source = tmp_path / f"{SOURCE_STEM}.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")
    return source


def run_profile(tmp_path: Path, runner: FakeRunner) -> object:
    return profile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        capabilities=available(),
        build_dir=tmp_path / "build",
        runner=runner,
    )


def a_working_profiler() -> FakeRunner:
    return FakeRunner({"report": RunResult(exit_code=0, output=REPORT)})


# ------------------------------------------------------------------------------- the gate


def test_a_machine_that_cannot_profile_is_told_so_and_nothing_is_spawned(tmp_path: Path) -> None:
    """The same hard stop the sanitizers have. Running anyway produces an empty ranking,
    which reads exactly like a program with no hot code."""
    runner = FakeRunner({})

    answer = profile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        capabilities=denied(),
        build_dir=tmp_path / "build",
        runner=runner,
    )

    assert isinstance(answer, CapabilityStatus)
    assert answer.reason == DENIED_REASON
    assert runner.calls == []


def test_a_project_that_cannot_be_profiled_is_refused_before_cmake_is_asked(
    tmp_path: Path,
) -> None:
    """Configuring a project is minutes; the gate is what keeps them from being spent."""
    runner = FakeRunner({})

    answer = profile_project(
        tmp_path,
        toolchain=a_clang(),
        platform=a_linux(),
        capabilities=denied(),
        build_dir=tmp_path / "build",
        runner=runner,
    )

    assert isinstance(answer, CapabilityStatus)
    assert runner.calls == []


def test_a_build_that_failed_comes_back_as_the_failure(tmp_path: Path) -> None:
    """Nothing was sampled, so there is no ranking to report -- and the compiler's own
    words are the answer rather than an empty profile."""
    runner = FakeRunner({"compile": RunResult(exit_code=1, output=BUILD_FAILED)})

    answer = run_profile(tmp_path, runner)

    assert isinstance(answer, BuildFailure)
    assert answer.stage == "compile"
    assert BUILD_FAILED in answer.output
    # and nothing was profiled after the build died
    assert [step(cmd) for cmd in runner.calls] == ["compile"]


# ------------------------------------------------------------------------ what gets built


def test_the_profiled_build_is_optimized_and_carries_no_sanitizer(tmp_path: Path) -> None:
    """The decision the whole pipeline turns on. -O1 would rank call sites the release
    binary does not contain, and instrumentation would change the thing being measured.
    """
    runner = a_working_profiler()

    run_profile(tmp_path, runner)

    compiled = runner.calls[0]
    assert "-O2" in compiled
    assert "-O1" not in compiled
    # frame pointers, because perf walks them to attribute cumulative time
    assert "-fno-omit-frame-pointer" in compiled
    assert "-g" in compiled
    assert not any(arg.startswith("-fsanitize") for arg in compiled)


def test_the_recording_asks_for_a_call_graph_and_writes_beside_the_build(
    tmp_path: Path,
) -> None:
    """Without the call graph there is no cumulative time; without -o the trace lands in
    whatever directory the server happened to be launched from."""
    runner = a_working_profiler()

    run_profile(tmp_path, runner)

    recorded = runner.calls[1]
    assert recorded[0] == "perf"
    assert recorded[1] == "record"
    assert "--call-graph=fp" in recorded
    assert "-F" in recorded
    assert recorded[-1] == str(tmp_path / "build" / SOURCE_STEM)
    # the trace goes in the build directory this call owns, and the run happens there
    assert str(tmp_path / "build" / "perf.data") in recorded
    assert runner.cwds[1] == tmp_path / "build"


def test_the_report_reads_back_the_trace_that_was_just_written(tmp_path: Path) -> None:
    runner = a_working_profiler()

    run_profile(tmp_path, runner)

    reported = runner.calls[2]
    assert reported[:2] == ["perf", "report"]
    assert str(tmp_path / "build" / "perf.data") in reported
    # delimited, or the parser reads column positions that move with the widest symbol
    assert "-t" in reported


# ----------------------------------------------------------------------- what comes back


def test_a_profile_carries_the_ranking_and_what_it_rests_on(tmp_path: Path) -> None:
    """A ranking with no sample count behind it cannot be told from a ranking of noise."""
    answer = run_profile(tmp_path, a_working_profiler())

    assert isinstance(answer, ProfileReport)
    assert answer.analysis is Analysis.PROFILE
    assert answer.samples == 286
    assert answer.event == "cpu/cycles/P"
    assert answer.hotspots[0].function == "Book::AddOrder(int, int)"
    assert answer.hotspots[0].self_pct == 89.72
    assert answer.exit_code == 0
    assert not answer.timed_out


def test_the_probe_that_proved_this_works_travels_with_the_answer(tmp_path: Path) -> None:
    """An empty ranking from a proven profiler and one from a profiler that never sampled
    look identical; verified_by is the only thing that separates them."""
    answer = run_profile(tmp_path, a_working_profiler())

    assert isinstance(answer, ProfileReport)
    assert answer.verified_by == VERIFIED_BY
    assert LIMITATION in answer.limitations


def test_the_threshold_that_hid_rows_is_reported_rather_than_left_silent(
    tmp_path: Path,
) -> None:
    """Rows under a fraction of a percent are dropped at the tool. Unsaid, that reads as
    completeness to anyone acting on the list."""
    answer = run_profile(tmp_path, a_working_profiler())

    assert isinstance(answer, ProfileReport)
    assert any("not listed" in note for note in answer.limitations)


def test_a_workload_killed_at_its_timeout_still_reports_what_it_sampled(
    tmp_path: Path,
) -> None:
    """Every sample taken before the kill is a real measurement. Refusing to read them
    throws away the only profile there is, and says nothing about why."""
    runner = FakeRunner(
        {
            "record": RunResult(exit_code=None, output="[killed after 300s timeout]"),
            "report": RunResult(exit_code=0, output=REPORT),
        }
    )

    answer = run_profile(tmp_path, runner)

    assert isinstance(answer, ProfileReport)
    assert answer.timed_out
    assert answer.samples == 286
    assert answer.hotspots
    # the report step ran regardless of how the recording ended
    assert [step(cmd) for cmd in runner.calls] == ["compile", "record", "report"]


def test_a_profile_that_sampled_nothing_says_zero_rather_than_looking_clean(
    tmp_path: Path,
) -> None:
    """What a report over a truncated trace prints. An empty hotspot list with samples=0 is
    readable as a failed measurement; the same list with no count is not."""
    runner = FakeRunner(
        {
            "record": RunResult(exit_code=1, output="failed to collect"),
            "report": RunResult(exit_code=1, output="failed to open perf.data"),
        }
    )

    answer = run_profile(tmp_path, runner)

    assert isinstance(answer, ProfileReport)
    assert answer.hotspots == ()
    assert answer.samples == 0
    assert answer.event == ""
    assert answer.exit_code == 1


def test_a_denied_platform_never_reaches_the_pipeline_through_its_own_tables(
    tmp_path: Path,
) -> None:
    """The denial lives on the platform and the status is what the gate reads; a platform
    that refuses profiling must not be able to have its refusal skipped by a stale status."""
    platform = Platform(name="darwin", denied={Analysis.PROFILE: Denial(reason=DENIED_REASON)})
    runner = FakeRunner({})

    answer = profile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=platform,
        capabilities=denied(),
        build_dir=tmp_path / "build",
        runner=runner,
    )

    assert isinstance(answer, CapabilityStatus)
    assert runner.calls == []


def test_plain_user_code_reports_no_fingerprints_and_a_coarse_confidence(
    tmp_path: Path,
) -> None:
    """286 samples of the user's own function: nothing to name, nothing to race, and the
    confidence line says the ranking is coarse."""
    result = run_profile(tmp_path, a_working_profiler())

    assert isinstance(result, ProfileReport)
    assert result.fingerprints == ()
    assert result.next_step is None
    assert result.confidence is not None
    assert "coarse" in result.confidence


def test_library_machinery_is_named_with_its_breadcrumb(tmp_path: Path) -> None:
    """The same report with the hot symbol swapped for a std::map tree walk: the pattern
    gets named, the candidates arrive, and next_step points at the race."""
    tree_report = REPORT.replace(
        "Book::AddOrder(int, int)", "std::_Rb_tree<int, int>::find(int const&)"
    )
    runner = FakeRunner({"report": RunResult(exit_code=0, output=tree_report)})
    result = run_profile(tmp_path, runner)

    assert isinstance(result, ProfileReport)
    assert [mark.category for mark in result.fingerprints] == ["map-machinery"]
    assert result.fingerprints[0].candidates
    assert result.next_step is not None
    assert "benchmark_variants" in result.next_step
