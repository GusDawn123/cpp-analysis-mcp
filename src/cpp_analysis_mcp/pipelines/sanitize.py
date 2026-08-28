"""Gate on the capability, build, run, parse -- the four steps of one sanitizer analysis.

Each step is worthless alone and the order is the whole point. A capability status with no
run is a promise; a run without the environment its build decided on reports nothing; and
findings with no status behind them cannot be told from a detector that was never watching.
Keeping the four in one place means there is one order rather than one per caller.

The gate is a hard stop, not a note on the report. If the probe could not catch a planted
bug on this machine, nothing is spawned and the status comes back as the answer -- running
anyway would produce an empty finding list that reads exactly like clean code, which is the
false all-clear this project exists to avoid.

Composes primitives only, never another pipeline (rule 1); the Platform and Toolchain arrive
as arguments (rule 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cpp_analysis_mcp import process
from cpp_analysis_mcp.build import cmake, single_file
from cpp_analysis_mcp.parsers import PARSER_FOR
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import (
    SANITIZER_FOR,
    Analysis,
    AnalysisReport,
    BuildFailure,
    BuiltBinary,
    CapabilityStatus,
)
from cpp_analysis_mcp.toolchains.base import Toolchain

# a sanitized program runs at a fraction of its normal speed, so this is generous next to
# how long the same binary takes unsanitized
RUN_TIMEOUT_S = 60

SNIPPET_STEM = "snippet"


def analyze_file(
    source: Path,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    compile_timeout_s: int = single_file.COMPILE_TIMEOUT_S,
    run_timeout_s: int = RUN_TIMEOUT_S,
    runner: Runner = process.run,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Build one source file under its sanitizer and report what running it produced.

    Three outcomes, all of them ordinary: a report, the build failure that stopped one being
    produced, or the capability status saying this machine cannot run this analysis at all.
    """
    kind = SANITIZER_FOR[analysis]
    status = capabilities[analysis]
    if not status.available:
        return status

    built = single_file.compile_file(
        source,
        toolchain=toolchain,
        platform=platform,
        sanitizer=kind,
        build_dir=build_dir,
        timeout_s=compile_timeout_s,
        runner=runner,
    )
    if isinstance(built, BuildFailure):
        return built
    return _observe(built, analysis, status, run_timeout_s=run_timeout_s, runner=runner)


def analyze_project(
    project_dir: Path,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    target: str | None = None,
    build_timeout_s: int = cmake.CMAKE_TIMEOUT_S,
    run_timeout_s: int = RUN_TIMEOUT_S,
    runner: Runner = process.run,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Build a CMake project under its sanitizer and report what running its binary produced.

    With no `target`, a project holding one executable builds it; anything else comes back
    as the build's failure naming the targets there are.
    """
    kind = SANITIZER_FOR[analysis]
    status = capabilities[analysis]
    if not status.available:
        return status

    built = cmake.build_project(
        project_dir,
        toolchain=toolchain,
        platform=platform,
        sanitizer=kind,
        build_dir=build_dir,
        target=target,
        timeout_s=build_timeout_s,
        runner=runner,
    )
    if isinstance(built, BuildFailure):
        return built
    return _observe(built, analysis, status, run_timeout_s=run_timeout_s, runner=runner)


def analyze_snippet(
    text: str,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    compile_timeout_s: int = single_file.COMPILE_TIMEOUT_S,
    run_timeout_s: int = RUN_TIMEOUT_S,
    runner: Runner = process.run,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Write a piece of C++ to disk and analyze it as a file.

    The file stays behind on purpose: every path in a compiler diagnostic and every frame in
    a sanitizer report names it, and those are unreadable pointing at something deleted.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    source = build_dir / f"{SNIPPET_STEM}.cpp"
    source.write_text(text, encoding="utf-8")

    return analyze_file(
        source,
        analysis,
        toolchain=toolchain,
        platform=platform,
        capabilities=capabilities,
        build_dir=build_dir,
        compile_timeout_s=compile_timeout_s,
        run_timeout_s=run_timeout_s,
        runner=runner,
    )


def _observe(
    built: BuiltBinary,
    analysis: Analysis,
    status: CapabilityStatus,
    *,
    run_timeout_s: int,
    runner: Runner,
) -> AnalysisReport:
    """Run the binary under the environment it was bound to, and read what it said."""
    # what BuiltBinary is for: the options the build chose travel with the binary, and
    # hygienic_env drops the developer's shell copies of them on the way in. A sanitized
    # run without its own options reports nothing, which reads exactly like clean code.
    result = runner(
        [str(built.path)],
        timeout_s=run_timeout_s,
        env=process.hygienic_env(built.runtime_env),
        cwd=built.build_dir,
    )
    # parsed whatever the exit code was: UBSan reports and exits 0, TSan's pinned options
    # exit 66, and a run killed at its timeout still leaves the output it had produced
    return AnalysisReport(
        analysis=analysis,
        findings=tuple(PARSER_FOR[analysis](result.output)),
        build_warnings=built.warnings,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        limitations=status.limitations,
        verified_by=status.verified_by,
    )
