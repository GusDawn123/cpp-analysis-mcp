"""Drive the race with no compiler and no child process anywhere.

Only the subprocess boundary and the clock are faked. Validation, the build composition,
the warmup ordering, the round-robin interleave, the answer comparison, and the ranking
math are all the real code.

What these pin is mostly what the race must never do: time a variant before its answer is
checked, keep timing a variant that started answering differently, hand a rejected variant
numbers, or rank anything when the baseline itself cannot hold its own answer steady.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp.pipelines import benchmark
from cpp_analysis_mcp.pipelines.benchmark import race
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store.models import BenchmarkReport, BuildFailure, Variant, VariantResult
from cpp_analysis_mcp.toolchains.base import BENCH_FLAGS, Toolchain

CLANG = "clang++"
ANSWER = "trades=1200 volume=48000\n"

PROGRAM = "int main() { return 0; }\n"


@dataclass
class FakeRace:
    """Answer scripted results per variant and keep a readable log of every step."""

    compile_failures: set[str] = field(default_factory=set)
    # replies popped per run of that variant; empty or missing means a clean default run
    run_replies: dict[str, list[RunResult]] = field(default_factory=dict)
    default_output: str = ANSWER
    calls: list[str] = field(default_factory=list)
    compile_cmds: list[list[str]] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        if Path(cmd[0]).name == CLANG:
            name = Path(next(arg for arg in cmd if arg.endswith(".cpp"))).stem
            self.calls.append(f"compile:{name}")
            self.compile_cmds.append(list(cmd))
            if name in self.compile_failures:
                return RunResult(exit_code=1, output=f"{name}.cpp:1:1: error: boom")
            return RunResult(exit_code=0, output="")
        name = Path(cmd[0]).stem
        self.calls.append(f"run:{name}")
        queue = self.run_replies.get(name)
        if queue:
            return queue.pop(0)
        return RunResult(exit_code=0, output=self.default_output)


@dataclass
class FakeClock:
    """Serve scripted instants; only timed runs consume them, two per run."""

    instants: list[float]

    def __call__(self) -> float:
        return self.instants.pop(0)


def a_clang() -> Toolchain:
    return Toolchain(
        family="clang",
        compiler=Path("/usr/bin/clang++"),
        version="Ubuntu clang version 21.1.8",
        warning_flags=("-Wthread-safety",),
    )


def a_linux() -> Platform:
    return Platform(name="linux", compile_extras=("-pthread",))


def two_variants() -> list[Variant]:
    return [Variant(name="baseline", code=PROGRAM), Variant(name="flat", code=PROGRAM)]


def run_race(
    tmp_path: Path,
    runner: FakeRace,
    *,
    variants: list[Variant] | None = None,
    repeats: int = 2,
    clock: FakeClock | None = None,
    run_timeout_s: int = benchmark.RUN_TIMEOUT_S,
) -> BenchmarkReport | BuildFailure:
    # seconds ticking steadily unless a test scripts its own; far more than any race needs
    ticking = clock if clock is not None else FakeClock([float(i) for i in range(200)])
    return race(
        variants if variants is not None else two_variants(),
        toolchain=a_clang(),
        platform=a_linux(),
        build_dir=tmp_path / "build",
        repeats=repeats,
        run_timeout_s=run_timeout_s,
        runner=runner,
        clock=ticking,
    )


def report_of(outcome: BenchmarkReport | BuildFailure) -> BenchmarkReport:
    assert isinstance(outcome, BenchmarkReport)
    return outcome


def row(report: BenchmarkReport, name: str) -> VariantResult:
    return next(result for result in report.variants if result.name == name)


# ------------------------------------------------------------------------------ the happy race


def test_survivors_are_ranked_fastest_first_with_real_statistics(tmp_path: Path) -> None:
    # baseline runs take 100ms and 120ms; flat takes 50ms and 70ms
    clock = FakeClock([0.0, 0.0, 0.0, 0.0, 0.100, 1.0, 1.050, 2.0, 2.120, 3.0, 3.070])
    report = report_of(run_race(tmp_path, FakeRace(), clock=clock))

    assert [result.name for result in report.variants] == ["flat", "baseline"]
    flat = report.variants[0]
    assert flat.runs == 2
    assert flat.mean_ms == pytest.approx(60.0)
    assert flat.min_ms == pytest.approx(50.0)
    assert flat.stddev_ms == pytest.approx(14.142, abs=0.01)
    assert flat.matches_baseline is True
    assert flat.rejected is None
    assert report.baseline == "baseline"
    assert report.repeats == 2


def test_warmups_run_first_and_timed_rounds_interleave(tmp_path: Path) -> None:
    """Round-robin is the fairness rule: machine drift lands on every variant evenly."""
    runner = FakeRace()
    report_of(run_race(tmp_path, runner))

    assert runner.calls == [
        "compile:baseline",
        "compile:flat",
        "run:baseline",  # the answer-defining warmup
        "run:flat",  # the answer check, before any timing
        "run:baseline",  # round one
        "run:flat",
        "run:baseline",  # round two
        "run:flat",
    ]


def test_the_winning_variant_is_named_with_its_next_step(tmp_path: Path) -> None:
    clock = FakeClock([0.0, 0.0, 0.0, 0.0, 0.100, 1.0, 1.050, 2.0, 2.120, 3.0, 3.070])
    report = report_of(run_race(tmp_path, FakeRace(), clock=clock))

    assert report.next_step is not None
    assert report.next_step.startswith("flat won")
    assert "sanitize_snippet" in report.next_step


def test_a_race_the_baseline_wins_says_so(tmp_path: Path) -> None:
    # baseline takes 10ms per run; flat takes 500ms
    clock = FakeClock([0.0, 0.0, 0.0, 0.0, 0.010, 1.0, 1.500, 2.0, 2.010, 3.0, 3.500])
    report = report_of(run_race(tmp_path, FakeRace(), clock=clock))

    assert report.variants[0].name == "baseline"
    assert report.next_step == benchmark.NO_WINNER


def test_builds_race_at_release_flags(tmp_path: Path) -> None:
    """The race times what a release would ship, not the sanitizers' -O1 debug shape."""
    runner = FakeRace()
    report_of(run_race(tmp_path, runner))

    for cmd in runner.compile_cmds:
        for flag in BENCH_FLAGS:
            assert flag in cmd
        assert "-O1" not in cmd
        assert "-fsanitize=thread" not in cmd


