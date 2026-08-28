"""Run the clang-tidy plugin against the real tool and require it to name the planted bug.

The unit suite proves the adapter's shape with scripted outcomes; this proves it holds
against a real toolchain. Same doctrine as the other integration suites: assert the bug
lands on its marked line, and the clean fixture proves an all-clear is earned.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
from helpers import bug_line, cpp_source

from cpp_analysis_mcp import platforms
from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Scope
from cpp_analysis_mcp.analyzers.clang_tidy import ClangTidyAnalyzer
from cpp_analysis_mcp.capabilities import discover_toolchains, probe_all
from cpp_analysis_mcp.pipelines.static_check import check_file
from cpp_analysis_mcp.store.models import Analysis, Severity

pytestmark = pytest.mark.integration

NULLPTR_ZERO = "nullptr_zero"
CLEAN = "clean"
TIDY_CATEGORY = "modernize-use-nullptr"
# the nullptr fixture is silent under tidy's defaults, so the check has to be asked for
NULLPTR_CHECKS = f"-*,{TIDY_CATEGORY}"


def a_real_analyzer(checks: str | None = None) -> ClangTidyAnalyzer:
    """The plugin wired the way the tool surface will wire it: a partial over check_file."""
    host = platforms.detect()
    clangs = [chain for chain in discover_toolchains() if chain.family == "clang"]
    if not clangs:
        pytest.skip("no clang on this machine")
    capabilities = probe_all(clangs[0], host, cache_dir=None)
    if not capabilities[Analysis.CLANG_TIDY].available:
        pytest.skip("clang-tidy is not available on this host")

    check = functools.partial(
        check_file,
        analysis=Analysis.CLANG_TIDY,
        toolchain=clangs[0],
        platform=host,
        capabilities=capabilities,
        checks=checks,
    )
    return ClangTidyAnalyzer(check=check)


def scope_of(fixture: str) -> Scope:
    source = cpp_source(fixture)
    return Scope(project_root=source.parent, files=(source.name,))


def test_the_plugin_names_the_planted_bug_on_its_marked_line() -> None:
    analyzer = a_real_analyzer(checks=NULLPTR_CHECKS)

    findings = analyzer.run(scope_of(NULLPTR_ZERO), AnalyzerContext())

    named = [finding for finding in findings if finding.category == TIDY_CATEGORY]
    assert named, f"expected a {TIDY_CATEGORY} finding, got {findings!r}"
    assert named[0].location is not None
    assert named[0].location.line == bug_line(NULLPTR_ZERO)


def test_the_clean_fixture_earns_its_all_clear() -> None:
    analyzer = a_real_analyzer(checks=NULLPTR_CHECKS)

    findings = analyzer.run(scope_of(CLEAN), AnalyzerContext())

    # notes about check-set choices are fine; errors and warnings are not
    assert all(finding.severity == Severity.NOTE for finding in findings)


def test_the_gate_still_speaks_before_the_real_tool_does() -> None:
    analyzer = a_real_analyzer()

    verdict = analyzer.applicable(
        Scope(project_root=Path("/tmp"), files=("readme.md",)), AnalyzerContext()
    )

    assert not verdict.eligible
    assert verdict.reason is not None
