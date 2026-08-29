"""Gate on the capability, run one compile-time check, parse -- one static analysis.

The real invocations live with their plugins (analyzers.clang_tidy and
analyzers.warnings); this front routes one file or snippet through the registry's
gates, binds the check, and stamps every finding with its identity on the way out.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from cpp_analysis_mcp import process
from cpp_analysis_mcp.analyzers import clang_tidy as tidy_plugin
from cpp_analysis_mcp.analyzers import warnings as warnings_plugin
from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Registry, Resolution, Scope
from cpp_analysis_mcp.analyzers.clang_tidy import ClangTidyAnalyzer
from cpp_analysis_mcp.analyzers.warnings import WarningsAnalyzer
from cpp_analysis_mcp.planner.scope import line_reader, relativizer
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.fingerprints import fingerprint_batch
from cpp_analysis_mcp.store.models import (
    Analysis,
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
)
from cpp_analysis_mcp.toolchains.base import Toolchain

# nothing runs the checked program, so this only has to cover parsing one translation unit;
# it is generous next to the seconds either check usually takes
CHECK_TIMEOUT_S = 60

SNIPPET_STEM = "snippet"


class _Builder(Protocol):
    """One plugin's file_check signature, shared so the caller never branches."""

    def __call__(
        self,
        *,
        toolchain: Toolchain,
        platform: Platform,
        status: CapabilityStatus,
        checks: str | None,
        timeout_s: int,
        runner: Runner,
    ) -> Callable[[Path], AnalysisReport | BuildFailure | CapabilityStatus]: ...


def check_file(
    source: Path,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    checks: str | None = None,
    timeout_s: int = CHECK_TIMEOUT_S,
    runner: Runner = process.run,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Check one source file at compile time and report what the tool said about it.

    Three outcomes, all of them ordinary: a report, the failure that stopped one being
    produced, or the capability status saying this machine cannot run this check at all.
    """
    outcome, _ = _routed_check(
        source,
        analysis,
        toolchain=toolchain,
        platform=platform,
        capabilities=capabilities,
        checks=checks,
        timeout_s=timeout_s,
        runner=runner,
    )
    return outcome


def check_snippet(
    text: str,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    checks: str | None = None,
    timeout_s: int = CHECK_TIMEOUT_S,
    runner: Runner = process.run,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Write a piece of C++ to disk and check it as a file.

    The file stays behind on purpose: every finding names it, and a location pointing at
    something that was deleted is unreadable.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    source = build_dir / f"{SNIPPET_STEM}.cpp"
    source.write_text(text, encoding="utf-8")

    outcome, _ = _routed_check(
        source,
        analysis,
        toolchain=toolchain,
        platform=platform,
        capabilities=capabilities,
        checks=checks,
        timeout_s=timeout_s,
        runner=runner,
        # the scratch directory is minted fresh per call; hashing paths relative to it
        # is what makes the same snippet the same finding on every machine and run
        canonical=relativizer(build_dir),
    )
    return outcome


def _routed_check(
    source: Path,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    checks: str | None,
    timeout_s: int,
    runner: Runner,
    canonical: Callable[[str], str] | None = None,
) -> tuple[AnalysisReport | BuildFailure | CapabilityStatus, tuple[Resolution, ...]]:
    """The registry decides, the check runs, and every finding leaves carrying identity.

    Routes through the registry's gate chain over both compile-time plugins, so a refusal
    here and a skip in a plan trace are the same verdict from the same code. A
    caller-named scope passes the selection gates by design; the capability gate binds
    regardless, returning the probe's own status object on refusal.

    File checks fingerprint the tool's printed absolute paths (no `canonical`): their
    project root arrives with the git-aware scope, and no persisted baseline exists yet
    for that change to orphan. Snippet checks pass one rooted at their scratch dir.
    """
    build_check = _BUILDERS[analysis]
    resolutions = _registry().resolve(
        Scope(project_root=source.parent, files=(source.name,), caller_named=True),
        AnalyzerContext(
            capabilities={
                ClangTidyAnalyzer.name: capabilities[Analysis.CLANG_TIDY],
                WarningsAnalyzer.name: capabilities[Analysis.THREAD_SAFETY],
            }
        ),
    )
    verdict = next(
        row.verdict for row in resolutions if row.analyzer.name == _PLUGIN_NAMES[analysis]
    )
    if not verdict.eligible:
        return capabilities[analysis], resolutions

    check = build_check(
        toolchain=toolchain,
        platform=platform,
        status=capabilities[analysis],
        checks=checks,
        timeout_s=timeout_s,
        runner=runner,
    )
    outcome = check(source)
    if isinstance(outcome, AnalysisReport):
        stamped = fingerprint_batch(outcome.findings, line_reader(), canonical=canonical)
        outcome = replace(outcome, findings=stamped)
    return outcome, resolutions


def _registry() -> Registry:
    """Both compile-time plugins, registered for their gates alone.

    The sentinel check documents that resolution never executes: if it ever fires, a
    plugin's run loop entered a path that promised verdicts only.
    """
    registry = Registry()
    registry.register(ClangTidyAnalyzer(check=_no_execution))
    registry.register(WarningsAnalyzer(check=_no_execution))
    return registry


def _no_execution(source: Path) -> AnalysisReport | BuildFailure | CapabilityStatus:
    raise RuntimeError(f"gate resolution never runs a tool, but was asked to check {source}")


# only the compile-time analyses: asking this pipeline for a sanitizer raises rather than
# quietly checking syntax and calling the result a clean TSan run
_BUILDERS: Mapping[Analysis, _Builder] = {
    Analysis.THREAD_SAFETY: warnings_plugin.file_check,
    Analysis.CLANG_TIDY: tidy_plugin.file_check,
}

# which plugin fronts each analysis in the registry. The warnings plugin answers for the
# thread-safety probe because that is literally today's gate for the warnings path; the
# probe rework of a later phase renames probes after the analyzers that own them
_PLUGIN_NAMES: Mapping[Analysis, str] = {
    Analysis.THREAD_SAFETY: WarningsAnalyzer.name,
    Analysis.CLANG_TIDY: ClangTidyAnalyzer.name,
}
