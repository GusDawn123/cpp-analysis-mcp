"""Gate on the capability, build optimized, record, report -- one profiling run.

The same four-step shape sanitize.py has, and the same reason for keeping the steps in one
place: each is worthless alone and the order carries meaning that neither end would enforce.
The gate is the same hard stop, for the same reason -- a profiler that was never watching
produces an empty hotspot list, and an empty hotspot list reads exactly like a program with
no hot code.

One step differs from the sanitizers' and it is the step that decides whether the answer is
worth anything: the build is optimized rather than instrumented. A sanitizer wants the code
it was given, so it builds at -O1 and accepts the slowdown. A profiler wants the code that
will actually run, and -O2 makes different inlining decisions than -O1 -- profile the -O1
build and the ranking describes call sites the release binary does not contain.

Composes primitives only, never another pipeline (rule 1); the Platform and Toolchain arrive
as arguments (rule 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cpp_analysis_mcp import fingerprints, process, profiler
from cpp_analysis_mcp.build import cmake, single_file
from cpp_analysis_mcp.parsers import perf
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import (
    Analysis,
    BuildFailure,
    BuiltBinary,
    CapabilityStatus,
    ProfileReport,
)
from cpp_analysis_mcp.toolchains.base import PROFILE_FLAGS, Toolchain

# a profiled binary runs at very close to its normal speed -- sampling costs a timer
# interrupt, not the 10x a sanitizer's instrumentation does -- so this is five minutes of
# real work rather than five minutes of overhead, and a benchmark worth profiling uses it
RUN_TIMEOUT_S = 300

# reading the trace back means resolving every sampled address against the binary and every
# shared library it loaded, which on a large C++ program is slower than it sounds
REPORT_TIMEOUT_S = 120


def profile_file(
    source: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    compile_timeout_s: int = single_file.COMPILE_TIMEOUT_S,
    run_timeout_s: int = RUN_TIMEOUT_S,
    runner: Runner = process.run,
) -> ProfileReport | BuildFailure | CapabilityStatus:
    """Build one source file optimized and report where running it spent its time.

    Three outcomes, all of them ordinary: a report, the build failure that stopped one being
    produced, or the capability status saying this machine cannot profile at all.
    """
    status = capabilities[Analysis.PROFILE]
    if not status.available:
        return status

    built = single_file.compile_file(
        source,
        toolchain=toolchain,
        platform=platform,
        sanitizer=None,
        build_dir=build_dir,
        base_flags=PROFILE_FLAGS,
        timeout_s=compile_timeout_s,
        runner=runner,
    )
    if isinstance(built, BuildFailure):
        return built
    return _observe(built, status, run_timeout_s=run_timeout_s, runner=runner)


def profile_project(
    project_dir: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    target: str | None = None,
    build_timeout_s: int = cmake.CMAKE_TIMEOUT_S,
    run_timeout_s: int = RUN_TIMEOUT_S,
    runner: Runner = process.run,
) -> ProfileReport | BuildFailure | CapabilityStatus:
    """Build a CMake project optimized and report where running its binary spent its time.

    With no `target`, a project holding one executable builds it; anything else comes back
    as the build's failure naming the targets there are. On a project that has a benchmark
    target, that target is almost always the one worth naming here: profiling whatever the
    default happens to be measures its startup.
    """
    status = capabilities[Analysis.PROFILE]
    if not status.available:
        return status

    built = cmake.build_project(
        project_dir,
        toolchain=toolchain,
        platform=platform,
        sanitizer=None,
        build_dir=build_dir,
        target=target,
        base_flags=PROFILE_FLAGS,
        timeout_s=build_timeout_s,
        runner=runner,
    )
    if isinstance(built, BuildFailure):
        return built
    return _observe(built, status, run_timeout_s=run_timeout_s, runner=runner)


def _observe(
    built: BuiltBinary,
    status: CapabilityStatus,
    *,
    run_timeout_s: int,
    runner: Runner,
) -> ProfileReport:
    """Sample the binary, then read the trace back into a ranking.

    The report step runs whatever the recording exited with. A workload that crashed halfway
    still profiled the half it reached, and a recording killed at its timeout still holds
    every sample taken before the kill -- refusing to read either would throw away the only
    measurement there is. What the run did is carried on the report instead, where a reader
    weighing the ranking can see it: `exit_code`, `timed_out`, and above all `samples`.
    """
    data = built.build_dir / profiler.DATA_NAME
    recorded = runner(
        profiler.record_command(built.path, data),
        timeout_s=run_timeout_s,
        env=process.hygienic_env({}),
        cwd=built.build_dir,
    )
    reported = runner(
        profiler.report_command(data),
        timeout_s=REPORT_TIMEOUT_S,
        env=process.hygienic_env({}),
        cwd=built.build_dir,
    )

    samples, event = perf.header(reported.output)
    spots = perf.parse(reported.output)
    found = fingerprints.read(spots)
    return ProfileReport(
        analysis=Analysis.PROFILE,
        hotspots=spots,
        samples=samples,
        event=event,
        fingerprints=found,
        confidence=fingerprints.confidence(samples),
        next_step=fingerprints.next_step(found),
        exit_code=recorded.exit_code,
        timed_out=recorded.timed_out,
        limitations=(*status.limitations, profiler.TRUNCATION),
        verified_by=status.verified_by,
    )
