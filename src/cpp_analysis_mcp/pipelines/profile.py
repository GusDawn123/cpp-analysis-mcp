"""Gate on the capability, build optimized, record, report -- one profiling run. Optimized
because -O2 inlines differently: an -O1 profile ranks call sites the release binary lacks.
Composes primitives only (rule 1); Platform and Toolchain arrive as arguments (rule 3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

from cpp_analysis_mcp import fingerprints, process, profiler, wsl
from cpp_analysis_mcp.build import cmake, single_file
from cpp_analysis_mcp.parsers import perf
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import (
    Analysis,
    BuildFailure,
    BuiltBinary,
    CapabilityStatus,
    Hotspot,
    Location,
    ProfileReport,
)
from cpp_analysis_mcp.toolchains.base import PROFILE_FLAGS, Toolchain

# sampling costs a timer interrupt, not a sanitizer's 10x, so a profiled binary runs at
# near-normal speed and this budget is five minutes of real work rather than of overhead
RUN_TIMEOUT_S = 300

# reading the trace back means resolving every sampled address against the binary and every
# shared library it loaded, which on a large C++ program is slower than it sounds
REPORT_TIMEOUT_S = 120

# perf places a sample at the innermost frame, which after inlining is a line in whatever
# header the optimizer pulled in. The line is real and it is not where the hot function
# lives, so it is labelled rather than dropped: evidence, just not an address to go open.
OUTSIDE_NOTE = (
    "this line is outside the profiled project, usually a library header inlined into the "
    "function -- the function name, not this location, is what to act on"
)


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
    """Build one source file optimized and report where running it spent its time. Three
    ordinary outcomes: a report, the build failure that stopped one, or the capability
    status saying this machine cannot profile at all.
    """
    # a hard stop, not a degrade: a profiler that was never watching produces an empty
    # hotspot list, and an empty hotspot list reads exactly like a program with no hot code
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
    return _observe(built, status, root=source.parent, run_timeout_s=run_timeout_s, runner=runner)


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
    With no `target`, a one-executable project builds it; anything else is the build's
    failure naming the targets. Prefer a benchmark target: the default measures its startup.
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
    return _observe(built, status, root=project_dir, run_timeout_s=run_timeout_s, runner=runner)


def _observe(
    built: BuiltBinary,
    status: CapabilityStatus,
    *,
    root: Path,
    run_timeout_s: int,
    runner: Runner,
) -> ProfileReport:
    """Sample the binary, then read the trace back into a ranking. The report step runs
    whatever the recording exited with -- a crashed or killed workload still holds every
    sample it took, and `exit_code`, `timed_out`, and `samples` tell the reader what happened.
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
    spots = _attributed(perf.parse(reported.output), root)
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


def _attributed(spots: Sequence[Hotspot], root: Path) -> tuple[Hotspot, ...]:
    """Note every hotspot whose line landed outside the project that was profiled."""
    roots = _roots(root)
    return tuple(spot if _inside(spot.location, roots) else _noted(spot) for spot in spots)


def _roots(root: Path) -> tuple[PurePosixPath, ...]:
    """Every absolute spelling the profiled project can appear under in a perf report: the
    bridge's /mnt/<drive> form for a run that crossed into WSL, the host's own for one that
    did not, and resolved spellings so a caller's relative root still draws the boundary.
    """
    resolved = root.resolve()
    spellings = (
        PurePosixPath(root.as_posix()),
        PurePosixPath(wsl.to_wsl(str(root))),
        PurePosixPath(resolved.as_posix()),
        PurePosixPath(wsl.to_wsl(str(resolved))),
    )
    return tuple(dict.fromkeys(spelling for spelling in spellings if spelling.is_absolute()))


def _inside(location: Location | None, roots: tuple[PurePosixPath, ...]) -> bool:
    """Report whether a location is the project's own, by path alone -- nothing is stat'd,
    because a bridged run reports Linux paths this host cannot see. Relative lines and
    unplaced locations read as inside: the note is added on a match, never on a guess.
    """
    if location is None or not roots:
        return True
    file = PurePosixPath(location.file.replace("\\", "/"))
    return not file.is_absolute() or any(file.is_relative_to(root) for root in roots)


def _noted(spot: Hotspot) -> Hotspot:
    """Append the note, keeping whatever the parser observed: both facts hold at once."""
    return replace(spot, note=f"{spot.note}; {OUTSIDE_NOTE}" if spot.note else OUTSIDE_NOTE)
