"""Prove plugin #2 fits the same shape -- which is the contract's whole claim. The
clang-tidy suite already proves the shared spine's mechanics; this file pins only what
makes the warnings plugin itself: its name, its refusal words, its failure stage.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Registry, Scope
from cpp_analysis_mcp.analyzers.clang_tidy import ClangTidyAnalyzer
from cpp_analysis_mcp.analyzers.warnings import WarningsAnalyzer
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


def a_parsed_warning() -> Finding:
    return Finding(
        id="diag-0001",
        tool="clang",
        severity=Severity.WARNING,
        category="thread-safety-analysis",
        message="writing variable 'guarded' requires holding mutex 'm' exclusively",
        location=Location(file="src/a.cpp", line=17),
    )


class ScriptedCheck:
    def __init__(self, *outcomes: AnalysisReport | BuildFailure | CapabilityStatus) -> None:
        self.outcomes = list(outcomes)

    def __call__(self, source: Path) -> AnalysisReport | BuildFailure | CapabilityStatus:
        return self.outcomes.pop(0)


def test_the_refusal_speaks_in_this_plugins_words() -> None:
    analyzer = WarningsAnalyzer(check=ScriptedCheck())

    verdict = analyzer.applicable(
        Scope(project_root=ROOT, files=("include/a.hpp",)), AnalyzerContext()
    )

    assert verdict.reason == (
        "no translation units in scope: compiler warnings come from compiling"
    )


def test_parsed_diagnostics_pass_through_with_their_own_tool_intact() -> None:
    parsed = a_parsed_warning()
    report = AnalysisReport(
        analysis=Analysis.THREAD_SAFETY, findings=(parsed,), verified_by="probe"
    )
    analyzer = WarningsAnalyzer(check=ScriptedCheck(report))

    produced = analyzer.run(Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext())

    assert produced.findings == (parsed,)
    # the compiler offers no machine-readable fixes, so this plugin never carries any
    assert produced.suggestions == ()
    assert produced.findings[0].tool == "clang"  # the parser's attribution survives the adapter


def test_a_failed_compile_names_the_thread_safety_stage() -> None:
    failure = BuildFailure(stage="thread-safety", output="a.cpp:2:14: error: expected ';'")
    analyzer = WarningsAnalyzer(check=ScriptedCheck(failure))

    (finding,) = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext()
    ).findings

    assert finding.tool == "compiler-warnings"
    assert finding.id == "compiler-warnings-thread-safety-failed"
    assert finding.category == "thread-safety-failed"


def test_a_stale_capability_confesses_under_this_plugins_name() -> None:
    gone = CapabilityStatus(available=False, reason="gcc offers no -Wthread-safety")
    analyzer = WarningsAnalyzer(check=ScriptedCheck(gone))

    (finding,) = analyzer.run(
        Scope(project_root=ROOT, files=("src/a.cpp",)), AnalyzerContext()
    ).findings

    assert finding.id == "compiler-warnings-unavailable"
    assert finding.message == "gcc offers no -Wthread-safety"


def test_both_plugins_share_one_registry_without_collision() -> None:
    registry = Registry()
    registry.register(ClangTidyAnalyzer(check=ScriptedCheck()))
    registry.register(WarningsAnalyzer(check=ScriptedCheck()))

    context = AnalyzerContext(
        capabilities={
            "clang-tidy": CapabilityStatus(available=True, verified_by="probe"),
            "compiler-warnings": CapabilityStatus(available=True, verified_by="probe"),
        }
    )
    resolutions = registry.resolve(Scope(project_root=ROOT, files=("src/a.cpp",)), context)

    assert [r.analyzer.name for r in resolutions] == ["clang-tidy", "compiler-warnings"]
    assert all(r.verdict.eligible for r in resolutions)
