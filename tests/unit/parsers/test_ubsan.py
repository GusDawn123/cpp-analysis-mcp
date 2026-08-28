"""Check the UBSan parser against every committed UBSan golden.

UBSan reports the source position on the runtime-error line itself, so the goldens
agree on line and column and differ only in the path the build saw. The multi-error
case is hand-written in the same format; no captured run trips twice.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from helpers import GOLDEN_DIR

from cpp_analysis_mcp.parsers import ubsan
from cpp_analysis_mcp.store.models import Severity


@dataclass(frozen=True)
class Expected:
    """What one golden file should parse into."""

    category: str
    message: str
    file: str
    line: int
    column: int


OVERFLOW_MESSAGE = "signed integer overflow: 2147483647 + 1 cannot be represented in type 'int'"
DARWIN_SOURCE = (
    "/Users/gustavorosas/Documents/cpp-analysis-mcp/tests/fixtures/cpp/signed_overflow.cpp"
)
LINUX_SOURCE = "/w/tests/fixtures/cpp/signed_overflow.cpp"

GOLDENS: dict[str, Expected] = {
    "ubsan_signed_overflow.darwin-clang.txt": Expected(
        category="signed-integer-overflow",
        message=OVERFLOW_MESSAGE,
        file=DARWIN_SOURCE,
        line=10,
        column=11,
    ),
    "ubsan_signed_overflow.linux-clang.txt": Expected(
        category="signed-integer-overflow",
        message=OVERFLOW_MESSAGE,
        file=LINUX_SOURCE,
        line=10,
        column=11,
    ),
    "ubsan_signed_overflow.linux-gcc.txt": Expected(
        category="signed-integer-overflow",
        message=OVERFLOW_MESSAGE,
        file=LINUX_SOURCE,
        line=10,
        column=11,
    ),
}

CLEAN_GOLDENS = [
    "ubsan_clean.darwin-clang.txt",
    "ubsan_clean.linux-clang.txt",
    "ubsan_clean.linux-gcc.txt",
]

TWO_ERRORS = """/w/src/math.cpp:12:9: runtime error: signed integer overflow: 2 * 2 overflows
    #0 0x1111 in scale() /w/src/math.cpp:12:9
/w/src/math.cpp:20:5: runtime error: load of null pointer of type 'int'
    #0 0x2222 in read_it() /w/src/math.cpp:20:5

SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior /w/src/math.cpp:20:5
"""

GARBAGE = "not sanitizer output\nsome/file.cpp:3:1: warning: unused variable\n"


def read(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_each_golden_yields_one_finding(name: str) -> None:
    findings = ubsan.parse(read(name))

    assert len(findings) == 1
    assert findings[0].id == "ubsan-1"
    assert findings[0].tool == "ubsan"
    assert findings[0].severity is Severity.ERROR


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_category_kebabs_the_error_phrase(name: str) -> None:
    assert ubsan.parse(read(name))[0].category == GOLDENS[name].category


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_message_is_what_follows_runtime_error(name: str) -> None:
    message = ubsan.parse(read(name))[0].message

    assert message == GOLDENS[name].message
    assert "runtime error" not in message


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_location_is_the_prefix_of_the_runtime_error_line(name: str) -> None:
    expected = GOLDENS[name]
    location = ubsan.parse(read(name))[0].location

    assert location is not None
    assert location.file == expected.file
    assert location.line == expected.line
    assert location.column == expected.column


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_unused_fields_stay_at_their_defaults(name: str) -> None:
    finding = ubsan.parse(read(name))[0]

    assert finding.allocated_at is None
    assert finding.threads == ()
    assert finding.occurrences == 1


def test_one_finding_per_runtime_error_numbered_in_report_order() -> None:
    findings = ubsan.parse(TWO_ERRORS)

    assert [finding.id for finding in findings] == ["ubsan-1", "ubsan-2"]
    assert [finding.category for finding in findings] == [
        "signed-integer-overflow",
        "load-of-null-pointer-of-type-int",
    ]
    assert findings[1].location is not None
    assert findings[1].location.line == 20


def test_summary_line_is_not_a_finding() -> None:
    """The SUMMARY line repeats the position but carries no runtime error of its own."""
    findings = ubsan.parse(TWO_ERRORS)

    assert len(findings) == 2


@pytest.mark.parametrize("name", CLEAN_GOLDENS)
def test_clean_goldens_report_nothing(name: str) -> None:
    assert ubsan.parse(read(name)) == []


def test_empty_text_reports_nothing() -> None:
    assert ubsan.parse("") == []


def test_garbage_text_reports_nothing() -> None:
    assert ubsan.parse(GARBAGE) == []
