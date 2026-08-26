"""Race whole-program variants and reject any whose answer changed -- one benchmark run.

The measurement rules live here instead of in the caller's judgement. Warmups come before
any timing. Timed runs interleave round-robin, so drift in the machine lands on every
variant evenly rather than on whichever ran last. Measurement is serial on purpose:
variants racing concurrently would fight for the same caches and clocks, and the loser of
that fight would look slow for reasons that are not in its code.

The comparison rule is the point of the tool. Every variant runs the same workload, and a
variant only keeps its numbers if its output matched the baseline's on every run. A rewrite
that got faster by answering differently is not faster, it is wrong, and "wrong but quick"
must never survive into a ranking an agent will act on.

Composes primitives only, never another pipeline (rule 1); the Platform and Toolchain
arrive as arguments (rule 3).
"""

from __future__ import annotations

import re
import statistics
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from cpp_analysis_mcp import process
from cpp_analysis_mcp.build import single_file
from cpp_analysis_mcp.models import (
    BenchmarkReport,
    BuildFailure,
    BuiltBinary,
    Variant,
    VariantResult,
)
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner, RunResult
from cpp_analysis_mcp.toolchains.base import BENCH_FLAGS, Toolchain

# benchmarks are meant to be seconds of work; a variant that runs longer than this per
# repeat would make a five-repeat race take ten minutes, which is a workload problem
RUN_TIMEOUT_S = 120

DEFAULT_REPEATS = 5
MIN_REPEATS, MAX_REPEATS = 2, 20
MIN_VARIANTS, MAX_VARIANTS = 2, 5

# variant names become file names, so they are held to what every filesystem accepts
NAME_SHAPE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# how much of a failing variant's output travels inside its rejection reason
SNIPPET_CHARS = 300

DIFFERS = "output differs from the baseline"
UNSTABLE = "output changes between identical runs; give the workload a fixed seed"
STRANDED = "not compared: the baseline failed"

LIMITATIONS = (
    "times include process start and teardown; differences of a few milliseconds are noise",
    "times are this machine's, for this compiler; other machines may rank differently",
)

NO_WINNER = "no variant beat the baseline on this machine"

Clock = Callable[[], float]


def race(
    variants: Sequence[Variant],
    *,
    toolchain: Toolchain,
    platform: Platform,
    build_dir: Path,
    repeats: int = DEFAULT_REPEATS,
    compile_timeout_s: int = single_file.COMPILE_TIMEOUT_S,
    run_timeout_s: int = RUN_TIMEOUT_S,
    runner: Runner = process.run,
    clock: Clock = time.perf_counter,
) -> BenchmarkReport | BuildFailure:
    """Build every variant at release optimization, race them, and rank the survivors.

    The first variant is the baseline: its output defines the right answer, so a baseline
    that does not build is the whole race failing and comes back as that BuildFailure. Any
    other variant that fails to build, crashes, or answers differently is rejected on its
    own and the race continues without it.
    """
    _validate(variants, repeats)
    baseline = variants[0].name

    built, rejected = _build_all(
        variants,
        toolchain=toolchain,
        platform=platform,
        build_dir=build_dir,
        compile_timeout_s=compile_timeout_s,
        runner=runner,
    )
    if isinstance(built, BuildFailure):
        return built

    # The baseline's warmup settles what the right answer is; a baseline that cannot
    # produce one strands the race, because there is nothing to compare anybody against.
    first = _run(built[baseline], run_timeout_s=run_timeout_s, runner=runner)
    refusal = _refusal(first, run_timeout_s)
    if refusal is not None:
        return _stranded(variants, rejected, baseline_reason=refusal, repeats=repeats)
    expected = first.output

    # Everyone else warms up next, and the answer check happens here, before any timing:
    # a variant that already answered differently is not worth paying repeats for.
    racing = [baseline]
    for variant in variants[1:]:
        if variant.name in rejected:
            continue
        outcome = _run(built[variant.name], run_timeout_s=run_timeout_s, runner=runner)
        reason = _refusal(outcome, run_timeout_s) or (
            None if outcome.output == expected else DIFFERS
        )
        if reason is not None:
            rejected[variant.name] = reason
        else:
            racing.append(variant.name)

    timed, unstable = _timed_rounds(
        racing,
        built,
        rejected,
        baseline=baseline,
        expected=expected,
        repeats=repeats,
        run_timeout_s=run_timeout_s,
        runner=runner,
        clock=clock,
    )
    if unstable is not None:
        return _stranded(variants, rejected, baseline_reason=unstable, repeats=repeats)

    return _report(variants, timed, rejected, baseline=baseline, repeats=repeats)


def _validate(variants: Sequence[Variant], repeats: int) -> None:
    """Refuse shapes the race cannot mean anything for, before anything is written."""
    if not MIN_VARIANTS <= len(variants) <= MAX_VARIANTS:
        raise ValueError(f"between {MIN_VARIANTS} and {MAX_VARIANTS} variants, got {len(variants)}")
    if not MIN_REPEATS <= repeats <= MAX_REPEATS:
        raise ValueError(f"repeats must be {MIN_REPEATS} to {MAX_REPEATS}, got {repeats}")
    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise ValueError(f"variant names must be unique, got {names}")
    for name in names:
        if not NAME_SHAPE.match(name):
            raise ValueError(f"variant name {name!r} is not a safe file name")


