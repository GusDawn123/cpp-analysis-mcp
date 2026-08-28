"""Pin the analysis-to-parser table against the analysis-to-sanitizer one.

An analysis with a sanitizer but no reader would build, run, and silently report nothing;
entries are compared by module, not just by key, so mismatched wiring (TSan output routed
to the ASan reader) fails here.
"""

from __future__ import annotations

from cpp_analysis_mcp.parsers import PARSER_FOR, asan, lsan, tsan, ubsan
from cpp_analysis_mcp.store.models import SANITIZER_FOR, Analysis


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
