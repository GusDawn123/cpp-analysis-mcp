"""The analyzer contract: one shape every tool fits, static or dynamic (layer 3).

clang-tidy reads code in seconds; ThreadSanitizer builds and runs it for minutes. The
layers above must not care. Each analyzer declares what it costs, what unit it works in,
and whether it applies to a given scope -- and the registry turns those declarations
into verdicts the planner can schedule from. Nothing in this module executes a tool:
resolving is layer 3's job, running is layer 4's, and keeping that line is what keeps
an analyzer a plugin instead of a pipeline.

A verdict is never a bare boolean. Every gate that says no says why, in words, because
the plan trace reports skips and "cppcheck skipped: no capability probe result" is the
same honesty the capability report already practices -- an absent tool must be
distinguishable from an unconsulted one.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from cpp_analysis_mcp.store.models import CapabilityStatus, Finding

__all__ = [
    "Analyzer",
    "AnalyzerContext",
    "Applicability",
    "CostTier",
    "Registry",
    "Resolution",
    "Scope",
    "UnitOfWork",
]


class CostTier(StrEnum):
    """What running this analyzer costs, coarsely -- the planner orders cheap-first.

    Two tiers because the planner currently makes exactly one ordering decision with
    them; more tiers than decisions would be taxonomy for its own sake.
    """

    STATIC_SECONDS = "static-seconds"
    DYNAMIC_MINUTES = "dynamic-minutes"


class UnitOfWork(StrEnum):
    """The granularity an analyzer naturally works in, declared rather than assumed.

    clang-tidy wants translation units, sanitizers want build targets, and forcing
    either into per-file calls would be wrong twice. Escalation depends on this too:
    a TARGET-grained analyzer can be pointed at the one target a finding implicates.
    """

    FILE = "file"
    TRANSLATION_UNIT = "translation-unit"
    TARGET = "target"
    PROJECT = "project"


@dataclass(frozen=True, slots=True)
class Scope:
    """The files a run is about, already resolved to concrete paths.

    Resolution -- diff against a ref, an explicit list, the whole project -- happens
    above this layer; analyzers receive the outcome and never touch git.
    """

    project_root: Path
    files: tuple[str, ...]
    # whether a caller pointed at these exact files, as opposed to a scan resolving them.
    # Selection gates -- suffix, build membership -- exist to pick files out of a scan,
    # and there is no picking to do when the user already pointed; capability gates bind
    # either way. The same line black and ESLint each drew half of: explicit naming wins
    # over selection, and nothing wins over a tool that cannot run.
    caller_named: bool = False


_NO_CAPABILITIES: Mapping[str, CapabilityStatus] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class AnalyzerContext:
    """What gates are allowed to know: configuration, the build, and the probes.

    Deliberately not the server's Context -- layer 3 sees only what its gates consult,
    which is also exactly what a test must fake. `enabled=None` means no configuration
    spoke, and everything is enabled; an empty frozenset means configuration spoke and
    said nothing is.
    """

    enabled: frozenset[str] | None = None
    # paths that are genuinely part of the build, from compile_commands.json -- the
    # membership gate for compilation-dependent tools, precomputed to a set so each
    # lookup is O(1) rather than a scan
    translation_units: frozenset[str] = frozenset()
    # probe results keyed by analyzer name; a missing entry is an unconsulted probe,
    # which the capability gate treats as "not available" rather than "probably fine".
    # the factory hands every instance the same immutable empty proxy -- dataclasses
    # refuses an unhashable default outright, and a fresh dict would be pointless churn
    capabilities: Mapping[str, CapabilityStatus] = field(default_factory=lambda: _NO_CAPABILITIES)


@dataclass(frozen=True, slots=True)
class Applicability:
    """A gate verdict with its why. Eligible verdicts carry no reason; refusals must.

    The contract is enforced, not just documented: a reasonless refusal would put a
    silent skip in the plan trace, and a reasoned approval would invite gates to chat.
    """

    eligible: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.eligible == (self.reason is not None):
            raise ValueError("eligible verdicts carry no reason; refusals require one")


class Analyzer(Protocol):
    """What every tool implements -- structurally, no base class to inherit.

    `applicable` holds the tool-specific gates (do these files concern me, are they in
    the build); the registry wraps it with the generic gates it should not have to
    repeat. `run` executes on an already-approved scope and returns findings destined
    for the store.
    """

    name: str
    cost_tier: CostTier
    unit_of_work: UnitOfWork

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability: ...

    def run(self, scope: Scope, context: AnalyzerContext) -> tuple[Finding, ...]: ...


@dataclass(frozen=True, slots=True)
class Resolution:
    """One analyzer's verdict for one scope -- the row a plan trace is made of."""

    analyzer: Analyzer
    verdict: Applicability


class Registry:
    """Registered analyzers, and the gate chain that turns scope into verdicts.

    The chain runs in a fixed order and stops at the first refusal, so the reported
    reason is always the earliest gate that said no: disabled in configuration, then
    the analyzer's own gates, then the capability probes. Resolution never executes
    a tool and touches nothing outside its arguments -- same scope and context, same
    verdicts, every time.
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
