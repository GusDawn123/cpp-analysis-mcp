"""Dispatch runs the plan and nothing else, deterministically.

Thread timing must never reach the caller: results arrive in the plan's own step
order, tiers run strictly one after another, and a skipped analyzer never runs.
"""

from __future__ import annotations

import threading
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
from cpp_analysis_mcp.planner.dispatch import execute
from cpp_analysis_mcp.planner.plan import Plan, Step, plan
from cpp_analysis_mcp.store.models import CapabilityStatus, Finding, Location, Severity

ROOT = Path("/repo")


def a_finding(tool: str) -> Finding:
    return Finding(
        id=f"{tool}-0001",
        tool=tool,
        severity=Severity.WARNING,
        category="bugprone-use-after-move",
        message="'order' used after it was moved",
        location=Location(file="src/a.cpp", line=12),
    )


@dataclass
class FakeAnalyzer:
    """A plugin with scripted findings, and optional events to choreograph threads."""

    name: str
    cost_tier: CostTier = CostTier.STATIC_SECONDS
    unit_of_work: UnitOfWork = UnitOfWork.FILE
    verdict: Applicability = field(default_factory=lambda: Applicability(eligible=True))
    found: tuple[Finding, ...] = ()
    waits_for: threading.Event | None = None
    signals: threading.Event | None = None
    ran: list[Scope] = field(default_factory=list)

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        return self.verdict

    def run(self, scope: Scope, context: AnalyzerContext) -> tuple[Finding, ...]:
        self.ran.append(scope)
        if self.signals is not None:
            self.signals.set()
        if self.waits_for is not None:
            assert self.waits_for.wait(timeout=5), (
                "the tier ran serially; a parallel peer never signaled"
            )
        return self.found


def registered(*analyzers: FakeAnalyzer) -> Registry:
    registry = Registry()
    for analyzer in analyzers:
        registry.register(analyzer)
    return registry


def allowing(*names: str) -> AnalyzerContext:
    return AnalyzerContext(
        capabilities={name: CapabilityStatus(available=True, verified_by="probe") for name in names}
    )


def a_scope() -> Scope:
    return Scope(project_root=ROOT, files=("src/a.cpp",))


def test_results_arrive_in_plan_order_not_finish_order() -> None:
    """The first-sorted step is forced to finish last; its result still comes first."""
    gate = threading.Event()
    slow = FakeAnalyzer("aa-slow", waits_for=gate, found=(a_finding("aa-slow"),))
    quick = FakeAnalyzer("zz-quick", signals=gate, found=(a_finding("zz-quick"),))
    registry = registered(slow, quick)
    scope, context = a_scope(), allowing("aa-slow", "zz-quick")

    ran = execute(plan(scope, context, registry), scope, context, registry)

    assert [executed.analyzer for executed in ran] == ["aa-slow", "zz-quick"]
    assert ran[0].findings == (a_finding("aa-slow"),)
    assert ran[1].findings == (a_finding("zz-quick"),)


def test_a_dynamic_step_waits_for_the_static_tier() -> None:
    """Minutes of dynamic work must not start while cheap evidence is still arriving."""
    dynamic_started = threading.Event()

    @dataclass
    class StaticProbe(FakeAnalyzer):
        def run(self, scope: Scope, context: AnalyzerContext) -> tuple[Finding, ...]:
            self.ran.append(scope)
            # the window in which a missing tier barrier would start the dynamic step
            assert not dynamic_started.wait(timeout=0.2), "dynamic work began mid-static-tier"
            return ()

    static = StaticProbe("lint")
    deep = FakeAnalyzer("deep", cost_tier=CostTier.DYNAMIC_MINUTES, signals=dynamic_started)
    registry = registered(static, deep)
    scope, context = a_scope(), allowing("lint", "deep")

    ran = execute(plan(scope, context, registry), scope, context, registry)

    assert dynamic_started.is_set()
    assert [executed.analyzer for executed in ran] == ["lint", "deep"]


def test_a_skipped_analyzer_never_runs() -> None:
    refused = FakeAnalyzer(
        "warnings",
        verdict=Applicability(eligible=False, reason="gcc has no -Wthread-safety"),
    )
    registry = registered(refused, FakeAnalyzer("tidy"))
    scope, context = a_scope(), allowing("warnings", "tidy")

    ran = execute(plan(scope, context, registry), scope, context, registry)

    assert refused.ran == []
    assert [executed.analyzer for executed in ran] == ["tidy"]


def test_each_analyzer_is_handed_the_scope_exactly_once() -> None:
    fake = FakeAnalyzer("tidy")
    registry = registered(fake)
    scope, context = a_scope(), allowing("tidy")

    execute(plan(scope, context, registry), scope, context, registry)

    assert fake.ran == [scope]


def test_an_empty_plan_executes_nothing() -> None:
    registry = Registry()
    scope, context = a_scope(), AnalyzerContext()

    assert execute(plan(scope, context, registry), scope, context, registry) == ()


def test_a_plan_naming_an_unknown_analyzer_is_a_caller_bug() -> None:
    decided = Plan(
        files=("src/a.cpp",),
        steps=(Step(analyzer="ghost", tier=CostTier.STATIC_SECONDS),),
        skips=(),
    )

    with pytest.raises(KeyError, match="ghost"):
        execute(decided, a_scope(), AnalyzerContext(), Registry())
