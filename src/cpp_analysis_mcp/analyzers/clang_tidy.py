"""clang-tidy behind the analyzer contract: the first real tool to fit the shape.

This module is deliberately thin. The static_check pipeline already knows how to find
clang-tidy, choose a check set, ride the compilation database, and read what came back;
the plugin's whole job is contract conformance -- gates that explain themselves, and a
run() whose outcomes are all findings. What it refuses to be is a second pipeline.

The check step arrives as a constructor argument rather than being built here: composing
the real one takes a toolchain, a platform, and probe results, all of which are resolved
per-request by the layer that owns them. A default would mean toolchain discovery at
import time, which is how a library grows a hidden global. The wiring hands in a
partially-applied check_file; tests hand in fakes and never need a compiler.

Every outcome of a check is reported as findings, because the contract's callers read
nothing else -- and silence is the one thing this project never fabricates. A build
failure becomes an ERROR finding naming its stage; a stale capability (probed available,
gone by run time) becomes an ERROR finding saying the detector was not watching; the
pipeline's notes -- the chosen default check set, the missing database -- become NOTE
findings, because "what was decided for you" is a fact a reader needs, ranked last.
"""

from collections.abc import Callable
from pathlib import Path

from cpp_analysis_mcp.analyzers.base import (
    AnalyzerContext,
    Applicability,
    CostTier,
    Scope,
    UnitOfWork,
)
from cpp_analysis_mcp.store.models import (
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
    Location,
    Severity,
)

__all__ = ["CPP_SOURCE_SUFFIXES", "CheckFile", "ClangTidyAnalyzer"]

# what clang-tidy can be pointed at: translation units, not headers -- a header is checked
# through the units that include it, and handing tidy a bare .hpp invites a parse of code
# that was never meant to stand alone
CPP_SOURCE_SUFFIXES = (".cpp", ".cc", ".cxx")

# one file in, one of the pipeline's three ordinary outcomes out; the toolchain, platform,
# probes, and check-set choice are already inside the callable
CheckFile = Callable[[Path], AnalysisReport | BuildFailure | CapabilityStatus]


class ClangTidyAnalyzer:
    """The static tier's first plugin: TU-grained, seconds-cheap, compilation-dependent."""

    name = "clang-tidy"
    cost_tier = CostTier.STATIC_SECONDS
    unit_of_work = UnitOfWork.TRANSLATION_UNIT

    def __init__(self, check: CheckFile) -> None:
        self._check = check

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        sources = _sources_in(scope)
        if not sources:
            return Applicability(
                eligible=False,
                reason="no translation units in scope: clang-tidy analyzes what compiles",
            )
        # an empty set means no compilation database is known, and the suffix gate above
        # is all there is to say -- single files and snippets live in that world today
        if context.translation_units and not any(
            file in context.translation_units for file in sources
        ):
            return Applicability(
                eligible=False,
                reason=(
                    f"none of the {len(sources)} C++ files in scope are in compile_commands.json"
                ),
            )
        return Applicability(eligible=True)

    def run(self, scope: Scope, context: AnalyzerContext) -> tuple[Finding, ...]:
        # the same membership rule the gate applied: with a compilation database known,
        # only its members are checked -- handing tidy a file the build never compiles
        # would manufacture exactly the missing-include failures the gate screens for
        sources = _sources_in(scope)
        if context.translation_units:
            sources = tuple(file for file in sources if file in context.translation_units)

        findings: list[Finding] = []
        for file in sources:
            outcome = self._check(scope.project_root / file)
            findings.extend(_as_findings(outcome, file))
        return tuple(findings)


def _sources_in(scope: Scope) -> tuple[str, ...]:
    return tuple(file for file in scope.files if file.endswith(CPP_SOURCE_SUFFIXES))


def _as_findings(
    outcome: AnalysisReport | BuildFailure | CapabilityStatus, file: str
) -> tuple[Finding, ...]:
    """Every outcome becomes findings; nothing is swallowed on the way to the store."""
    if isinstance(outcome, AnalysisReport):
        notes = tuple(
            Finding(
                id=f"clang-tidy-note-{index}",
                tool="clang-tidy",
                severity=Severity.NOTE,
                category="analysis-note",
                message=note,
                location=Location(file=file, line=1),
            )
            for index, note in enumerate(outcome.limitations)
        )
        return (*outcome.findings, *notes)

    if isinstance(outcome, BuildFailure):
        # the tool's own words explain a failure better than a summary; first line here,
        # the full output stays in the failure the legacy surface still reports
        message = outcome.reason or _first_line(outcome.output)
        return (
            Finding(
                id=f"clang-tidy-{outcome.stage}-failed",
                tool="clang-tidy",
                severity=Severity.ERROR,
                category=f"{outcome.stage}-failed",
                message=message,
                location=Location(file=file, line=1),
            ),
        )

    # probed available, gone by run time: the detector was not watching, and saying so
    # is the difference between "no findings" and a false all-clear
    return (
        Finding(
            id="clang-tidy-unavailable",
            tool="clang-tidy",
            severity=Severity.ERROR,
            category="tool-unavailable",
            message=outcome.reason or "capability probe reported unavailable",
            location=Location(file=file, line=1),
        ),
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "the tool produced no output"
