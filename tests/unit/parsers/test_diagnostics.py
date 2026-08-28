"""Pin the compiler-diagnostic parser against a captured -Wthread-safety run.

Every number here was read out of the golden by eye and written down; nothing is computed
from the parser it checks, so a parser that starts reporting a different line, severity or
category fails instead of moving the expectation with it. The synthetic cases below cover
the forms one macOS clang run cannot show: gcc's flagless diagnostics, errors, missing
columns, and the note lines that must never become findings of their own.
"""

from __future__ import annotations

from pathlib import Path

from helpers import GOLDEN_DIR, bug_line

from cpp_analysis_mcp.parsers.diagnostics import parse
from cpp_analysis_mcp.store.models import Location, Severity

THREAD_SAFETY_GOLDEN = "thread_safety_unguarded_write.darwin-clang.txt"

# the path as the golden spells it -- the capture named the source relative to the repo root
UNGUARDED_WRITE_CPP = "tests/fixtures/cpp/unguarded_write.cpp"

# read off line 1 of the golden, flag suffix excluded: that suffix is the category
THREAD_SAFETY_MESSAGE = (
    "writing variable 'counter' requires holding mutex 'counter_mutex' exclusively"
)
THREAD_SAFETY_LINE = 21
THREAD_SAFETY_COLUMN = 5


def golden(name: str) -> str:
    """Read a captured compile; the parser is handed the text, never the path."""
    path: Path = GOLDEN_DIR / name
    assert path.is_file(), f"missing golden {path}"
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------------- the golden


def test_thread_safety_golden_yields_one_finding() -> None:
    """Four lines of output: one diagnostic, the source echo, the caret, and the tally."""
    findings = parse(golden(THREAD_SAFETY_GOLDEN))

    assert len(findings) == 1


def test_thread_safety_finding_is_pinned_to_the_golden() -> None:
    finding = parse(golden(THREAD_SAFETY_GOLDEN))[0]

    assert finding.id == "compiler-1"
    assert finding.tool == "compiler"
    assert finding.severity is Severity.WARNING
    assert finding.category == "thread-safety-analysis"
    assert finding.message == THREAD_SAFETY_MESSAGE
    assert finding.location == Location(
        file=UNGUARDED_WRITE_CPP, line=THREAD_SAFETY_LINE, column=THREAD_SAFETY_COLUMN
    )
    assert finding.occurrences == 1


def test_the_flag_suffix_leaves_the_message() -> None:
    """The bracketed flag is the category; repeating it in the message says it twice."""
    finding = parse(golden(THREAD_SAFETY_GOLDEN))[0]

    assert "[-Wthread-safety-analysis]" not in finding.message
    assert finding.message.endswith("exclusively")


def test_the_golden_blames_the_marked_bug_line() -> None:
    """End-to-end on the fixture convention: clang named the line // BUG: sits on."""
    finding = parse(golden(THREAD_SAFETY_GOLDEN))[0]

    assert finding.location is not None
    assert finding.location.line == bug_line("unguarded_write") == THREAD_SAFETY_LINE


# ------------------------------------------------------------------------ line forms


def test_a_flagged_warning_is_categorised_by_its_flag() -> None:
    text = "a.cpp:12:9: warning: unused variable 'x' [-Wunused-variable]\n"

    finding = parse(text)[0]

    assert finding.severity is Severity.WARNING
    assert finding.category == "unused-variable"
    assert finding.message == "unused variable 'x'"
    assert finding.location == Location(file="a.cpp", line=12, column=9)


def test_a_diagnostic_with_no_flag_is_categorised_as_a_diagnostic() -> None:
    """gcc leaves the suffix off diagnostics no flag controls, and it still needs a name."""
    text = "src/main.cpp:88:3: warning: this decimal constant is unsigned only in ISO C90\n"

    finding = parse(text)[0]

    assert finding.category == "diagnostic"
    assert finding.severity is Severity.WARNING
    assert finding.message == "this decimal constant is unsigned only in ISO C90"
    assert finding.location == Location(file="src/main.cpp", line=88, column=3)


def test_errors_keep_their_severity() -> None:
    text = "a.cpp:4:11: error: use of undeclared identifier 'counter'\n"

    finding = parse(text)[0]

    assert finding.severity is Severity.ERROR
    assert finding.category == "diagnostic"
    assert finding.message == "use of undeclared identifier 'counter'"


