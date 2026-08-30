"""The analyzer contract: one shape every tool fits, static or dynamic (layer 3).
Nothing here executes a tool -- resolving is layer 3's job, running is layer 4's, and
that line is what keeps an analyzer a plugin instead of a pipeline.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from cpp_analysis_mcp.store.models import CapabilityStatus, Finding, SuggestedFix

__all__ = [
    "Analyzer",
    "AnalyzerContext",
    "AnalyzerRun",
    "Applicability",
    "CostTier",
    "Registry",
    "Resolution",
    "Scope",
    "UnitOfWork",
]


class CostTier(StrEnum):
    """What running this analyzer costs, coarsely -- the planner orders cheap-first.
    Two tiers because the planner makes exactly one ordering decision with them.
    """

    STATIC_SECONDS = "static-seconds"
    DYNAMIC_MINUTES = "dynamic-minutes"


class UnitOfWork(StrEnum):
    """The granularity an analyzer naturally works in, declared rather than assumed:
    clang-tidy wants translation units, sanitizers want build targets, and dynamic
    verification points a TARGET-grained analyzer at the target a finding implicates.
    """

    FILE = "file"
    TRANSLATION_UNIT = "translation-unit"
    TARGET = "target"
    PROJECT = "project"


@dataclass(frozen=True, slots=True)
class Scope:
    """The files a run is about, already resolved to concrete paths. Resolution -- diff,
    explicit list, whole project -- happens above this layer; analyzers never touch git.
    """

    project_root: Path
    files: tuple[str, ...]
    # whether a caller pointed at these exact files rather than a scan resolving them:
    # selection gates pick files out of a scan and have nothing to pick when the user
    # already pointed; capability gates bind either way
    caller_named: bool = False


_NO_CAPABILITIES: Mapping[str, CapabilityStatus] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class AnalyzerContext:
    """What gates are allowed to know: configuration, the build, and the probes --
    deliberately not the server's Context, and exactly what a test must fake.
    `enabled=None` means no configuration spoke; an empty frozenset means it said no to all.
    """

    enabled: frozenset[str] | None = None
    # paths that are genuinely part of the build, from compile_commands.json -- the
    # membership gate for compilation-dependent tools, precomputed to a set so each
    # lookup is O(1) rather than a scan
    translation_units: frozenset[str] = frozenset()
    # probe results keyed by analyzer name; a missing entry is an unconsulted probe, which
    # the capability gate reads as "not available" rather than "probably fine". The shared
    # empty proxy exists because dataclasses refuses an unhashable default outright
    capabilities: Mapping[str, CapabilityStatus] = field(default_factory=lambda: _NO_CAPABILITIES)


@dataclass(frozen=True, slots=True)
class Applicability:
    """A gate verdict with its why. Eligible verdicts carry no reason; refusals must --
    enforced, because a reasonless refusal would put a silent skip in the plan trace.
    """

    eligible: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.eligible == (self.reason is not None):
            raise ValueError("eligible verdicts carry no reason; refusals require one")


@dataclass(frozen=True, slots=True)
class AnalyzerRun:
    """What one analyzer produced: what it observed, and what its tool offered to fix.
    Suggestions travel beside the findings -- the Finding schema is frozen (ADR-0002).
    """

    findings: tuple[Finding, ...]
    suggestions: tuple[SuggestedFix, ...] = ()


class Analyzer(Protocol):
    """What every tool implements -- structurally, no base class to inherit. `applicable`
    holds the tool's own gates; the registry wraps them with the generic ones.
    """

    name: str
    cost_tier: CostTier
    unit_of_work: UnitOfWork

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability: ...

    def run(self, scope: Scope, context: AnalyzerContext) -> AnalyzerRun: ...


@dataclass(frozen=True, slots=True)
class Resolution:
    """One analyzer's verdict for one scope -- the row a plan trace is made of."""

    analyzer: Analyzer
    verdict: Applicability


class Registry:
    """Registered analyzers, and the gate chain that turns scope into verdicts. The chain
    stops at the first refusal -- configuration, then the analyzer's own gates, then the
    capability probes -- and resolution never executes a tool: same inputs, same verdicts.
    """

    def __init__(self) -> None:
        # name-keyed and insertion-ordered: registration order is resolution order,
        # and a duplicate name is a programming error worth hearing about immediately
        self._analyzers: dict[str, Analyzer] = {}

    def register(self, analyzer: Analyzer) -> None:
        if analyzer.name in self._analyzers:
            raise ValueError(f"analyzer {analyzer.name!r} is already registered")
        self._analyzers[analyzer.name] = analyzer

    def analyzers(self) -> tuple[Analyzer, ...]:
        return tuple(self._analyzers.values())

    def resolve(self, scope: Scope, context: AnalyzerContext) -> tuple[Resolution, ...]:
        return tuple(
            Resolution(analyzer, self._verdict(analyzer, scope, context))
            for analyzer in self._analyzers.values()
        )

    @staticmethod
    def _verdict(analyzer: Analyzer, scope: Scope, context: AnalyzerContext) -> Applicability:
        if context.enabled is not None and analyzer.name not in context.enabled:
            return Applicability(eligible=False, reason="disabled in configuration")

        own_gate = analyzer.applicable(scope, context)
        if not own_gate.eligible:
            return own_gate

        probe = context.capabilities.get(analyzer.name)
        if probe is None:
            return Applicability(
                eligible=False,
                reason=f"no capability probe result for {analyzer.name!r}",
            )
        if not probe.available:
            reason = probe.reason or "capability probe reported unavailable"
            return Applicability(eligible=False, reason=reason)

        return Applicability(eligible=True)
