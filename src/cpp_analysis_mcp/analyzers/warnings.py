"""The compiler's own warnings behind the analyzer contract: plugin #2, and the proof
that tools with nothing procedurally in common still fit one shape -- these findings
fall out of a compile the toolchain was doing anyway (-fsyntax-only, -Wthread-safety).

Thread-safety analysis is clang's alone, and this module never mentions that: gcc hosts
probe it unavailable and the capability gate refuses with the probe's own reason.
"""

from pathlib import Path

from cpp_analysis_mcp.analyzers._adapter import (
    NO_DATABASE_NOTE,
    STANDARD,
    Checked,
    CheckFile,
    as_findings,
    checkable_sources,
    membership_gate,
    outcome,
    project_flags,
)
from cpp_analysis_mcp.analyzers.base import (
    AnalyzerContext,
    AnalyzerRun,
    Applicability,
    CostTier,
    Scope,
    UnitOfWork,
)
from cpp_analysis_mcp.parsers.diagnostics import parse as parse_diagnostics
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner, hygienic_env
from cpp_analysis_mcp.store.models import (
    Analysis,
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
)
from cpp_analysis_mcp.toolchains.base import Toolchain

__all__ = ["WarningsAnalyzer", "file_check"]

STAGE = "thread-safety"


class WarningsAnalyzer:
    """Compile-time diagnostics as findings: TU-grained, seconds-cheap, build-borne."""

    name = "compiler-warnings"
    cost_tier = CostTier.STATIC_SECONDS
    unit_of_work = UnitOfWork.TRANSLATION_UNIT

    def __init__(self, check: CheckFile) -> None:
        self._check = check

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        return membership_gate(
            scope,
            context,
            no_sources_reason=(
                "no translation units in scope: compiler warnings come from compiling"
            ),
        )

    def run(self, scope: Scope, context: AnalyzerContext) -> AnalyzerRun:
        findings: list[Finding] = []
        for file in checkable_sources(scope, context):
            checked = self._check(scope.project_root / file)
            findings.extend(as_findings(checked, file, self.name))
        # the compiler offers no machine-readable edits, so none travel from here
        return AnalyzerRun(findings=tuple(findings))


def file_check(
    *,
    toolchain: Toolchain,
    platform: Platform,
    status: CapabilityStatus,
    checks: str | None,
    timeout_s: int,
    runner: Runner,
) -> CheckFile:
    """Bind the real compile-and-read invocation into the contract's one-argument shape.

    `checks` means something to clang-tidy alone; it rides on the shared signature so
    a caller never branches on the analysis before dispatching on it.
    """

    def check(source: Path) -> AnalysisReport | BuildFailure | CapabilityStatus:
        checked = _invoke(
            source, toolchain=toolchain, platform=platform, timeout_s=timeout_s, runner=runner
        )
        return outcome(checked, Analysis.THREAD_SAFETY, status, engine=platform.engine)

    return check


def _invoke(
    source: Path, *, toolchain: Toolchain, platform: Platform, timeout_s: int, runner: Runner
) -> Checked:
    """Compile the file with -fsyntax-only and read the compiler's own diagnostics."""
    database, project = project_flags(source)
    result = runner(
        [
            str(toolchain.compiler),
            STANDARD,
            # no output file: the warnings are the product, and a snippet with no main()
            # must still be checkable, which a link step would refuse
            "-fsyntax-only",
            *toolchain.warning_flags,
            *platform.compile_extras,
            # after ours, so a project that builds at a different language standard wins:
            # last -std= on a clang command line is the one that takes effect
            *project,
            str(source),
        ],
        timeout_s=timeout_s,
        env=hygienic_env({}),
    )
    return Checked(
        stage=STAGE,
        result=result,
        findings=parse_diagnostics(result.output),
        notes=() if database is not None else (NO_DATABASE_NOTE,),
        database=database,
    )
