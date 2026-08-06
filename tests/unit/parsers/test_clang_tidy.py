"""Pin the clang-tidy parser against a captured run of the modernize-use-nullptr check.

Every number and every string here was read out of the golden by eye and written down;
nothing is computed from the parser it checks, so a parser that starts reporting a different
line, category or message fails instead of moving the expectation along with it. The
synthetic cases below cover what one clean capture cannot show: the compile errors clang-tidy
files under clang-diagnostic-error, a line that trips several checks at once, and every shape
of noise clang-tidy prints around its diagnostics.
"""

from __future__ import annotations

from pathlib import Path

from helpers import GOLDEN_DIR, bug_line

from cpp_analysis_mcp.models import Location, Severity
from cpp_analysis_mcp.parsers.clang_tidy import parse

CLANG_TIDY_GOLDEN = "clang_tidy_nullptr_zero.linux-clang.txt"

# the path as the golden spells it: clang-tidy always prints absolute paths, and the capture
# ran in a container that mounted the repository at /w
NULLPTR_ZERO_CPP = "/w/tests/fixtures/cpp/nullptr_zero.cpp"

# read off the golden's warning line, bracket excluded: that bracket is the category
NULLPTR_ZERO_MESSAGE = "use nullptr"
NULLPTR_ZERO_CHECK = "modernize-use-nullptr"
NULLPTR_ZERO_LINE = 5
NULLPTR_ZERO_COLUMN = 14

# what clang-tidy calls a compile error in the code it was asked to check
COMPILE_ERROR_CHECK = "clang-diagnostic-error"

# what one line looks like when it trips more than one check at once
TWO_CHECKS = "bugprone-branch-clone,readability-else-after-return"


def golden(name: str) -> str:
    """Read a captured clang-tidy run; the parser is handed the text, never the path."""
    path: Path = GOLDEN_DIR / name
    assert path.is_file(), f"missing golden {path}"
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------------- the golden


def test_the_golden_yields_one_finding() -> None:
    """Five lines of output: the tally, the warning, the source echo, the caret, the fix-it."""
    findings = parse(golden(CLANG_TIDY_GOLDEN))

    assert len(findings) == 1


def test_the_finding_is_pinned_to_the_golden() -> None:
    finding = parse(golden(CLANG_TIDY_GOLDEN))[0]

    assert finding.id == "clang-tidy-1"
    assert finding.tool == "clang-tidy"
    assert finding.severity is Severity.WARNING
    assert finding.category == NULLPTR_ZERO_CHECK
    assert finding.message == NULLPTR_ZERO_MESSAGE
    assert finding.location == Location(
        file=NULLPTR_ZERO_CPP, line=NULLPTR_ZERO_LINE, column=NULLPTR_ZERO_COLUMN
    )
    assert finding.occurrences == 1


def test_the_check_name_leaves_the_message() -> None:
    """The bracket is the category; repeating it in the message says it twice."""
    finding = parse(golden(CLANG_TIDY_GOLDEN))[0]

    assert f"[{NULLPTR_ZERO_CHECK}]" not in finding.message
    assert finding.message == NULLPTR_ZERO_MESSAGE


def test_the_golden_blames_the_marked_bug_line() -> None:
    """End-to-end on the fixture convention: clang-tidy named the line // BUG: sits on."""
    finding = parse(golden(CLANG_TIDY_GOLDEN))[0]

    assert finding.location is not None
    assert finding.location.line == bug_line("nullptr_zero") == NULLPTR_ZERO_LINE


# ------------------------------------------------------------------------ line forms


def test_a_warning_is_categorised_by_the_check_that_fired() -> None:
    """The bracket carries no -W: it names a clang-tidy check, not a compiler flag."""
    text = "a.cpp:12:9: warning: do not use 'else' after 'return' [readability-else-after-return]\n"

    finding = parse(text)[0]

    assert finding.severity is Severity.WARNING
    assert finding.category == "readability-else-after-return"
    assert finding.message == "do not use 'else' after 'return'"
    assert finding.location == Location(file="a.cpp", line=12, column=9)


