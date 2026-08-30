"""Check the ASan parser against every committed ASan golden.

Each constant below was read out of the golden file it is keyed by. clang prints a
column and gcc does not, so the same bug yields different expectations per toolchain;
nothing here is derived from another platform's output. Headlines are pinned only up
to `at pc`, since re-capturing the same run prints different pc/bp/sp addresses.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from helpers import GOLDEN_DIR

from cpp_analysis_mcp.parsers import asan
from cpp_analysis_mcp.store.models import Severity


@dataclass(frozen=True)
class Expected:
    """What one golden file should parse into."""

    category: str
    # only the front of the headline: pc/bp/sp move with ASLR on every run
    message_prefix: str
    file: str
    line: int
    column: int | None
    alloc_file: str
    alloc_line: int
    alloc_column: int | None


ERROR_PREFIX = "ERROR: AddressSanitizer: "

LINUX_HEAP_OVERFLOW = "/w/tests/fixtures/cpp/heap_overflow.cpp"
LINUX_USE_AFTER_FREE = "/w/tests/fixtures/cpp/use_after_free.cpp"

GOLDENS: dict[str, Expected] = {
    "asan_heap_overflow.darwin-clang.txt": Expected(
        category="heap-buffer-overflow",
        message_prefix="heap-buffer-overflow on address 0x6020000000e0 at pc ",
        file="heap_overflow.cpp",
        line=10,
        column=None,
        alloc_file="heap_overflow.cpp",
        alloc_line=8,
        alloc_column=None,
    ),
    "asan_heap_overflow.linux-clang.txt": Expected(
        category="heap-buffer-overflow",
        message_prefix="heap-buffer-overflow on address 0x502000000020 at pc ",
        file=LINUX_HEAP_OVERFLOW,
        line=10,
        column=17,
        alloc_file=LINUX_HEAP_OVERFLOW,
        alloc_line=8,
        alloc_column=19,
    ),
    "asan_heap_overflow.linux-gcc.txt": Expected(
        category="heap-buffer-overflow",
        message_prefix="heap-buffer-overflow on address 0x502000000020 at pc ",
        file=LINUX_HEAP_OVERFLOW,
        line=10,
        column=None,
        alloc_file=LINUX_HEAP_OVERFLOW,
        alloc_line=8,
        alloc_column=None,
    ),
    "asan_use_after_free.darwin-clang.txt": Expected(
        category="heap-use-after-free",
        message_prefix="heap-use-after-free on address 0x6020000000d4 at pc ",
        file="use_after_free.cpp",
        line=8,
        column=None,
        alloc_file="use_after_free.cpp",
        alloc_line=5,
        alloc_column=None,
    ),
    "asan_use_after_free.linux-clang.txt": Expected(
        category="heap-use-after-free",
        message_prefix="heap-use-after-free on address 0x502000000014 at pc ",
        file=LINUX_USE_AFTER_FREE,
        line=8,
        column=25,
        alloc_file=LINUX_USE_AFTER_FREE,
        alloc_line=5,
        alloc_column=19,
    ),
    "asan_use_after_free.linux-gcc.txt": Expected(
        category="heap-use-after-free",
        message_prefix="heap-use-after-free on address 0x502000000014 at pc ",
        file=LINUX_USE_AFTER_FREE,
        line=8,
        column=None,
        alloc_file=LINUX_USE_AFTER_FREE,
        alloc_line=5,
        alloc_column=None,
    ),
}

CLEAN_GOLDENS = [
    "asan_clean.darwin-clang.txt",
    "asan_clean.linux-clang.txt",
    "asan_clean.linux-gcc.txt",
]

# the two goldens whose allocation stack opens inside libsanitizer's own source
GCC_GOLDENS = ["asan_heap_overflow.linux-gcc.txt", "asan_use_after_free.linux-gcc.txt"]

USE_AFTER_FREE_GOLDENS = [name for name in GOLDENS if "use_after_free" in name]

GARBAGE = "not sanitizer output\njust some log lines\n#0 0xdeadbeef in nowhere\n"


def read(name: str) -> str:
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_each_golden_yields_one_finding(name: str) -> None:
    findings = asan.parse(read(name))

    assert len(findings) == 1
    assert findings[0].id == "asan-1"
    assert findings[0].tool == "asan"
    assert findings[0].severity is Severity.ERROR


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_category_names_the_report_kind(name: str) -> None:
    assert asan.parse(read(name))[0].category == GOLDENS[name].category


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_message_drops_the_error_prefix(name: str) -> None:
    text = read(name)
    headline = next(line for line in text.splitlines() if ERROR_PREFIX in line)
    message = asan.parse(text)[0].message

    assert message.startswith(GOLDENS[name].message_prefix)
    assert message == headline.split(ERROR_PREFIX, 1)[1].rstrip()
    # the pid banner in front of the prefix goes too
    assert not message.startswith("==")


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_location_is_the_first_framed_source_line(name: str) -> None:
    expected = GOLDENS[name]
    location = asan.parse(read(name))[0].location

    assert location is not None
    assert location.file == expected.file
    assert location.line == expected.line
    assert location.column == expected.column


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_allocated_at_comes_from_the_allocation_stack(name: str) -> None:
    expected = GOLDENS[name]
    allocated_at = asan.parse(read(name))[0].allocated_at

    assert allocated_at is not None
    assert allocated_at.file == expected.alloc_file
    assert allocated_at.line == expected.alloc_line
    assert allocated_at.column == expected.alloc_column


@pytest.mark.parametrize("name", GCC_GOLDENS)
def test_allocation_stack_skips_the_sanitizer_runtime(name: str) -> None:
    """gcc's frame #0 is asan_new_delete.cpp; the caller's frame is the useful one."""
    allocated_at = asan.parse(read(name))[0].allocated_at

    assert allocated_at is not None
    assert "libsanitizer" not in allocated_at.file


@pytest.mark.parametrize("name", USE_AFTER_FREE_GOLDENS)
def test_use_after_free_ignores_the_freed_stack(name: str) -> None:
    """The freed-by block sits above the allocation block and must not win."""
    finding = asan.parse(read(name))[0]

    assert finding.allocated_at is not None
    # every use_after_free golden frees on line 6 and allocates on line 5
    assert finding.allocated_at.line == 5


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_thread_and_count_fields_stay_at_their_defaults(name: str) -> None:
    finding = asan.parse(read(name))[0]

    assert finding.threads == ()
    assert finding.occurrences == 1


@pytest.mark.parametrize("name", CLEAN_GOLDENS)
def test_clean_goldens_report_nothing(name: str) -> None:
    assert asan.parse(read(name)) == []


def test_empty_text_reports_nothing() -> None:
    assert asan.parse("") == []


def test_garbage_text_reports_nothing() -> None:
    assert asan.parse(GARBAGE) == []


def test_headline_text_inside_program_output_is_not_a_report() -> None:
    embedded = "log: saw ERROR: AddressSanitizer: heap-buffer-overflow in the wild\n"
    assert asan.parse(embedded) == []


def test_pid_banner_prefix_still_parses() -> None:
    real = "==94551==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1 at pc 0x2\n"
    findings = asan.parse(real)
    assert len(findings) == 1
    assert findings[0].category == "heap-buffer-overflow"


def test_a_windows_drive_survives_the_frame_parse() -> None:
    """A drive-lettered path carries its own colon; the file capture must keep it."""
    report = (
        "==11==ERROR: AddressSanitizer: heap-use-after-free on address 0x602000000010\n"
        "    #0 0x55f in main C:\\Users\\dev\\ws\\snip\\snippet.cpp:1:44\n"
        "==11==ABORTING\n"
    )
    location = asan.parse(report)[0].location

    assert location is not None
    assert location.file == "C:\\Users\\dev\\ws\\snip\\snippet.cpp"
    assert location.line == 1
