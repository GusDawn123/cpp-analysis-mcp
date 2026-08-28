"""clang-tidy behind the analyzer contract: the first real tool to fit the shape.

Deliberately thin -- the static_check pipeline and shared adapter own the real work;
this holds only this plugin's name, tiers, and refusal words. The check step arrives as
a constructor argument rather than being built here, so import time never triggers
toolchain discovery -- a default would grow this into a library with a hidden global.
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

__all__ = ["ClangTidyAnalyzer"]


class ClangTidyAnalyzer:
    """The static tier's first plugin: TU-grained, seconds-cheap, compilation-dependent."""

    name = "clang-tidy"
    cost_tier = CostTier.STATIC_SECONDS
    unit_of_work = UnitOfWork.TRANSLATION_UNIT

    def __init__(self, check: CheckFile) -> None:
        self._check = check

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        return membership_gate(
            scope,
            context,
            no_sources_reason="no translation units in scope: clang-tidy analyzes what compiles",
        )

    def run(self, scope: Scope, context: AnalyzerContext) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for file in checkable_sources(scope, context):
            outcome = self._check(scope.project_root / file)
            findings.extend(as_findings(outcome, file, self.name))
        return tuple(findings)
