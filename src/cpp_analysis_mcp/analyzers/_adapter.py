"""The shared spine of the compile-time plugins: gates, screening, and outcome mapping.

Private on purpose -- plugins share this spine, but it isn't contract surface, and
nothing outside analyzers/ may import it. Extracted once a second plugin (compiler
warnings, alongside clang-tidy) needed the same TU-grained, seconds-cheap shape.
"""

from collections.abc import Callable
from pathlib import Path

from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Applicability, Scope
from cpp_analysis_mcp.store.models import (
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
    Location,
    Severity,
)

# what a compile-time check can be pointed at: translation units, not headers -- a header
# is checked through the units that include it, and a bare .hpp was never meant to stand
# alone in front of a compiler
CPP_SOURCE_SUFFIXES = (".cpp", ".cc", ".cxx")

# one file in, one of the pipeline's three ordinary outcomes out; the toolchain, platform,
# probes, and per-tool choices are already inside the callable
CheckFile = Callable[[Path], AnalysisReport | BuildFailure | CapabilityStatus]


def checkable_sources(scope: Scope, context: AnalyzerContext) -> tuple[str, ...]:
    """The files a compile-time plugin may actually check, gate and run agreeing.

    A caller-named scope is checked verbatim: the user pointed at those exact files, and
    a header parses standalone today. For a scan, suffix first; then, when a compilation
    database is known, only its members -- checking a file the build never compiles
    manufactures exactly the missing-include noise the membership gate exists to prevent.
    An empty database means none is known, and the suffixes decide alone.
    """
    if scope.caller_named:
        return scope.files
    sources = tuple(file for file in scope.files if file.endswith(CPP_SOURCE_SUFFIXES))
    if context.translation_units:
        return tuple(file for file in sources if file in context.translation_units)
    return sources


def membership_gate(
    scope: Scope, context: AnalyzerContext, *, no_sources_reason: str
) -> Applicability:
    """The two gates every compile-time plugin shares, refusing in the same order.

    Both are selection gates, so a caller-named scope passes them outright (Scope says
    why); the capability gate is the registry's and binds regardless.
    """
    if scope.caller_named:
        return Applicability(eligible=True)
    sources = tuple(file for file in scope.files if file.endswith(CPP_SOURCE_SUFFIXES))
    if not sources:
        return Applicability(eligible=False, reason=no_sources_reason)
    if not checkable_sources(scope, context):
        return Applicability(
            eligible=False,
            reason=(f"none of the {len(sources)} C++ files in scope are in compile_commands.json"),
        )
    return Applicability(eligible=True)


def as_findings(
    outcome: AnalysisReport | BuildFailure | CapabilityStatus, file: str, tool: str
) -> tuple[Finding, ...]:
    """Every outcome becomes findings; nothing is swallowed on the way to the store."""
    if isinstance(outcome, AnalysisReport):
        notes = tuple(
            Finding(
                id=f"{tool}-note-{index}",
                tool=tool,
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
        # the full output stays with the failure the legacy surface still reports
        return (
            Finding(
                id=f"{tool}-{outcome.stage}-failed",
                tool=tool,
                severity=Severity.ERROR,
                category=f"{outcome.stage}-failed",
                message=outcome.reason or first_line(outcome.output),
                location=Location(file=file, line=1),
            ),
        )

    # probed available, gone by run time: the detector was not watching, and saying so
    # is the difference between "no findings" and a false all-clear
    return (
        Finding(
            id=f"{tool}-unavailable",
            tool=tool,
            severity=Severity.ERROR,
            category="tool-unavailable",
            message=outcome.reason or "capability probe reported unavailable",
            location=Location(file=file, line=1),
        ),
    )


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "the tool produced no output"
