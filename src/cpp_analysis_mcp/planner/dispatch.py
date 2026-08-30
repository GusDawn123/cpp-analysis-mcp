"""Dispatch: run the plan's steps, cheap tier first, parallel inside a tier. Results come
back in the plan's own step order -- executor.map returns in submission order, so
determinism is structural rather than repaired by a sort afterward.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from itertools import groupby

from cpp_analysis_mcp.analyzers.base import Analyzer, AnalyzerContext, Registry, Scope
from cpp_analysis_mcp.planner.plan import Plan
from cpp_analysis_mcp.store.models import Finding, SuggestedFix

__all__ = ["Executed", "execute"]


@dataclass(frozen=True, slots=True)
class Executed:
    """One analyzer's completed dispatch: who ran, what it found, what it would fix."""

    analyzer: str
    findings: tuple[Finding, ...]
    suggestions: tuple[SuggestedFix, ...] = ()


def execute(
    decided: Plan, scope: Scope, context: AnalyzerContext, registry: Registry
) -> tuple[Executed, ...]:
    """Run every planned step and pair each analyzer with what it found. Tiers run strictly
    one after another -- dynamic minutes must not start while cheap static evidence is still
    arriving; steps inside a tier run in parallel, each waiting on a child process anyway.
    """
    analyzers: dict[str, Analyzer] = {analyzer.name: analyzer for analyzer in registry.analyzers()}
    missing = [step.analyzer for step in decided.steps if step.analyzer not in analyzers]
    if missing:
        raise KeyError(f"the plan names analyzers the registry does not hold: {missing}")

    ran: list[Executed] = []
    for _tier, batch in groupby(decided.steps, key=lambda step: step.tier):
        steps = list(batch)
        with ThreadPoolExecutor(max_workers=len(steps)) as pool:
            results = pool.map(lambda step: analyzers[step.analyzer].run(scope, context), steps)
            ran += [
                Executed(
                    analyzer=step.analyzer,
                    findings=produced.findings,
                    suggestions=produced.suggestions,
                )
                for step, produced in zip(steps, results, strict=True)
            ]
    return tuple(ran)
