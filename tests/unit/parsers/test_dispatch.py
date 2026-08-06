"""Pin the analysis-to-parser table against the analysis-to-sanitizer one.

The two tables have to agree: an analysis that names a sanitizer to build with and then
has no reader for what that sanitizer prints would build, run, and report nothing found.
Each entry is spelled out by module rather than compared to a computed list, so a table
that dispatched TSan output to the ASan reader fails here.
"""

from __future__ import annotations

from cpp_analysis_mcp.models import SANITIZER_FOR, Analysis
from cpp_analysis_mcp.parsers import PARSER_FOR, asan, lsan, tsan, ubsan


def test_every_analysis_that_needs_a_sanitizer_has_a_reader() -> None:
    assert set(PARSER_FOR) == set(SANITIZER_FOR)


def test_the_compile_time_analyses_are_not_in_here() -> None:
    """These read a compiler's own output, which parsers.diagnostics handles instead."""
    assert Analysis.THREAD_SAFETY not in PARSER_FOR
    assert Analysis.CLANG_TIDY not in PARSER_FOR


def test_each_analysis_dispatches_to_its_own_parser() -> None:
    assert PARSER_FOR[Analysis.TSAN] is tsan.parse
    assert PARSER_FOR[Analysis.ASAN] is asan.parse
    assert PARSER_FOR[Analysis.LSAN] is lsan.parse
    assert PARSER_FOR[Analysis.UBSAN] is ubsan.parse
