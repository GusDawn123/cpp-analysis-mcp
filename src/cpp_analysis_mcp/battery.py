"""Run every correctness analysis over one file and merge what they saw.

Sits above the pipelines, below the server: the layer rule bars a pipeline from
importing another, but this needs all of them, so the composition lives here instead.
Merging deduplicates, since all four sanitizer builds share warning flags and would
otherwise report one compile-time warning four times.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from cpp_analysis_mcp.pipelines import sanitize, static_check
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import (
    Analysis,
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
    FullCheckReport,
)
from cpp_analysis_mcp.toolchains.base import Toolchain

CORRECTNESS: tuple[Analysis, ...] = (
    Analysis.THREAD_SAFETY,
    Analysis.CLANG_TIDY,
    Analysis.TSAN,
    Analysis.ASAN,
    Analysis.LSAN,
    Analysis.UBSAN,
)

FIX_AND_RERUN = "fix the findings, then run full_check_file again to confirm the fixes hold"


class Engine(Protocol):
    """What one analysis runs on. Structural, so context's Engine fits without an import."""

    @property
    def toolchain(self) -> Toolchain: ...
    @property
    def platform(self) -> Platform: ...
    @property
    def runner(self) -> Runner: ...


def check_file(
    source: Path,
    *,
    engines: Mapping[Analysis, Engine],
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    checks: str | None = None,
) -> FullCheckReport:
    """Run the whole battery and fold six outcomes into one report.

    An unavailable analysis or a failed build never stops the rest: each lands in its
    own section of the report with its reason, and the analyses that could run still do.
    """
    with ThreadPoolExecutor(max_workers=len(CORRECTNESS)) as pool:
        futures = {
            analysis: pool.submit(
                _one,
                analysis,
                source,
                engine=engines[analysis],
                capabilities=capabilities,
                build_dir=build_dir,
                checks=checks,
            )
            for analysis in CORRECTNESS
        }
        outcomes = {analysis: future.result() for analysis, future in futures.items()}
    return _merge(outcomes)


def _one(
    analysis: Analysis,
    source: Path,
    *,
    engine: Engine,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    checks: str | None,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    if analysis in (Analysis.THREAD_SAFETY, Analysis.CLANG_TIDY):
        return static_check.check_file(
            source,
            analysis,
            toolchain=engine.toolchain,
            platform=engine.platform,
            capabilities=capabilities,
            checks=checks,
            runner=engine.runner,
        )
    return sanitize.analyze_file(
        source,
        analysis,
        toolchain=engine.toolchain,
        platform=engine.platform,
        capabilities=capabilities,
        # separate directories, or parallel builds of the same source overwrite each other
        build_dir=build_dir / analysis.value,
        runner=engine.runner,
    )


def _merge(
    outcomes: Mapping[Analysis, AnalysisReport | BuildFailure | CapabilityStatus],
) -> FullCheckReport:
    findings: list[Finding] = []
    ran: list[str] = []
    unavailable: dict[str, str] = {}
    failed: dict[str, str] = {}
    for analysis in CORRECTNESS:
        outcome = outcomes[analysis]
        if isinstance(outcome, AnalysisReport):
            ran.append(analysis.value)
            findings.extend(outcome.findings)
            findings.extend(outcome.build_warnings)
        elif isinstance(outcome, BuildFailure):
            failed[analysis.value] = outcome.reason or _first_line(outcome.output)
        else:
            unavailable[analysis.value] = outcome.reason or "unavailable on this machine"

    unique = _dedupe(findings)
    return FullCheckReport(
        findings=unique,
        ran=tuple(ran),
        unavailable=unavailable,
        failed_builds=failed,
        next_step=FIX_AND_RERUN if unique else None,
    )


def _dedupe(findings: list[Finding]) -> tuple[Finding, ...]:
    seen: set[tuple[str, str, str | None, int | None]] = set()
    unique: list[Finding] = []
    for finding in findings:
        where = finding.location
        key = (
            finding.category,
            finding.message,
            where.file if where else None,
            where.line if where else None,
        )
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return tuple(unique)


def _first_line(output: str) -> str:
    stripped = output.strip()
    return stripped.splitlines()[0] if stripped else "build failed with no output"