def test_a_promoted_warning_is_an_error_filed_under_its_own_flag() -> None:
    """-Werror=flag prints [-Werror=flag]: the severity is promoted, the category is not.

    A category of "error=thread-safety-analysis" would slip past every filter that looks
    for the flag's name, which is exactly how a promoted lock bug would go unreported.
    """
    text = (
        "a.cpp:21:5: error: writing variable 'counter' requires holding mutex "
        "'counter_mutex' exclusively [-Werror=thread-safety-analysis]\n"
    )

    finding = parse(text)[0]

    assert finding.severity is Severity.ERROR
    assert finding.category == "thread-safety-analysis"
    assert finding.message.endswith("exclusively")


def test_a_line_without_a_column_has_no_column() -> None:
    """gcc reports file:line on some diagnostics; a missing column is not column 0."""
    text = "a.cpp:7: warning: control reaches end of non-void function [-Wreturn-type]\n"

    finding = parse(text)[0]

    assert finding.location == Location(file="a.cpp", line=7, column=None)
    assert finding.category == "return-type"


def test_notes_are_not_findings() -> None:
    """A note elaborates the diagnostic above it; counting one reports the problem twice."""
    text = (
        "a.cpp:3:5: note: mutex acquired here\n"
        "a.cpp:9:1: note: in instantiation of function template specialization\n"
    )

    assert parse(text) == ()


def test_notes_between_warnings_do_not_break_them() -> None:
    text = (
        "a.cpp:12:9: warning: first problem [-Wthread-safety-analysis]\n"
        "a.cpp:4:5: note: mutex 'm' acquired here\n"
        "   12 |     counter = 1;\n"
        "      |     ^\n"
        "b.cpp:31:2: warning: second problem [-Wunused-variable]\n"
    )

    findings = parse(text)

    assert [finding.id for finding in findings] == ["compiler-1", "compiler-2"]
    assert [finding.message for finding in findings] == ["first problem", "second problem"]
    assert [finding.category for finding in findings] == [
        "thread-safety-analysis",
        "unused-variable",
    ]


def test_source_echoes_and_tallies_are_not_findings() -> None:
    text = (
        "   21 |     counter = 1;  // BUG: writes a guarded variable with no lock held\n"
        "      |     ^\n"
        "1 warning generated.\n"
        "make[1]: Leaving directory '/w/build'\n"
    )

    assert parse(text) == ()


# ------------------------------------------------------------------------ aggregation


def test_identical_lines_aggregate_into_one_finding() -> None:
    """A header pulled into three translation units repeats its diagnostic three times."""
    line = "h.hpp:5:1: warning: unused parameter 'n' [-Wunused-parameter]\n"

    findings = parse(line * 3)

    assert len(findings) == 1
    assert findings[0].occurrences == 3
    assert findings[0].id == "compiler-1"


def test_aggregation_keeps_first_seen_order() -> None:
    text = (
        "a.cpp:1:1: warning: first [-Wone]\n"
        "b.cpp:2:2: warning: second [-Wtwo]\n"
        "a.cpp:1:1: warning: first [-Wone]\n"
        "c.cpp:3:3: warning: third [-Wthree]\n"
        "b.cpp:2:2: warning: second [-Wtwo]\n"
        "b.cpp:2:2: warning: second [-Wtwo]\n"
    )

    findings = parse(text)

    assert [finding.message for finding in findings] == ["first", "second", "third"]
    assert [finding.occurrences for finding in findings] == [2, 3, 1]
    assert [finding.id for finding in findings] == ["compiler-1", "compiler-2", "compiler-3"]


def test_lines_differing_anywhere_stay_separate() -> None:
    """Same message, different position, severity or flag: three problems, not one."""
    text = (
        "a.cpp:1:1: warning: same words [-Wone]\n"
        "a.cpp:2:1: warning: same words [-Wone]\n"
        "a.cpp:1:2: warning: same words [-Wone]\n"
        "a.cpp:1:1: error: same words [-Wone]\n"
        "a.cpp:1:1: warning: same words [-Wtwo]\n"
    )

    findings = parse(text)

    assert len(findings) == 5
    assert [finding.occurrences for finding in findings] == [1, 1, 1, 1, 1]


# ------------------------------------------------------------------ degenerate input


def test_empty_text_yields_no_findings() -> None:
    assert parse("") == ()


def test_whitespace_only_text_yields_no_findings() -> None:
    assert parse("\n\n   \n\t\n") == ()


def test_output_with_no_diagnostics_yields_no_findings() -> None:
    text = (
        "[ 50%] Building CXX object CMakeFiles/app.dir/main.cpp.o\n"
        "clang: warning: argument unused during compilation: '-pthread'\n"
        "In file included from a.cpp:3:\n"
    )

    # the clang: line names no file:line, so it is not a diagnostic this parser can place
    assert parse(text) == ()