def _build_all(
    variants: Sequence[Variant],
    *,
    toolchain: Toolchain,
    platform: Platform,
    build_dir: Path,
    compile_timeout_s: int,
    runner: Runner,
) -> tuple[dict[str, BuiltBinary] | BuildFailure, dict[str, str]]:
    """Write and compile every variant; a broken baseline fails the race, others just lose."""
    built: dict[str, BuiltBinary] = {}
    rejected: dict[str, str] = {}
    for index, variant in enumerate(variants):
        source = build_dir / f"{variant.name}.cpp"
        build_dir.mkdir(parents=True, exist_ok=True)
        source.write_text(variant.code, encoding="utf-8")
        result = single_file.compile_file(
            source,
            toolchain=toolchain,
            platform=platform,
            sanitizer=None,
            build_dir=build_dir,
            base_flags=BENCH_FLAGS,
            timeout_s=compile_timeout_s,
            runner=runner,
        )
        if isinstance(result, BuildFailure):
            if index == 0:
                return result, rejected
            rejected[variant.name] = f"did not build: {_snippet(result.output)}"
        else:
            built[variant.name] = result
    return built, rejected


def _timed_rounds(
    racing: list[str],
    built: dict[str, BuiltBinary],
    rejected: dict[str, str],
    *,
    baseline: str,
    expected: str,
    repeats: int,
    run_timeout_s: int,
    runner: Runner,
    clock: Clock,
) -> tuple[dict[str, list[float]], str | None]:
    """Interleave the timed runs; a variant that misbehaves mid-race is dropped there.

    The baseline is held to its own answer too. A baseline that changes output between
    identical runs makes every comparison in the race meaningless, so that one case does
    not reject a variant -- it comes back as the reason to strand the whole report.
    """
    times: dict[str, list[float]] = {name: [] for name in racing}
    for _ in range(repeats):
        for name in racing:
            if name in rejected:
                continue
            started = clock()
            outcome = _run(built[name], run_timeout_s=run_timeout_s, runner=runner)
            elapsed_ms = (clock() - started) * 1000.0
            reason = _refusal(outcome, run_timeout_s) or (
                None if outcome.output == expected else DIFFERS
            )
            if reason is None:
                times[name].append(elapsed_ms)
            elif name == baseline:
                return times, (UNSTABLE if reason == DIFFERS else reason)
            else:
                rejected[name] = reason
    return times, None


def _report(
    variants: Sequence[Variant],
    times: dict[str, list[float]],
    rejected: dict[str, str],
    *,
    baseline: str,
    repeats: int,
) -> BenchmarkReport:
    """Rank the survivors by mean, carry every rejection with its reason, name the winner."""
    survivors = sorted(
        (
            VariantResult(
                name=name,
                runs=len(runs),
                mean_ms=statistics.fmean(runs),
                min_ms=min(runs),
                stddev_ms=statistics.stdev(runs),
                matches_baseline=True,
            )
            for name, runs in times.items()
            if name not in rejected
        ),
        key=lambda result: result.mean_ms or 0.0,
    )
    losers = tuple(
        VariantResult(name=variant.name, runs=0, rejected=rejected[variant.name])
        for variant in variants
        if variant.name in rejected
    )
    fastest = survivors[0].name
    next_step = (
        NO_WINNER
        if fastest == baseline
        else f"{fastest} won; sanitize it (sanitize_snippet: tsan, then asan) before adopting it"
    )
    return BenchmarkReport(
        baseline=baseline,
        variants=(*survivors, *losers),
        repeats=repeats,
        limitations=LIMITATIONS,
        next_step=next_step,
    )


def _stranded(
    variants: Sequence[Variant],
    rejected: dict[str, str],
    *,
    baseline_reason: str,
    repeats: int,
) -> BenchmarkReport:
    """Report a race with no usable baseline: every row rejected, each with its own why."""
    rows: list[VariantResult] = []
    for index, variant in enumerate(variants):
        reason = baseline_reason if index == 0 else rejected.get(variant.name, STRANDED)
        rows.append(VariantResult(name=variant.name, runs=0, rejected=reason))
    return BenchmarkReport(
        baseline=variants[0].name,
        variants=tuple(rows),
        repeats=repeats,
        limitations=LIMITATIONS,
        next_step=None,
    )


def _run(built: BuiltBinary, *, run_timeout_s: int, runner: Runner) -> RunResult:
    """One execution of one variant, in its build directory, under a scrubbed environment."""
    return runner(
        [str(built.path)],
        timeout_s=run_timeout_s,
        env=process.hygienic_env({}),
        cwd=built.build_dir,
    )


def _refusal(outcome: RunResult, run_timeout_s: int) -> str | None:
    """Name why a run cannot be compared, or None for a clean exit."""
    if outcome.timed_out:
        return f"timed out after {run_timeout_s}s"
    if outcome.exit_code != 0:
        return f"exited with code {outcome.exit_code}"
    return None


def _snippet(output: str) -> str:
    """The tail of a compiler's output, where the first real error usually is not -- but
    the last one always is, and a rejection reason has to fit inside a report row."""
    trimmed = output.strip()
    return trimmed[-SNIPPET_CHARS:] if len(trimmed) > SNIPPET_CHARS else trimmed