def test_limitations_travel_on_every_report(tmp_path: Path) -> None:
    report = report_of(run_race(tmp_path, FakeRace()))

    assert report.limitations == benchmark.LIMITATIONS


# ------------------------------------------------------------------------ answers over speed


def test_a_variant_whose_answer_differs_is_rejected_before_any_timing(tmp_path: Path) -> None:
    runner = FakeRace(run_replies={"flat": [RunResult(exit_code=0, output="trades=999\n")]})
    report = report_of(run_race(tmp_path, runner))

    rejected = row(report, "flat")
    assert rejected.rejected == benchmark.DIFFERS
    assert rejected.mean_ms is None
    assert rejected.runs == 0
    # one warmup run and not a single timed one
    assert runner.calls.count("run:flat") == 1


def test_a_variant_that_turns_wrong_mid_race_is_dropped_there(tmp_path: Path) -> None:
    runner = FakeRace(
        run_replies={
            "flat": [
                RunResult(exit_code=0, output=ANSWER),  # warmup agrees
                RunResult(exit_code=0, output="trades=999\n"),  # round one lies
            ]
        }
    )
    report = report_of(run_race(tmp_path, runner))

    assert row(report, "flat").rejected == benchmark.DIFFERS
    # warmup, the lying round, and nothing after
    assert runner.calls.count("run:flat") == 2
    survivor = row(report, "baseline")
    assert survivor.rejected is None
    assert survivor.runs == 2


def test_a_crashing_variant_reports_its_exit_code(tmp_path: Path) -> None:
    runner = FakeRace(run_replies={"flat": [RunResult(exit_code=3, output="")]})
    report = report_of(run_race(tmp_path, runner))

    assert row(report, "flat").rejected == "exited with code 3"


def test_a_variant_that_times_out_reports_the_budget_it_blew(tmp_path: Path) -> None:
    runner = FakeRace(run_replies={"flat": [RunResult(exit_code=None, output="")]})
    report = report_of(run_race(tmp_path, runner, run_timeout_s=7))

    assert row(report, "flat").rejected == "timed out after 7s"


# ----------------------------------------------------------------------- baselines gone wrong


def test_a_baseline_that_does_not_build_fails_the_whole_race(tmp_path: Path) -> None:
    outcome = run_race(tmp_path, FakeRace(compile_failures={"baseline"}))

    assert isinstance(outcome, BuildFailure)
    assert "error: boom" in outcome.output


def test_a_variant_that_does_not_build_loses_alone(tmp_path: Path) -> None:
    report = report_of(run_race(tmp_path, FakeRace(compile_failures={"flat"})))

    rejected = row(report, "flat")
    assert rejected.rejected is not None
    assert rejected.rejected.startswith("did not build:")
    assert row(report, "baseline").rejected is None
    assert report.next_step == benchmark.NO_WINNER


def test_a_baseline_that_crashes_strands_the_race(tmp_path: Path) -> None:
    runner = FakeRace(run_replies={"baseline": [RunResult(exit_code=9, output="")]})
    report = report_of(run_race(tmp_path, runner))

    assert row(report, "baseline").rejected == "exited with code 9"
    assert row(report, "flat").rejected == benchmark.STRANDED
    assert report.next_step is None
    # nothing was timed: the only run is the baseline's failed warmup
    assert runner.calls.count("run:flat") == 0


