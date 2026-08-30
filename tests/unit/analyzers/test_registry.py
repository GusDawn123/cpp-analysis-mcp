"""Prove the gate chain refuses in order, explains itself, and never executes a tool. The
fakes are plain classes with no shared base; a consultation counter proves short-circuiting,
since a disabled analyzer's own (possibly costly) gates must never run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cpp_analysis_mcp.analyzers.base import (
    AnalyzerContext,
    AnalyzerRun,
    Applicability,
    CostTier,
    Registry,
    Scope,
    UnitOfWork,
)
from cpp_analysis_mcp.store.models import CapabilityStatus

A_SCOPE = Scope(project_root=Path("/repo"), files=("src/a.cpp", "src/b.cpp"))


class FakeTidy:
    """A static, TU-grained analyzer that gates on C++ sources being in scope."""

    name = "fake-tidy"
    cost_tier = CostTier.STATIC_SECONDS
    unit_of_work = UnitOfWork.TRANSLATION_UNIT

    def __init__(self) -> None:
        self.gate_consultations = 0

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        self.gate_consultations += 1
        if any(file.endswith(".cpp") for file in scope.files):
            return Applicability(eligible=True)
        return Applicability(eligible=False, reason="no C++ sources in scope")

    def run(self, scope: Scope, context: AnalyzerContext) -> AnalyzerRun:
        raise AssertionError("resolution must never execute a tool")


class FakeSanitizer:
    """A dynamic, target-grained analyzer -- same chain, different tier, no branches."""

    name = "fake-asan"
    cost_tier = CostTier.DYNAMIC_MINUTES
    unit_of_work = UnitOfWork.TARGET

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        return Applicability(eligible=True)

    def run(self, scope: Scope, context: AnalyzerContext) -> AnalyzerRun:
        raise AssertionError("resolution must never execute a tool")


def probes_saying_yes(*names: str) -> dict[str, CapabilityStatus]:
    return {name: CapabilityStatus(available=True, verified_by="test probe") for name in names}


# ---------------------------------------------------------------- registration


def test_registration_order_is_resolution_order() -> None:
    registry = Registry()
    registry.register(FakeSanitizer())
    registry.register(FakeTidy())

    context = AnalyzerContext(capabilities=probes_saying_yes("fake-asan", "fake-tidy"))
    names = [resolution.analyzer.name for resolution in registry.resolve(A_SCOPE, context)]

    assert names == ["fake-asan", "fake-tidy"]


def test_a_duplicate_name_is_refused_loudly() -> None:
    registry = Registry()
    registry.register(FakeTidy())

    with pytest.raises(ValueError, match="fake-tidy"):
        registry.register(FakeTidy())


# ---------------------------------------------------------------- the gate chain


def test_everything_passing_yields_an_eligible_verdict_with_no_reason() -> None:
    registry = Registry()
    registry.register(FakeTidy())

    (resolution,) = registry.resolve(
        A_SCOPE, AnalyzerContext(capabilities=probes_saying_yes("fake-tidy"))
    )

    assert resolution.verdict.eligible
    assert resolution.verdict.reason is None


def test_a_disabled_analyzer_is_refused_before_its_own_gates_run() -> None:
    tidy = FakeTidy()
    registry = Registry()
    registry.register(tidy)

    context = AnalyzerContext(
        enabled=frozenset(),  # configuration spoke, and named nobody
        capabilities=probes_saying_yes("fake-tidy"),
    )
    (resolution,) = registry.resolve(A_SCOPE, context)

    assert resolution.verdict.reason == "disabled in configuration"
    assert tidy.gate_consultations == 0  # the chain stopped before consulting the tool


def test_no_configuration_at_all_means_everything_is_enabled() -> None:
    registry = Registry()
    registry.register(FakeTidy())

    context = AnalyzerContext(enabled=None, capabilities=probes_saying_yes("fake-tidy"))
    (resolution,) = registry.resolve(A_SCOPE, context)

    assert resolution.verdict.eligible


def test_the_tools_own_refusal_reason_surfaces_verbatim() -> None:
    registry = Registry()
    registry.register(FakeTidy())

    headers_only = Scope(project_root=Path("/repo"), files=("include/a.hpp",))
    context = AnalyzerContext(capabilities=probes_saying_yes("fake-tidy"))
    (resolution,) = registry.resolve(headers_only, context)

    assert resolution.verdict.reason == "no C++ sources in scope"


def test_an_unconsulted_probe_is_not_treated_as_available() -> None:
    registry = Registry()
    registry.register(FakeTidy())

    (resolution,) = registry.resolve(A_SCOPE, AnalyzerContext())

    assert not resolution.verdict.eligible
    assert resolution.verdict.reason == "no capability probe result for 'fake-tidy'"


def test_the_probes_own_reason_surfaces_when_it_has_one() -> None:
    registry = Registry()
    registry.register(FakeTidy())

    context = AnalyzerContext(
        capabilities={
            "fake-tidy": CapabilityStatus(
                available=False, reason="clang-tidy is not on PATH", suggestion="brew install llvm"
            )
        }
    )
    (resolution,) = registry.resolve(A_SCOPE, context)

    assert resolution.verdict.reason == "clang-tidy is not on PATH"


def test_a_reasonless_refusal_is_rejected_at_construction() -> None:
    # a refusal with nothing to say would become a silent skip in the plan trace
    with pytest.raises(ValueError, match="refusals require one"):
        Applicability(eligible=False)


def test_a_reasoned_approval_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="carry no reason"):
        Applicability(eligible=True, reason="looks fine to me")


# ---------------------------------------------------------------- determinism


def test_resolution_is_deterministic_for_identical_inputs() -> None:
    registry = Registry()
    registry.register(FakeTidy())
    registry.register(FakeSanitizer())

    context = AnalyzerContext(capabilities=probes_saying_yes("fake-tidy"))

    first = registry.resolve(A_SCOPE, context)
    second = registry.resolve(A_SCOPE, context)

    assert [r.verdict for r in first] == [r.verdict for r in second]
    assert [r.analyzer.name for r in first] == [r.analyzer.name for r in second]


def test_both_tiers_fit_the_same_chain_without_branches() -> None:
    registry = Registry()
    registry.register(FakeTidy())
    registry.register(FakeSanitizer())

    context = AnalyzerContext(capabilities=probes_saying_yes("fake-tidy", "fake-asan"))
    static, dynamic = registry.resolve(A_SCOPE, context)

    assert static.verdict.eligible and dynamic.verdict.eligible
    assert {static.analyzer.cost_tier, dynamic.analyzer.cost_tier} == {
        CostTier.STATIC_SECONDS,
        CostTier.DYNAMIC_MINUTES,
    }
