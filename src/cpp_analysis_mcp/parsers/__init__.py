"""Pure functions: raw tool output in, Finding objects out. No subprocess, no filesystem.

PARSER_FOR is the table that keeps that purity usable: an analysis already implies which
report format its output is in, so a caller holding a run's text picks the reader by name
instead of branching on the sanitizer it asked for.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from cpp_analysis_mcp.models import Analysis, Finding
from cpp_analysis_mcp.parsers import asan, lsan, tsan, ubsan

# one entry per sanitizer analysis; the compile-time analyses read a compiler's own output
# and go through parsers.diagnostics instead
PARSER_FOR: Mapping[Analysis, Callable[[str], Sequence[Finding]]] = {
    Analysis.TSAN: tsan.parse,
    Analysis.ASAN: asan.parse,
    Analysis.LSAN: lsan.parse,
    Analysis.UBSAN: ubsan.parse,
}
