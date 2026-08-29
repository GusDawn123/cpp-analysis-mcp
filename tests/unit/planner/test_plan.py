"""The plan is the trace: what will run in what order, what will not and why.

Built from the registry's verdicts and nothing else -- planning never executes a
tool, and the same scope, context, and registry produce the same plan every time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp.analyzers.base import (
    AnalyzerContext,
    Applicability,
    CostTier,
    Registry,
    Scope,
    UnitOfWork,
)
from cpp_analysis_mcp.analyzers.clang_tidy import ClangTidyAnalyzer
from cpp_analysis_mcp.analyzers.warnings import WarningsAnalyzer
from cpp_analysis_mcp.planner.plan import Skip, Step, plan
from cpp_analysis_mcp.store.models import (
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
)

ROOT = Path("/repo")

GCC_REFUSAL = "gcc has no equivalent of clang's -Wthread-safety, not a weaker version."


@dataclass
class FakeAnalyzer:
    """A plugin that answers a scripted verdict and records any run it is asked for."""

    name: str
    cost_tier: CostTier = CostTier.STATIC_SECONDS
    unit_of_work: UnitOfWork = UnitOfWork.FILE
    verdict: Applicability = field(default_factory=lambda: Applicability(eligible=True))
    ran: list[Scope] = field(default_factory=list)

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        return self.verdict

    def run(self, scope: Scope, context: AnalyzerContext) -> tuple[Finding, ...]:
        self.ran.append(scope)
        return ()


def registered(*analyzers: FakeAnalyzer) -> Registry:
    registry = Registry()
    for analyzer in analyzers:
        registry.register(analyzer)
    return registry


def allowing(*names: str) -> AnalyzerContext:
    return AnalyzerContext(
        capabilities={name: CapabilityStatus(available=True, verified_by="probe") for name in names}
    )


def a_scope(*files: str) -> Scope:
    return Scope(project_root=ROOT, files=files or ("src/a.cpp",))


def test_cheap_tiers_run_first_and_names_break_ties() -> None:
    """Registration order must not leak into dispatch order."""
    registry = registered(
        FakeAnalyzer("zz-lint"),
        FakeAnalyzer("deep-check", cost_tier=CostTier.DYNAMIC_MINUTES),
        FakeAnalyzer("aa-lint"),
    )

    decided = plan(a_scope(), allowing("zz-lint", "deep-check", "aa-lint"), registry)

    assert decided.steps == (
        Step(analyzer="aa-lint", tier=CostTier.STATIC_SECONDS),
        Step(analyzer="zz-lint", tier=CostTier.STATIC_SECONDS),
        Step(analyzer="deep-check", tier=CostTier.DYNAMIC_MINUTES),
    )
    assert decided.skips == ()


def test_a_refusal_lands_in_skips_with_its_own_words() -> None:
    refused = FakeAnalyzer("warnings", verdict=Applicability(eligible=False, reason=GCC_REFUSAL))
    registry = registered(refused, FakeAnalyzer("tidy"))

    decided = plan(a_scope(), allowing("warnings", "tidy"), registry)

    assert decided.skips == (Skip(analyzer="warnings", reason=GCC_REFUSAL),)
    assert [step.analyzer for step in decided.steps] == ["tidy"]


def test_an_unprobed_analyzer_is_skipped_not_trusted() -> None:
    registry = registered(FakeAnalyzer("tidy"))

    decided = plan(a_scope(), AnalyzerContext(), registry)

    assert decided.steps == ()
    assert decided.skips[0].reason == "no capability probe result for 'tidy'"


def test_the_plan_carries_the_scope_files_once() -> None:
    registry = registered(FakeAnalyzer("tidy"))

    decided = plan(a_scope("src/a.cpp", "src/b.cpp"), allowing("tidy"), registry)

    assert decided.files == ("src/a.cpp", "src/b.cpp")


def test_planning_never_runs_a_tool() -> None:
    fakes = (FakeAnalyzer("tidy"), FakeAnalyzer("deep", cost_tier=CostTier.DYNAMIC_MINUTES))
    registry = registered(*fakes)

    plan(a_scope(), allowing("tidy", "deep"), registry)

    assert [fake.ran for fake in fakes] == [[], []]


def test_the_same_inputs_produce_the_same_plan() -> None:
    registry = registered(FakeAnalyzer("tidy"))
    scope, context = a_scope(), allowing("tidy")

    assert plan(scope, context, registry) == plan(scope, context, registry)


def test_a_skip_requires_words() -> None:
    with pytest.raises(ValueError, match="words"):
        Skip(analyzer="tidy", reason="")


def _never_checks(source: Path) -> AnalysisReport | BuildFailure | CapabilityStatus:
    raise AssertionError(f"planning must not execute, but was asked to check {source}")


def test_a_ten_thousand_file_scope_plans_in_under_a_second() -> None:
    """The latency gate for planning: its job is catching a per-file quadratic in the
    gate chain. Real plugins, so the measured cost includes their actual gates."""
    registry = Registry()
    registry.register(ClangTidyAnalyzer(check=_never_checks))
    registry.register(WarningsAnalyzer(check=_never_checks))
    scope = Scope(
        project_root=ROOT,
        files=tuple(f"src/dir_{index % 100}/file_{index}.cpp" for index in range(10_000)),
    )
    context = allowing("clang-tidy", "compiler-warnings")

    started = time.perf_counter()
    decided = plan(scope, context, registry)
    elapsed = time.perf_counter() - started

    assert len(decided.steps) == 2
    assert elapsed < 1.0, f"planning 10k files took {elapsed:.3f}s"
