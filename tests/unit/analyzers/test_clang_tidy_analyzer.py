"""Prove the clang-tidy plugin honors the contract without ever needing a compiler.

A fake check callable serves the pipeline's three outcomes by hand: reports pass through,
failures speak up, and a detector that stopped watching says so instead of sounding clean.
Gates get the same treatment as the registry's: exact reasons, exact order, no execution.
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
    SuggestedFix,
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


def a_report(
    *findings: Finding,
    limitations: tuple[str, ...] = (),
    fixes: tuple[SuggestedFix, ...] = (),
) -> AnalysisReport:
    return AnalysisReport(
        analysis=Analysis.CLANG_TIDY,
        findings=findings,
        limitations=limitations,
        suggested_fixes=fixes,
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

    produced = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert produced.findings == (parsed,)


def test_only_translation_units_are_checked_and_each_exactly_once() -> None:
    check = RecordingCheck(a_report(), a_report())
    analyzer = ClangTidyAnalyzer(check=check)

    analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp", "include/a.hpp", "src/b.cc")),
        AnalyzerContext(),
    )

    assert check.checked == [ROOT / "src/a.cpp", ROOT / "src/b.cc"]


def test_a_known_build_screens_run_to_its_members() -> None:
    # gate passes when one file is a build member; run must check only that member --
    # checking the stray would manufacture the missing-include noise the gate exists to prevent
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

    (finding,) = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext()
    ).findings

    assert finding.severity == Severity.NOTE
    assert finding.category == "analysis-note"
    assert finding.message == note


def test_a_build_failure_speaks_as_an_error_finding_naming_its_stage() -> None:
    failure = BuildFailure(
        stage="clang-tidy",
        output="error: no checks enabled\nusage: clang-tidy [options]",
    )
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(failure))

    (finding,) = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext()
    ).findings

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

    (finding,) = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext()
    ).findings

    assert finding.message == "no compile_commands.json was found near this file"


def test_a_detector_that_stopped_watching_says_so_instead_of_sounding_clean() -> None:
    gone = CapabilityStatus(available=False, reason="clang-tidy is not on PATH")
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(gone))

    (finding,) = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext()
    ).findings

    assert finding.severity == Severity.ERROR
    assert finding.category == "tool-unavailable"
    assert finding.message == "clang-tidy is not on PATH"


# ---------------------------------------------------------------- caller-named scopes


def test_a_scope_is_scan_resolved_unless_it_says_otherwise() -> None:
    assert Scope(project_root=ROOT, files=("src/a.cpp",)).caller_named is False


def test_a_caller_named_header_is_eligible() -> None:
    """Selection gates pick files out of a scan; there is no picking when the caller
    pointed at one file. Headers parse standalone today and must keep doing so."""
    analyzer = ClangTidyAnalyzer(check=RecordingCheck())

    verdict = analyzer.applicable(
        Scope(project_root=ROOT, files=("include/a.hpp",), caller_named=True),
        AnalyzerContext(),
    )

    assert verdict.eligible


def test_a_caller_named_file_outside_the_build_is_checked_anyway() -> None:
    check = RecordingCheck(a_report())
    analyzer = ClangTidyAnalyzer(check=check)

    analyzer.run(
        Scope(project_root=ROOT, files=("include/a.hpp",), caller_named=True),
        AnalyzerContext(translation_units=frozenset({"src/other.cpp"})),
    )

    assert check.checked == [ROOT / "include/a.hpp"]


def test_a_caller_named_file_still_answers_to_the_capability_gate() -> None:
    """Pointing at a file overrides selection, never capability: a tool that cannot run
    refuses in the probe's own words no matter how explicitly it was asked."""
    registry = Registry()
    registry.register(ClangTidyAnalyzer(check=RecordingCheck()))

    (resolution,) = registry.resolve(
        Scope(project_root=ROOT, files=("include/a.hpp",), caller_named=True),
        AnalyzerContext(
            capabilities={
                "clang-tidy": CapabilityStatus(available=False, reason="clang-tidy is not on PATH")
            }
        ),
    )

    assert not resolution.verdict.eligible
    assert resolution.verdict.reason == "clang-tidy is not on PATH"


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


# ---------------------------------------------------------------- offered fixes


def a_fix(file: str = "src/a.cpp") -> SuggestedFix:
    return SuggestedFix(
        check="bugprone-use-after-move",
        file=file,
        at=12,
        line=12,
        replaced="order",
        replacement="std::move(order)",
    )


def test_the_fixes_a_report_offered_travel_out_beside_its_findings() -> None:
    offered = a_fix()
    analyzer = ClangTidyAnalyzer(
        check=RecordingCheck(a_report(a_parsed_finding(), fixes=(offered,)))
    )

    produced = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert produced.suggestions == (offered,)


def test_every_checked_file_contributes_the_fixes_it_was_offered() -> None:
    first, second = a_fix("src/a.cpp"), a_fix("src/b.cc")
    check = RecordingCheck(a_report(fixes=(first,)), a_report(fixes=(second,)))
    analyzer = ClangTidyAnalyzer(check=check)

    produced = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp", "src/b.cc")), AnalyzerContext()
    )

    assert produced.suggestions == (first, second)


def test_an_outcome_that_is_not_a_report_offers_nothing() -> None:
    """A failure and a missing tool both still speak as findings; neither can suggest."""
    failure = BuildFailure(stage="clang-tidy", output="error: no checks enabled")
    gone = CapabilityStatus(available=False, reason="clang-tidy is not on PATH")
    analyzer = ClangTidyAnalyzer(check=RecordingCheck(failure, gone))

    produced = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp", "src/b.cc")), AnalyzerContext()
    )

    assert produced.suggestions == ()
    assert len(produced.findings) == 2
