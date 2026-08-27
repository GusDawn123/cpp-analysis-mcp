"""Prove the clang-tidy plugin honors the contract without ever needing a compiler.

The fake check callable is the whole trick: the pipeline's three ordinary outcomes are
constructed by hand and pushed through the adapter, which must turn every one of them
into findings -- reports pass through, failures speak up, and a detector that stopped
watching says so instead of sounding like clean code. The gates get the same treatment
as the registry's: exact reasons, exact order, no execution during applicability.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Registry, Scope
from cpp_analysis_mcp.analyzers.clang_tidy import ClangTidyAnalyzer
from cpp_analysis_mcp.store.models import (
    Analysis,
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
    Location,
    Severity,
)

ROOT = Path("/repo")


def a_parsed_finding(file: str = "src/a.cpp", line: int = 12) -> Finding:
    return Finding(
        id="tidy-0001",
        tool="clang-tidy",
        severity=Severity.WARNING,
        category="bugprone-use-after-move",
        message="'order' used after it was moved",
        location=Location(file=file, line=line),
    )


def a_report(*findings: Finding, limitations: tuple[str, ...] = ()) -> AnalysisReport:
    return AnalysisReport(
        analysis=Analysis.CLANG_TIDY,
        findings=findings,
        limitations=limitations,
        verified_by="test probe",
    )


class RecordingCheck:
    """A fake pipeline step that serves scripted outcomes and remembers what it saw."""

    def __init__(self, *outcomes: AnalysisReport | BuildFailure | CapabilityStatus) -> None:
        self.outcomes = list(outcomes)
        self.checked: list[Path] = []

    def __call__(self, source: Path) -> AnalysisReport | BuildFailure | CapabilityStatus:
        self.checked.append(source)
        return self.outcomes.pop(0)


# ---------------------------------------------------------------- the gates


def test_a_header_only_scope_is_refused_in_plain_words() -> None:
    analyzer = ClangTidyAnalyzer(check=RecordingCheck())

    verdict = analyzer.applicable(
        Scope(project_root=ROOT, files=("include/a.hpp", "docs/notes.md")),
        AnalyzerContext(),
    )

    assert not verdict.eligible
    assert verdict.reason == "no translation units in scope: clang-tidy analyzes what compiles"


def test_a_known_build_that_contains_none_of_the_scope_is_refused_with_the_count() -> None:
    analyzer = ClangTidyAnalyzer(check=RecordingCheck())

    verdict = analyzer.applicable(
        Scope(project_root=ROOT, files=("src/a.cpp", "src/b.cpp")),
        AnalyzerContext(translation_units=frozenset({"src/other.cpp"})),
    )

    assert not verdict.eligible
    assert verdict.reason == "none of the 2 C++ files in scope are in compile_commands.json"


def test_no_compilation_database_means_the_suffix_gate_decides_alone() -> None:
    analyzer = ClangTidyAnalyzer(check=RecordingCheck())

    verdict = analyzer.applicable(
        Scope(project_root=ROOT, files=("src/a.cpp",)),
        AnalyzerContext(translation_units=frozenset()),
    )

    assert verdict.eligible


def test_one_build_member_in_scope_is_enough() -> None:
    analyzer = ClangTidyAnalyzer(check=RecordingCheck())

    verdict = analyzer.applicable(
        Scope(project_root=ROOT, files=("src/a.cpp", "src/b.cpp")),
        AnalyzerContext(translation_units=frozenset({"src/b.cpp"})),
    )

    assert verdict.eligible


def test_applicability_never_runs_the_tool() -> None:
    check = RecordingCheck()
    analyzer = ClangTidyAnalyzer(check=check)

    analyzer.applicable(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert check.checked == []


# ---------------------------------------------------------------- run outcomes


def test_reports_pass_their_findings_through_untouched() -> None:
    parsed = a_parsed_finding()
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(a_report(parsed)))

    findings = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert findings == (parsed,)


def test_only_translation_units_are_checked_and_each_exactly_once() -> None:
    check = RecordingCheck(a_report(), a_report())
    analyzer = ClangTidyAnalyzer(check=check)

    analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp", "include/a.hpp", "src/b.cc")),
        AnalyzerContext(),
    )

    assert check.checked == [ROOT / "src/a.cpp", ROOT / "src/b.cc"]


def test_a_known_build_screens_run_to_its_members() -> None:
    # the gate passes when one file is a build member; run must then check only that
    # member -- checking the stray would manufacture the missing-include noise the
    # membership gate exists to prevent
    check = RecordingCheck(a_report())
    analyzer = ClangTidyAnalyzer(check=check)

    analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp", "src/stray.cpp")),
        AnalyzerContext(translation_units=frozenset({"src/a.cpp"})),
    )

    assert check.checked == [ROOT / "src/a.cpp"]


def test_pipeline_notes_become_note_findings_ranked_to_speak_last() -> None:
    note = "this project committed no .clang-tidy, so a default check set was used"
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(a_report(limitations=(note,))))

    (finding,) = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert finding.severity == Severity.NOTE
    assert finding.category == "analysis-note"
    assert finding.message == note


def test_a_build_failure_speaks_as_an_error_finding_naming_its_stage() -> None:
    failure = BuildFailure(
        stage="clang-tidy",
        output="error: no checks enabled\nusage: clang-tidy [options]",
    )
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(failure))

    (finding,) = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert finding.severity == Severity.ERROR
    assert finding.category == "clang-tidy-failed"
    assert finding.message == "error: no checks enabled"  # the tool's own first words
    assert finding.location == Location(file="src/a.cpp", line=1)


def test_a_failures_diagnosed_reason_outranks_its_raw_output() -> None:
    failure = BuildFailure(
        stage="clang-tidy",
        output="fatal error: 'project/header.hpp' file not found",
        reason="no compile_commands.json was found near this file",
    )
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(failure))

    (finding,) = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert finding.message == "no compile_commands.json was found near this file"


def test_a_detector_that_stopped_watching_says_so_instead_of_sounding_clean() -> None:
    gone = CapabilityStatus(available=False, reason="clang-tidy is not on PATH")
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(gone))

    (finding,) = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert finding.severity == Severity.ERROR
    assert finding.category == "tool-unavailable"
    assert finding.message == "clang-tidy is not on PATH"


# ---------------------------------------------------------------- conformance


def test_the_plugin_registers_and_resolves_like_any_analyzer() -> None:
    registry = Registry()
    registry.register(ClangTidyAnalyzer(check=RecordingCheck()))

    (resolution,) = registry.resolve(
        Scope(project_root=ROOT, files=("src/a.cpp",)),
        AnalyzerContext(
            capabilities={"clang-tidy": CapabilityStatus(available=True, verified_by="probe")}
        ),
    )

    assert resolution.analyzer.name == "clang-tidy"
    assert resolution.verdict.eligible
