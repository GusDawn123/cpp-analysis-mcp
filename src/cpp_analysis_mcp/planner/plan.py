"""The plan: what will run in what order, what will not and why (ADR-0001).

Assembled from the registry's verdicts before anything executes, and deterministic
by construction. Files are carried once for the whole plan -- per-step file lists
arrive when units of work split beyond whole-scope dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass

from cpp_analysis_mcp.analyzers.base import AnalyzerContext, CostTier, Registry, Scope

__all__ = ["Plan", "Skip", "Step", "plan"]

# cheap evidence first: CostTier's definition order is the spend order, and the enum's
# string values would sort dynamic before static
_TIER_RANK = {tier: rank for rank, tier in enumerate(CostTier)}


@dataclass(frozen=True, slots=True)
class Step:
    """One analyzer that will run, at its declared cost."""

    analyzer: str
    tier: CostTier


@dataclass(frozen=True, slots=True)
class Skip:
    """One analyzer that will not run, and the words saying why."""

    analyzer: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("a skip must say why in words")


@dataclass(frozen=True, slots=True)
class Plan:
    """What one call decided: the trace a caller reads before believing any result."""

    files: tuple[str, ...]
    steps: tuple[Step, ...]
    skips: tuple[Skip, ...]


def plan(scope: Scope, context: AnalyzerContext, registry: Registry) -> Plan:
    """Resolve every analyzer once and fix the running order before anything executes.

    Steps sort by (tier, name) so cheap evidence lands first and dispatch order can
    never depend on registration order. Skips keep the registry's order and each
    refusal's own words.
    """
    steps: list[Step] = []
    skips: list[Skip] = []
    for row in registry.resolve(scope, context):
        if row.verdict.eligible:
            steps.append(Step(analyzer=row.analyzer.name, tier=row.analyzer.cost_tier))
        else:
            assert row.verdict.reason is not None  # Applicability: refusals carry words
            skips.append(Skip(analyzer=row.analyzer.name, reason=row.verdict.reason))
    steps.sort(key=lambda step: (_TIER_RANK[step.tier], step.analyzer))
    return Plan(files=scope.files, steps=tuple(steps), skips=tuple(skips))