def test_a_compile_error_is_a_finding_like_any_other() -> None:
    """Code clang-tidy could not compile is reported as a check, so it stays structured."""
    text = f"/w/a.cpp:1:1: error: 'missing.h' file not found [{COMPILE_ERROR_CHECK}]\n"

    finding = parse(text)[0]

    assert finding.severity is Severity.ERROR
    assert finding.category == COMPILE_ERROR_CHECK
    assert finding.message == "'missing.h' file not found"
    assert finding.location == Location(file="/w/a.cpp", line=1, column=1)


def test_several_checks_in_one_bracket_stay_one_category() -> None:
    """One line tripped both checks; splitting would report two things clang-tidy saw once."""
    text = f"a.cpp:7:3: warning: repeated branch body [{TWO_CHECKS}]\n"

    findings = parse(text)

    assert len(findings) == 1
    assert findings[0].category == TWO_CHECKS
    assert findings[0].message == "repeated branch body"


def test_a_diagnostic_with_no_bracket_is_categorised_as_a_diagnostic() -> None:
    """clang-tidy names a check on almost every line; a finding still needs a category."""
    text = "src/main.cpp:88:3: warning: unknown pragma ignored\n"

    finding = parse(text)[0]

    assert finding.category == "diagnostic"
    assert finding.severity is Severity.WARNING
    assert finding.message == "unknown pragma ignored"
    assert finding.location == Location(file="src/main.cpp", line=88, column=3)


def test_a_line_without_a_column_has_no_column() -> None:
    """A missing column is not column 0."""
    text = "a.cpp:7: warning: use nullptr [modernize-use-nullptr]\n"

    finding = parse(text)[0]

    assert finding.location == Location(file="a.cpp", line=7, column=None)
    assert finding.category == "modernize-use-nullptr"


# ----------------------------------------------------------------------------- noise


def test_notes_are_not_findings() -> None:
    """A note elaborates the diagnostic above it; counting one reports the problem twice."""
    text = (
        "/w/a.cpp:3:5: note: expanded from macro 'GUARD'\n"
        "/w/a.cpp:9:1: note: in instantiation of function template specialization\n"
    )

    assert parse(text) == ()


def test_tallies_and_banners_are_not_findings() -> None:
    """Everything clang-tidy prints about its own run, and none of it names a problem."""
    text = (
        "1 warning generated.\n"
        "2 errors generated.\n"
        "Error while processing /w/tests/fixtures/cpp/nullptr_zero.cpp.\n"
        "Found compiler error(s).\n"
        "Suppressed 3 warnings (use -header-filter=.* to display errors from all "
        "non-system headers).\n"
    )

    assert parse(text) == ()


def test_the_caret_block_is_not_a_finding() -> None:
    """The echoed source, the caret under it, and the fix-it's replacement text."""
    text = (
        "    5 |     int* p = 0;  // BUG: a null pointer spelled 0 rather than nullptr\n"
        "      |              ^\n"
        "      |              nullptr\n"
    )

    assert parse(text) == ()


def test_echoed_source_that_looks_like_a_diagnostic_is_still_not_one() -> None:
    """Quoting a line that spells out a diagnostic must not invent a finding from it."""
    text = (
        "/w/a.cpp:9:5: warning: use nullptr [modernize-use-nullptr]\n"
        '    9 |     log("parse.cpp:3:1: error: bad token");\n'
        "      |     ^\n"
    )

    findings = parse(text)

    assert [finding.location for finding in findings] == [
        Location(file="/w/a.cpp", line=9, column=5)
    ]