def test_an_unstable_baseline_strands_the_race_with_the_seed_hint(tmp_path: Path) -> None:
    runner = FakeRace(
        run_replies={
            "baseline": [
                RunResult(exit_code=0, output=ANSWER),  # warmup
                RunResult(exit_code=0, output="trades=7\n"),  # first timed run disagrees
            ]
        }
    )
    report = report_of(run_race(tmp_path, runner))

    assert row(report, "baseline").rejected == benchmark.UNSTABLE
    assert row(report, "flat").rejected == benchmark.STRANDED


# ----------------------------------------------------------------------------- refused shapes


def test_too_few_too_many_and_duplicate_variants_are_refused(tmp_path: Path) -> None:
    one = [Variant(name="alone", code=PROGRAM)]
    six = [Variant(name=f"v{i}", code=PROGRAM) for i in range(6)]
    twins = [Variant(name="twin", code=PROGRAM), Variant(name="twin", code=PROGRAM)]

    for bad in (one, six, twins):
        with pytest.raises(ValueError, match="variant"):
            run_race(tmp_path, FakeRace(), variants=bad)


def test_unsafe_names_and_absurd_repeats_are_refused(tmp_path: Path) -> None:
    escapee = [Variant(name="../up", code=PROGRAM), Variant(name="ok", code=PROGRAM)]
    with pytest.raises(ValueError, match="safe file name"):
        run_race(tmp_path, FakeRace(), variants=escapee)

    for repeats in (1, 21):
        with pytest.raises(ValueError, match="repeats"):
            run_race(tmp_path, FakeRace(), repeats=repeats)


# ------------------------------------------------------------------------- the race budget


def test_the_budget_stops_the_race_and_says_so(tmp_path: Path) -> None:
    """Three repeats asked for, time for two: both variants keep their even two runs, the
    ranking stands, and the report admits it stopped early."""
    clock = FakeClock([0.0, 0.0, 0.0, 0.0, 0.100, 1.0, 1.050, 2.0, 2.120, 3.0, 3.070, 11.0])
    runner = FakeRace()
    report = report_of(
        race(
            two_variants(),
            toolchain=a_clang(),
            platform=a_linux(),
            build_dir=tmp_path / "build",
            repeats=3,
            race_timeout_s=10,
            runner=runner,
            clock=clock,
        )
    )

    assert benchmark.STOPPED_EARLY in report.limitations
    assert row(report, "baseline").runs == 2
    assert row(report, "flat").runs == 2
    assert [result.name for result in report.variants] == ["flat", "baseline"]


def test_a_variant_the_budget_left_with_one_run_is_not_ranked(tmp_path: Path) -> None:
    """One measurement has no spread to judge it by, so it is a rejection, not a row that
    looks comparable next to a variant that ran twice."""
    clock = FakeClock([0.0, 0.0, 0.0, 0.0, 0.100, 1.0, 1.050, 2.0, 2.100, 11.0])
    runner = FakeRace()
    report = report_of(
        race(
            two_variants(),
            toolchain=a_clang(),
            platform=a_linux(),
            build_dir=tmp_path / "build",
            repeats=2,
            race_timeout_s=10,
            runner=runner,
            clock=clock,
        )
    )

    starved = row(report, "flat")
    assert starved.rejected == benchmark.OVER_BUDGET
    assert starved.runs == 1
    assert starved.mean_ms is None
    survivor = row(report, "baseline")
    assert survivor.rejected is None
    assert survivor.runs == 2


def test_a_zero_budget_race_refuses_to_rank_anything(tmp_path: Path) -> None:
    clock = FakeClock([float(i) for i in range(20)])
    runner = FakeRace()
    report = report_of(
        race(
            two_variants(),
            toolchain=a_clang(),
            platform=a_linux(),
            build_dir=tmp_path / "build",
            repeats=2,
            race_timeout_s=0,
            runner=runner,
            clock=clock,
        )
    )

    assert row(report, "baseline").rejected == benchmark.OVER_BUDGET
    assert row(report, "flat").rejected == benchmark.OVER_BUDGET
    assert report.next_step is None
    assert benchmark.STOPPED_EARLY in report.limitations


def test_builds_yield_to_the_budget_too(tmp_path: Path) -> None:
    """A variant reached after the deadline is rejected unbuilt; only the baseline's
    compile is guaranteed."""
    runner = FakeRace()
    report = report_of(
        race(
            two_variants(),
            toolchain=a_clang(),
            platform=a_linux(),
            build_dir=tmp_path / "build",
            repeats=2,
            race_timeout_s=0,
            runner=runner,
            clock=FakeClock([float(i) for i in range(20)]),
        )
    )

    assert runner.calls.count("compile:baseline") == 1
    assert runner.calls.count("compile:flat") == 0
    assert row(report, "flat").rejected == benchmark.OVER_BUDGET
