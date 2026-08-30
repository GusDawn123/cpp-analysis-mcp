"""Check the LSan parser against the committed leak goldens.

LSan only runs on Linux, so there are two goldens. Every constant was read out of
the golden it is keyed by; the multi-record cases below are hand-written in the same
format, since no captured run leaks twice.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from helpers import GOLDEN_DIR

from cpp_analysis_mcp.parsers import lsan
from cpp_analysis_mcp.store.models import Severity


@dataclass(frozen=True)
class Expected:
    """What one golden file should parse into."""

    category: str
    message: str
    file: str
    line: int
    column: int | None


LEAK_SOURCE = "/w/tests/fixtures/cpp/leak.cpp"

GOLDENS: dict[str, Expected] = {
    "lsan_leak.linux-clang.txt": Expected(
        category="direct-leak",
        message="Direct leak of 16 byte(s) in 1 object(s) allocated from:",
        file=LEAK_SOURCE,
        line=6,
        column=28,
    ),
    "lsan_leak.linux-gcc.txt": Expected(
        category="direct-leak",
        message="Direct leak of 16 byte(s) in 1 object(s) allocated from:",
        file=LEAK_SOURCE,
        line=6,
        column=None,
    ),
}

TWO_RECORDS = """
=================================================================
==7==ERROR: LeakSanitizer: detected memory leaks

Direct leak of 32 byte(s) in 2 object(s) allocated from:
    #0 0xaaaa1111 in operator new(unsigned long) (/w/build/app+0x1000)
    #1 0xbbbb2222 in seed() /w/src/pool.cpp:12:9

Indirect leak of 8 byte(s) in 1 object(s) allocated from:
    #0 0xaaaa1111 in operator new(unsigned long) (/w/build/app+0x1000)
    #1 0xcccc3333 in Node::attach() /w/src/pool.cpp:31:5

SUMMARY: LeakSanitizer: 40 byte(s) leaked in 3 allocation(s).
"""

GARBAGE = "not sanitizer output\nleak of my patience\n#0 0xdeadbeef in nowhere\n"


def read(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_each_golden_yields_one_finding(name: str) -> None:
    findings = lsan.parse(read(name))

    assert len(findings) == 1
    assert findings[0].id == "lsan-1"
    assert findings[0].tool == "lsan"
    assert findings[0].severity is Severity.ERROR


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_category_names_the_leak_kind(name: str) -> None:
    assert lsan.parse(read(name))[0].category == GOLDENS[name].category


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_message_is_the_record_headline(name: str) -> None:
    assert lsan.parse(read(name))[0].message == GOLDENS[name].message


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_location_is_the_first_framed_source_line(name: str) -> None:
    expected = GOLDENS[name]
    location = lsan.parse(read(name))[0].location

    assert location is not None
    assert location.file == expected.file
    assert location.line == expected.line
    assert location.column == expected.column


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_allocated_at_repeats_the_location(name: str) -> None:
    finding = lsan.parse(read(name))[0]

    assert finding.allocated_at == finding.location


def test_allocation_stack_skips_the_sanitizer_runtime() -> None:
    """gcc's frame #0 is lsan_interceptors.cpp; the caller's frame is the useful one."""
    location = lsan.parse(read("lsan_leak.linux-gcc.txt"))[0].location

    assert location is not None
    assert "libsanitizer" not in location.file


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_thread_and_count_fields_stay_at_their_defaults(name: str) -> None:
    finding = lsan.parse(read(name))[0]

    assert finding.threads == ()
    assert finding.occurrences == 1


def test_one_finding_per_record_numbered_in_report_order() -> None:
    findings = lsan.parse(TWO_RECORDS)

    assert [finding.id for finding in findings] == ["lsan-1", "lsan-2"]
    assert [finding.category for finding in findings] == ["direct-leak", "indirect-leak"]
    assert findings[1].message == "Indirect leak of 8 byte(s) in 1 object(s) allocated from:"


def test_each_record_keeps_its_own_stack() -> None:
    first, second = lsan.parse(TWO_RECORDS)

    assert first.location is not None
    assert (first.location.file, first.location.line, first.location.column) == (
        "/w/src/pool.cpp",
        12,
        9,
    )
    assert second.location is not None
    assert (second.location.file, second.location.line, second.location.column) == (
        "/w/src/pool.cpp",
        31,
        5,
    )


def test_asan_output_without_leaks_reports_nothing() -> None:
    assert lsan.parse(read("asan_heap_overflow.linux-clang.txt")) == []


def test_empty_text_reports_nothing() -> None:
    assert lsan.parse("") == []


def test_garbage_text_reports_nothing() -> None:
    assert lsan.parse(GARBAGE) == []


def test_a_windows_drive_survives_the_frame_parse() -> None:
    """Same colon trap as ASan's frames: the drive letter must stay on the file."""
    report = (
        "==7==ERROR: LeakSanitizer: detected memory leaks\n"
        "\n"
        "Direct leak of 4 byte(s) in 1 object(s) allocated from:\n"
        "    #0 0x4df in operator new(unsigned long) compiler-rt/asan_new_delete.cpp:95\n"
        "    #1 0x55f in main C:\\Users\\dev\\ws\\leak.cpp:3:13\n"
        "\n"
        "SUMMARY: LeakSanitizer: 4 byte(s) leaked in 1 allocation(s).\n"
    )
    findings = lsan.parse(report)
    location = findings[0].location

    assert location is not None
    assert location.file == "C:\\Users\\dev\\ws\\leak.cpp"
    assert location.line == 3
    assert location.column == 13
