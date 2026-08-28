"""The compiler's own warnings behind the analyzer contract: plugin #2, and the proof
that tools with nothing procedurally in common still fit one shape -- these findings
fall out of a compile the toolchain was doing anyway (-fsyntax-only, -Wthread-safety).

Thread-safety analysis is clang's alone, and this module never mentions that: gcc hosts
probe it unavailable and the capability gate refuses with the probe's own reason.
"""

from cpp_analysis_mcp.analyzers._adapter import (
    CheckFile,
    as_findings,
    checkable_sources,
    membership_gate,
)
from cpp_analysis_mcp.analyzers.base import (
    AnalyzerContext,
    Applicability,
    CostTier,
    Scope,
    UnitOfWork,
)
from cpp_analysis_mcp.store.models import Finding

__all__ = ["WarningsAnalyzer"]


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

    def run(self, scope: Scope, context: AnalyzerContext) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for file in checkable_sources(scope, context):
            outcome = self._check(scope.project_root / file)
            findings.extend(as_findings(outcome, file, self.name))
        return tuple(findings)
