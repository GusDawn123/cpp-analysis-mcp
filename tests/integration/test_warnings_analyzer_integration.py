"""Run the warnings plugin against the real compiler and require the planted lock bug.

Same doctrine as the clang-tidy integration suite. The compiler is clang specifically
because -Wthread-safety is clang-only -- the plugin never checks compiler identity itself,
only the capability probe decides what runs.
"""

from __future__ import annotations

import functools

import pytest
from helpers import bug_line, cpp_source

from cpp_analysis_mcp import platforms
from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Scope
from cpp_analysis_mcp.analyzers.warnings import WarningsAnalyzer
from cpp_analysis_mcp.capabilities import discover_toolchains, probe_all
from cpp_analysis_mcp.pipelines.static_check import check_file
from cpp_analysis_mcp.store.models import Analysis

pytestmark = pytest.mark.integration

UNGUARDED_WRITE = "unguarded_write"
THREAD_SAFETY_CATEGORY = "thread-safety-analysis"


def a_real_analyzer() -> WarningsAnalyzer:
    host = platforms.detect()
    clangs = [chain for chain in discover_toolchains() if chain.family == "clang"]
    if not clangs:
        pytest.skip("no clang on this machine")
    capabilities = probe_all(clangs[0], host, cache_dir=None)
    if not capabilities[Analysis.THREAD_SAFETY].available:
        pytest.skip("-Wthread-safety is not available on this host")

    check = functools.partial(
        check_file,
        analysis=Analysis.THREAD_SAFETY,
        toolchain=clangs[0],
        platform=host,
        capabilities=capabilities,
    )
    return WarningsAnalyzer(check=check)


def test_the_plugin_names_the_planted_lock_bug_on_its_marked_line() -> None:
    analyzer = a_real_analyzer()
    source = cpp_source(UNGUARDED_WRITE)

    findings = analyzer.run(
        Scope(project_root=source.parent, files=(source.name,)), AnalyzerContext()
    )

    named = [finding for finding in findings if finding.category == THREAD_SAFETY_CATEGORY]
    assert named, f"expected a {THREAD_SAFETY_CATEGORY} finding, got {findings!r}"
    assert named[0].location is not None
    assert named[0].location.line == bug_line(UNGUARDED_WRITE)