def test_a_failed_run_reports_its_errors_and_nothing_else() -> None:
    """A realistic transcript: two errors, their notes, and three lines of running commentary."""
    text = (
        f"/w/a.cpp:2:5: error: use of undeclared identifier 'counter' [{COMPILE_ERROR_CHECK}]\n"
        "    2 |     counter = 1;\n"
        "      |     ^\n"
        f"/w/a.cpp:4:1: error: expected '}}' [{COMPILE_ERROR_CHECK}]\n"
        "/w/a.cpp:1:12: note: to match this '{'\n"
        "2 errors generated.\n"
        "Error while processing /w/a.cpp.\n"
        "Found compiler error(s).\n"
    )

    findings = parse(text)

    assert [finding.severity for finding in findings] == [Severity.ERROR, Severity.ERROR]
    assert [finding.category for finding in findings] == [
        COMPILE_ERROR_CHECK,
        COMPILE_ERROR_CHECK,
    ]
    assert [finding.message for finding in findings] == [
        "use of undeclared identifier 'counter'",
        "expected '}'",
    ]


def test_notes_between_warnings_do_not_break_them() -> None:
    text = (
        "a.cpp:12:9: warning: first problem [modernize-use-nullptr]\n"
        "a.cpp:4:5: note: expanded from macro 'NIL'\n"
        "   12 |     int* p = 0;\n"
        "      |              ^\n"
        "b.cpp:31:2: warning: second problem [readability-braces-around-statements]\n"
    )

    findings = parse(text)

    assert [finding.id for finding in findings] == ["clang-tidy-1", "clang-tidy-2"]
    assert [finding.message for finding in findings] == ["first problem", "second problem"]
    assert [finding.category for finding in findings] == [
        "modernize-use-nullptr",
        "readability-braces-around-statements",
    ]


# ------------------------------------------------------------------------ aggregation


def test_identical_lines_aggregate_into_one_finding() -> None:
    """A header checked from three translation units repeats its warning three times."""
    line = "h.hpp:5:1: warning: use nullptr [modernize-use-nullptr]\n"

    findings = parse(line * 3)

    assert len(findings) == 1
    assert findings[0].occurrences == 3
    assert findings[0].id == "clang-tidy-1"


def test_aggregation_keeps_first_seen_order() -> None:
    text = (
        "a.cpp:1:1: warning: first [modernize-use-nullptr]\n"
        "b.cpp:2:2: warning: second [bugprone-branch-clone]\n"
        "a.cpp:1:1: warning: first [modernize-use-nullptr]\n"
        "c.cpp:3:3: warning: third [performance-move-const-arg]\n"
        "b.cpp:2:2: warning: second [bugprone-branch-clone]\n"
        "b.cpp:2:2: warning: second [bugprone-branch-clone]\n"
    )

    findings = parse(text)

    assert [finding.message for finding in findings] == ["first", "second", "third"]
    assert [finding.occurrences for finding in findings] == [2, 3, 1]
    assert [finding.id for finding in findings] == [
        "clang-tidy-1",
        "clang-tidy-2",
        "clang-tidy-3",
    ]


def test_lines_differing_anywhere_stay_separate() -> None:
    """Same message, different position, severity or check: five problems, not one."""
    text = (
        "a.cpp:1:1: warning: same words [one-check]\n"
        "a.cpp:2:1: warning: same words [one-check]\n"
        "a.cpp:1:2: warning: same words [one-check]\n"
        "a.cpp:1:1: error: same words [one-check]\n"
        "a.cpp:1:1: warning: same words [other-check]\n"
    )

    findings = parse(text)

    assert len(findings) == 5
    assert [finding.occurrences for finding in findings] == [1, 1, 1, 1, 1]


# ------------------------------------------------------------------ degenerate input


def test_empty_text_yields_no_findings() -> None:
    assert parse("") == ()


def test_whitespace_only_text_yields_no_findings() -> None:
    assert parse("\n\n   \n\t\n") == ()


def test_a_silent_run_yields_no_findings() -> None:
    """clang-tidy with nothing to say still prints its header lines."""
    text = "Suppressed 0 warnings (use -header-filter=.* to display errors).\n"

    assert parse(text) == ()
