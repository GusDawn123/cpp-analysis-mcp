"""The shared spine of the compile-time plugins: gates, screening, invocation, outcomes.

Private on purpose -- plugins share this spine, but it isn't contract surface, and
nothing outside analyzers/ may import it. Extracted once a second plugin (compiler
warnings, alongside clang-tidy) needed the same TU-grained, seconds-cheap shape.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cpp_analysis_mcp import compile_db, process
from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Applicability, Scope
from cpp_analysis_mcp.store.models import (
    Analysis,
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


STANDARD = "-std=c++20"

# nothing runs the checked program, so this only has to cover parsing one translation
# unit; it is generous next to the seconds either check usually takes
CHECK_TIMEOUT_S = 60

NO_DATABASE_NOTE = (
    "no compile_commands.json was found near this file, so the check ran with no project "
    "include directories; a file that includes a project header will fail to parse"
)

# what clang says when an include could not be resolved, and the only case where the missing
# database is the explanation rather than a guess about someone else's compile error
MISSING_INCLUDE = "file not found"

NO_DATABASE_SUGGESTION = (
    "generate a compilation database and this check will find it by itself: configure with "
    "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON, or run bear -- <your build> for a non-CMake build"
)


@dataclass(frozen=True, slots=True)
class Checked:
    """What one check step produced: which step it was, what it printed, what that parsed to."""

    stage: str
    result: process.RunResult
    findings: tuple[Finding, ...]
    # what the caller has to know to read this result: the check set nobody chose,
    # the include paths that were never found
    notes: tuple[str, ...] = ()
    # whether a compilation database was behind the flags, which decides whether a parse
    # failure is explained by its absence or by the code
    database: Path | None = None


def project_flags(source: Path) -> tuple[Path | None, tuple[str, ...]]:
    """Find this file's compilation database and take the flags it needs to parse.

    Both checks want the same thing and neither can do its job without it: a file that
    includes a project header is unparseable until something says where that header lives,
    and the build already wrote it down.
    """
    database = compile_db.find(source)
    if database is None:
        return None, ()
    return database, compile_db.flags_for(database, source)


def outcome(
    checked: Checked, analysis: Analysis, status: CapabilityStatus
) -> AnalysisReport | BuildFailure:
    """Decide whether what came back is a report or the failure that replaced one."""
    if checked.result.timed_out:
        return BuildFailure(stage=checked.stage, output=checked.result.output, timed_out=True)
    # a nonzero exit with findings behind it is code that does not compile, and clang-tidy
    # files those under clang-diagnostic-error like any other check -- structured beats a
    # text blob. A nonzero exit with nothing parsed is the tool itself failing, and that
    # output is the only thing that explains it.
    if checked.result.exit_code != 0 and not checked.findings:
        # an unresolved include with no database behind the check is the one failure this
        # layer can explain and fix; every other one belongs to the code and the tool's
        # own words are the answer, so no reason is invented for it
        unresolved = checked.database is None and MISSING_INCLUDE in checked.result.output
        return BuildFailure(
            stage=checked.stage,
            output=checked.result.output,
            reason=NO_DATABASE_NOTE if unresolved else None,
            suggestion=NO_DATABASE_SUGGESTION if unresolved else None,
        )

    return AnalysisReport(
        analysis=analysis,
        findings=checked.findings,
        build_warnings=(),
        exit_code=checked.result.exit_code,
        timed_out=False,
        # the platform's caveats and this run's own, which are about what was decided for
        # the caller rather than about the machine
        limitations=(*status.limitations, *checked.notes),
        verified_by=status.verified_by,
    )
