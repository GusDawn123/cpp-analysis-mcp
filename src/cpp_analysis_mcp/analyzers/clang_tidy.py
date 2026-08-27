"""clang-tidy behind the analyzer contract: the first real tool to fit the shape.

This module is deliberately thin. The static_check pipeline already knows how to find
clang-tidy, choose a check set, ride the compilation database, and read what came back;
the shared adapter spine owns the gates and the outcome mapping. What is left here is
exactly what makes this plugin this plugin: its name, its tiers, and its refusal words.

The check step arrives as a constructor argument rather than being built here: composing
the real one takes a toolchain, a platform, and probe results, all of which are resolved
per-request by the layer that owns them. A default would mean toolchain discovery at
import time, which is how a library grows a hidden global. The wiring hands in a
partially-applied check_file; tests hand in fakes and never need a compiler.
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
