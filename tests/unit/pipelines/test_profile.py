"""Drive the profile chain with no compiler, no perf and no child process anywhere.

Only the subprocess boundary is faked; capability gating, build composition, and parsing
are real code. What's pinned is mostly what the chain must never do: sanitize the build,
build at -O1, drop a recording because its run failed, or rank with no sample count behind it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cpp_analysis_mcp.pipelines.profile import OUTSIDE_NOTE, profile_file, profile_project
from cpp_analysis_mcp.platforms.base import Denial, Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store.models import (
    Analysis,
    BuildFailure,
    CapabilityStatus,
    Hotspot,
    Location,
    ProfileReport,
)
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


# --------------------------------------------------------------- where a hotspot's line is

# real evidence, reassembled from a field run: perf placed a 3.75% sample from the user's
# own AddOrder inside the libstdc++ header the optimizer inlined there, .. segments and all
FIELD_FUNCTION = "OrderBook::AddOrder(std::shared_ptr<Order> const&)"
LIBRARY_HEADER = "/usr/lib/gcc/x86_64-linux-gnu/15/../../../../include/c++/15/bits/stl_function.h"
FIELD_REPORT = (
    "# Samples: 4108  of event 'cpu/cycles/P'\n"
    "#\n"
    "# Children;    Self;Symbol           ;Source:Line          ;IPC   [IPC Coverage]\n"
    f" 100.00%; 3.75%  ;[.] {FIELD_FUNCTION} ;{LIBRARY_HEADER}:398 ;-      -\n"
)

# the source REPORT's own rows were compiled from, so a row placed beside it reads as inside
NATIVE_SOURCE = Path("/work/bench.cpp")

# a Windows-spelled project, so the bridge's respelling is exercised on every host rather
# than only when these tests happen to run on Windows
BRIDGED_PROJECT = Path("C:/work/orderbook")
BRIDGED_ROOT = "/mnt/c/work/orderbook"

BENCH_TARGET = "orderbook_bench"

# how the parser opens its note for a frame the optimizer folded away; this pipeline's note
# has to arrive after one like it without disturbing it
INLINED_PREFIX = "inlined into its caller"


def a_report(source_line: str) -> str:
    """The field row placed at one source line, under a cumulative share the parser leaves
    unremarked -- so any note on the result came from this pipeline."""
    return (
        "# Samples: 4108  of event 'cpu/cycles/P'\n"
        "#\n"
        "# Children;    Self;Symbol           ;Source:Line          ;IPC   [IPC Coverage]\n"
        f" 90.00% ; 85.00% ;[.] {FIELD_FUNCTION} ;{source_line} ;-      -\n"
    )


def hotspots_of(report: str, source: Path, tmp_path: Path) -> tuple[Hotspot, ...]:
    """Profile `source` against a scripted perf report and hand back the ranking."""
    runner = FakeRunner({"report": RunResult(exit_code=0, output=report)})
    answer = profile_file(
        source,
        toolchain=a_clang(),
        platform=a_linux(),
        capabilities=available(),
        build_dir=tmp_path / "build",
        runner=runner,
    )
    assert isinstance(answer, ProfileReport)
    return answer.hotspots


def a_cmake_reply(build_dir: Path, target: str) -> None:
    """Leave behind the File API reply a real configure would have written.

    build_project reads the reply off disk after its configure returns, so planting it up
    front is all a faked configure needs. The layout is cmake's own contract.
    """
    reply = build_dir / ".cmake" / "api" / "v1" / "reply"
    reply.mkdir(parents=True, exist_ok=True)
    written = {
        "target.json": {"name": target, "type": "EXECUTABLE", "artifacts": [{"path": target}]},
        "codemodel-v2-stamp.json": {
            "configurations": [
                {"name": "", "targets": [{"name": target, "jsonFile": "target.json"}]}
            ]
        },
        "index-stamp.json": {"reply": {"codemodel-v2": {"jsonFile": "codemodel-v2-stamp.json"}}},
    }
    for name, document in written.items():
        (reply / name).write_text(json.dumps(document), encoding="utf-8")


def test_a_line_inside_the_project_is_reported_as_it_stands(tmp_path: Path) -> None:
    """The ordinary case, and the one the note must never reach: there is nothing to say
    about a hotspot whose line is in the code that was profiled."""
    spots = hotspots_of(a_report("/work/bench.cpp:6"), NATIVE_SOURCE, tmp_path)

    assert spots[0].note is None


def test_a_line_in_library_code_says_the_function_is_what_to_act_on(tmp_path: Path) -> None:
    """The field bug. The header is where perf put the sample; it is not where the user's
    hotspot lives, and nobody can act on a line in stl_function.h."""
    spots = hotspots_of(FIELD_REPORT, NATIVE_SOURCE, tmp_path)

    assert spots[0].function == FIELD_FUNCTION
    assert spots[0].note == OUTSIDE_NOTE
    # labelled, never dropped: the location is evidence, just not an address to go open
    assert spots[0].location == Location(file=LIBRARY_HEADER, line=398)


def test_the_bridges_spelling_of_the_project_is_the_projects_own_code(tmp_path: Path) -> None:
    """The profiler runs through WSL, so a Windows project's own files come back spelled
    /mnt/c/... . Matched against the Windows root as it stands, every one reads as foreign."""
    placed = a_report(f"{BRIDGED_ROOT}/src/book.cpp:88")

    spots = hotspots_of(placed, BRIDGED_PROJECT / "bench.cpp", tmp_path)

    assert spots[0].note is None


def test_a_note_the_parser_already_wrote_survives_the_one_added_here(tmp_path: Path) -> None:
    """Two true things about one row. The pipeline appends to what the parser observed
    rather than replacing it, joined the way the parser joins its own."""
    inlined = FIELD_REPORT.replace("const&) ;", "const&) (inlined) ;")

    spots = hotspots_of(inlined, NATIVE_SOURCE, tmp_path)

    assert spots[0].note is not None
    assert spots[0].note.startswith(INLINED_PREFIX)
    assert spots[0].note.endswith(f"; {OUTSIDE_NOTE}")


def test_a_relative_line_is_the_projects_own_code(tmp_path: Path) -> None:
    """Debug info recorded relative to the build directory places a line without naming any
    root. There is no outside to claim, so nothing is claimed."""
    spots = hotspots_of(a_report("src/book.cpp:88"), NATIVE_SOURCE, tmp_path)

    assert spots[0].note is None


def test_a_relative_project_path_still_places_the_boundary(tmp_path: Path) -> None:
    """An MCP caller may hand the project over as a relative path; the boundary must
    resolve with it rather than silently vanishing along with every note."""
    spots = hotspots_of(FIELD_REPORT, Path("work/bench.cpp"), tmp_path)

    assert spots[0].note == OUTSIDE_NOTE


def test_a_hotspot_perf_could_not_place_is_left_unannotated(tmp_path: Path) -> None:
    """No location is not a location outside the project. A row perf could not place says
    nothing about where its code lives, and the note would be inventing an answer."""
    unplaced = REPORT + " 5.00%  ; 5.00%  ;[.] operator new(unsigned long) ;??:0 ;-      -\n"

    spots = hotspots_of(unplaced, NATIVE_SOURCE, tmp_path)

    unnamed = next(spot for spot in spots if spot.function == "operator new(unsigned long)")
    assert unnamed.location is None
    assert unnamed.note is None


def test_a_project_profile_places_its_hotspots_against_the_project_root(tmp_path: Path) -> None:
    """Both entry points shape through one path, and the root a project profile uses is the
    project it was pointed at -- not the scratch directory it happened to build into."""
    build_dir = tmp_path / "build"
    a_cmake_reply(build_dir, BENCH_TARGET)
    own_row = f" 40.00%; 20.00% ;[.] OrderBook::Match() ;{BRIDGED_ROOT}/src/book.cpp:88 ;-  -\n"
    runner = FakeRunner({"report": RunResult(exit_code=0, output=FIELD_REPORT + own_row)})

    answer = profile_project(
        BRIDGED_PROJECT,
        toolchain=a_clang(),
        platform=a_linux(),
        capabilities=available(),
        build_dir=build_dir,
        target=BENCH_TARGET,
        runner=runner,
    )

    assert isinstance(answer, ProfileReport)
    noted = {spot.function: spot.note for spot in answer.hotspots}
    assert noted["OrderBook::Match()"] is None
    assert noted[FIELD_FUNCTION] == OUTSIDE_NOTE
