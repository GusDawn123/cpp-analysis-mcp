"""Pin the ThreadSanitizer parser against the committed goldens.

Every expected value here was read out of the golden file it names. clang and gcc
attribute the same planted bug to different lines and report different ops, so each
platform gets its own hardcoded numbers rather than a shared assumption. The parser
only ever sees text: the tests read the goldens, the parser does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import GOLDEN_DIR

from cpp_analysis_mcp.models import (
    AccessOp,
    Finding,
    Frame,
    Location,
    Severity,
    ThreadAccess,
)
from cpp_analysis_mcp.parsers.tsan import parse

# paths as the goldens spell them, kept here so the assertions stay under the line limit
CPP_DIR_IN_GOLDEN = "/w/tests/fixtures/cpp"
DATA_RACE_CPP = f"{CPP_DIR_IN_GOLDEN}/data_race.cpp"
RACE_WITH_LOCK_CPP = f"{CPP_DIR_IN_GOLDEN}/race_with_lock.cpp"
CLANG_GTHR_H = (
    "/usr/bin/../lib/gcc/aarch64-linux-gnu/13/../../../../include/"
    "aarch64-linux-gnu/c++/13/bits/gthr-default.h"
)
GCC_TSAN_INTERCEPTORS_CPP = "../../../../src/libsanitizer/tsan/tsan_interceptors_posix.cpp"

COUNTER = "(anonymous namespace)::counter"

RACE_GOLDENS = (
    "tsan_data_race.darwin-clang.txt",
    "tsan_data_race.linux-clang.txt",
    "tsan_data_race.linux-gcc.txt",
    "tsan_race_with_lock.darwin-clang.txt",
    "tsan_race_with_lock.linux-clang.txt",
    "tsan_race_with_lock.linux-gcc.txt",
)
DEADLOCK_GOLDENS = (
    "tsan_deadlock.linux-clang.txt",
    "tsan_deadlock.linux-gcc.txt",
)
CLEAN_GOLDENS = (
    "tsan_clean.darwin-clang.txt",
    "tsan_clean.linux-clang.txt",
    "tsan_clean.linux-gcc.txt",
)

# one report per golden, counted by hand from the WARNING: lines in each file
EXPECTED_REPORTS = {
    "tsan_data_race.darwin-clang.txt": 1,
    "tsan_data_race.linux-clang.txt": 1,
    "tsan_data_race.linux-gcc.txt": 1,
    "tsan_race_with_lock.darwin-clang.txt": 1,
    "tsan_race_with_lock.linux-clang.txt": 1,
    "tsan_race_with_lock.linux-gcc.txt": 2,
    "tsan_deadlock.linux-clang.txt": 1,
    "tsan_deadlock.linux-gcc.txt": 1,
}


def golden(name: str) -> str:
    """Read a captured sanitizer run; the parser is handed the text, never the path."""
    path: Path = GOLDEN_DIR / name
    assert path.is_file(), f"missing golden {path}"
    return path.read_text(encoding="utf-8")


def only(findings: list[Finding]) -> Finding:
    assert len(findings) == 1, f"expected one finding, got {len(findings)}"
    return findings[0]


def both(finding: Finding) -> tuple[ThreadAccess, ThreadAccess]:
    """Return a race's two access blocks, saying so when the parser found other than two."""
    assert len(finding.threads) == 2, f"expected two access blocks, got {len(finding.threads)}"
    first, second = finding.threads
    return first, second


# --------------------------------------------------------------------------- data race


def test_data_race_darwin_clang() -> None:
    finding = only(parse(golden("tsan_data_race.darwin-clang.txt")))

    assert finding.id == "tsan-1"
    assert finding.tool == "tsan"
    assert finding.severity is Severity.ERROR
    assert finding.category == "data-race"
    assert finding.message == "data race"
    assert finding.location == Location(file="data_race.cpp", line=12)
    assert finding.symbol == COUNTER
    assert finding.allocated_at is None
    assert finding.occurrences == 1

    first, second = both(finding)
    assert (first.thread_id, first.op, first.size) == ("T2", AccessOp.WRITE, 4)
    assert (second.thread_id, second.op, second.size) == ("T1", AccessOp.WRITE, 4)
    assert len(first.frames) == 2
    assert len(second.frames) == 2


def test_data_race_linux_clang() -> None:
    finding = only(parse(golden("tsan_data_race.linux-clang.txt")))

    assert finding.category == "data-race"
    assert finding.message == "data race"
    # clang blames the increment expression and reports a column
    assert finding.location == Location(file=DATA_RACE_CPP, line=12, column=9)
    assert finding.symbol == COUNTER
    assert finding.allocated_at is None

    first, second = both(finding)
    assert (first.thread_id, first.op, first.size) == ("T2", AccessOp.WRITE, 4)
    assert (second.thread_id, second.op, second.size) == ("T1", AccessOp.WRITE, 4)
    assert len(first.frames) == 7
    assert len(second.frames) == 7


def test_data_race_linux_gcc() -> None:
    finding = only(parse(golden("tsan_data_race.linux-gcc.txt")))

    assert finding.category == "data-race"
    assert finding.message == "data race"
    # gcc blames line 11 and reports no column, where clang blames 12:9
    assert finding.location == Location(file=DATA_RACE_CPP, line=11)
    assert finding.symbol == COUNTER

    first, second = both(finding)
    # gcc caught the read side first, clang caught two writes
    assert (first.thread_id, first.op, first.size) == ("T2", AccessOp.READ, 4)
    assert (second.thread_id, second.op, second.size) == ("T1", AccessOp.WRITE, 4)
    assert len(first.frames) == 7
    assert len(second.frames) == 7


# ---------------------------------------------------------------------- race with lock


def test_race_with_lock_darwin_clang() -> None:
    finding = only(parse(golden("tsan_race_with_lock.darwin-clang.txt")))

    assert finding.category == "data-race"
    assert finding.location == Location(file="race_with_lock.cpp", line=17)
    assert finding.symbol == COUNTER

    locked, unlocked = both(finding)
    assert (locked.thread_id, locked.op, locked.size) == ("T1", AccessOp.WRITE, 4)
    assert (unlocked.thread_id, unlocked.op, unlocked.size) == ("T2", AccessOp.WRITE, 4)


def test_race_with_lock_linux_clang() -> None:
    finding = only(parse(golden("tsan_race_with_lock.linux-clang.txt")))

    assert finding.category == "data-race"
    assert finding.location == Location(file=RACE_WITH_LOCK_CPP, line=23, column=9)
    assert finding.symbol == COUNTER

    unlocked, locked = both(finding)
    assert (unlocked.thread_id, unlocked.op, unlocked.size) == ("T2", AccessOp.WRITE, 4)
    assert (locked.thread_id, locked.op, locked.size) == ("T1", AccessOp.WRITE, 4)
    assert len(unlocked.frames) == 7
    assert len(locked.frames) == 7


def test_race_with_lock_linux_gcc() -> None:
    """gcc reported the same bug twice -- once read/write, once write/write."""
    findings = parse(golden("tsan_race_with_lock.linux-gcc.txt"))

    assert len(findings) == 2
    assert [finding.id for finding in findings] == ["tsan-1", "tsan-2"]

    for finding in findings:
        assert finding.category == "data-race"
        assert finding.message == "data race"
        assert finding.location == Location(file=RACE_WITH_LOCK_CPP, line=22)
        assert finding.symbol == COUNTER

    first_ops = [access.op for access in findings[0].threads]
    second_ops = [access.op for access in findings[1].threads]
    assert first_ops == [AccessOp.READ, AccessOp.WRITE]
    assert second_ops == [AccessOp.WRITE, AccessOp.WRITE]


# ------------------------------------------------------------------------- locks held


def test_locks_held_darwin_clang() -> None:
    """Line 3 of the golden carries `(mutexes: write M0)`; line 7 carries nothing."""
    finding = only(parse(golden("tsan_race_with_lock.darwin-clang.txt")))
    locked, unlocked = both(finding)

    assert locked.locks_held == ("M0",)
    assert unlocked.locks_held == ()


def test_locks_held_linux_clang() -> None:
    """Here the annotated access is the second one, not the first."""
    finding = only(parse(golden("tsan_race_with_lock.linux-clang.txt")))
    unlocked, locked = both(finding)

    assert unlocked.locks_held == ()
    assert locked.locks_held == ("M0",)


def test_locks_held_linux_gcc() -> None:
    """Two reports, each annotating only its `Previous write` side."""
    findings = parse(golden("tsan_race_with_lock.linux-gcc.txt"))

    assert [access.locks_held for access in findings[0].threads] == [(), ("M0",)]
    assert [access.locks_held for access in findings[1].threads] == [(), ("M0",)]


@pytest.mark.parametrize(
    "name", ("tsan_data_race.darwin-clang.txt", "tsan_data_race.linux-gcc.txt")
)
def test_plain_data_races_hold_no_locks(name: str) -> None:
    """No mutex is involved in these runs, so no access may claim one."""
    for finding in parse(golden(name)):
        for access in finding.threads:
            assert access.locks_held == ()


def test_mutex_ids_drop_their_qualifiers() -> None:
    """The goldens only ever show one mutex, so cover the list form here."""
    text = (
        "WARNING: ThreadSanitizer: data race (pid=1)\n"
        "  Write of size 8 at 0x1 by thread T3 (mutexes: write M10, read M5):\n"
        "    #0 bump() a.cpp:3\n"
    )

    access = only(parse(text)).threads[0]

    assert access.thread_id == "T3"
    assert access.size == 8
    assert access.locks_held == ("M10", "M5")


# -------------------------------------------------------------------------- deadlock


def test_deadlock_linux_clang() -> None:
    finding = only(parse(golden("tsan_deadlock.linux-clang.txt")))

    assert finding.id == "tsan-1"
    assert finding.category == "lock-order-inversion"
    assert finding.message == "lock-order-inversion (potential deadlock)"
    assert finding.severity is Severity.ERROR
    # first frame carrying file:line anywhere in the report -- #0 is a bare pthread call
    assert finding.location == Location(file=CLANG_GTHR_H, line=749, column=12)
    assert finding.symbol is None
    assert finding.allocated_at is None
    assert finding.threads == ()


def test_deadlock_linux_gcc() -> None:
    finding = only(parse(golden("tsan_deadlock.linux-gcc.txt")))

    assert finding.category == "lock-order-inversion"
    assert finding.message == "lock-order-inversion (potential deadlock)"
    # gcc's #0 does carry a file, so the location lands on the interceptor
    assert finding.location == Location(file=GCC_TSAN_INTERCEPTORS_CPP, line=1341)
    assert finding.symbol is None
    assert finding.threads == ()


@pytest.mark.parametrize("name", DEADLOCK_GOLDENS)
def test_mutex_and_thread_creation_stacks_are_not_accesses(name: str) -> None:
    """`Mutex M0 ... created at:` and `Thread T1 ... created by` stacks are not races."""
    for finding in parse(golden(name)):
        assert finding.threads == ()


# ----------------------------------------------------------------------------- frames


def test_frames_carry_function_and_location() -> None:
    finding = only(parse(golden("tsan_data_race.darwin-clang.txt")))
    frames = finding.threads[0].frames

    assert frames[0] == Frame(
        function="(anonymous namespace)::bump()",
        location=Location(file="data_race.cpp", line=12),
    )
    assert frames[1].location == Location(file="thread.h", line=168)
    assert frames[1].function.startswith("void* std::__1::__thread_proxy")
    # the module+offset suffix is not part of the function name
    assert "0x10000098c" not in frames[1].function


def test_frames_without_file_info_have_no_location() -> None:
    """`#6 <null> <null> (libstdc++.so.6+0xe1adc)` names a function but no source."""
    finding = only(parse(golden("tsan_data_race.linux-clang.txt")))
    last = finding.threads[0].frames[-1]

    assert last.location is None
    assert last.function == "<null>"


def test_frame_functions_keep_their_template_arguments() -> None:
    finding = only(parse(golden("tsan_data_race.linux-gcc.txt")))
    frame = finding.threads[0].frames[4]

    assert frame.function == "std::thread::_Invoker<std::tuple<void (*)()> >::operator()()"
    assert frame.location == Location(file="/usr/include/c++/13/bits/std_thread.h", line=299)


def test_only_the_first_access_block_sets_the_finding_location() -> None:
    """The second access block sits on a different line; the finding must not drift."""
    finding = only(parse(golden("tsan_race_with_lock.linux-clang.txt")))
    first, second = both(finding)

    assert finding.location == first.frames[0].location
    assert second.frames[0].location == Location(file=RACE_WITH_LOCK_CPP, line=17, column=9)


# ------------------------------------------------------------------- report splitting


@pytest.mark.parametrize("name", (*RACE_GOLDENS, *DEADLOCK_GOLDENS))
def test_report_counts_match_the_goldens(name: str) -> None:
    assert len(parse(golden(name))) == EXPECTED_REPORTS[name]


@pytest.mark.parametrize("name", (*RACE_GOLDENS, *DEADLOCK_GOLDENS))
def test_findings_are_numbered_tsan_errors(name: str) -> None:
    findings = parse(golden(name))

    assert [finding.id for finding in findings] == [
        f"tsan-{number}" for number in range(1, len(findings) + 1)
    ]
    for finding in findings:
        assert finding.tool == "tsan"
        assert finding.severity is Severity.ERROR
        assert finding.occurrences == 1
        assert "pid=" not in finding.message


def test_summary_line_is_not_a_finding() -> None:
    text = "SUMMARY: ThreadSanitizer: data race data_race.cpp:12 in bump()\n"

    assert parse(text) == []


def test_a_report_ends_at_its_summary_line() -> None:
    """Lines after SUMMARY: belong to no report, so they cannot join the one before it."""
    text = (
        "WARNING: ThreadSanitizer: data race (pid=9)\n"
        "  Write of size 4 at 0x1 by thread T1:\n"
        "    #0 store() a.cpp:20\n"
        "SUMMARY: ThreadSanitizer: data race a.cpp:20 in store()\n"
        "  Location is global 'not_part_of_the_report' of size 4 at 0x1 (bin+0x1)\n"
    )

    finding = only(parse(text))

    assert finding.symbol is None
    assert len(finding.threads[0].frames) == 1


# -------------------------------------------------------------------- degenerate input


def test_empty_text_yields_no_findings() -> None:
    assert parse("") == []


def test_whitespace_only_text_yields_no_findings() -> None:
    assert parse("\n\n   \n\t\n") == []


def test_garbage_text_yields_no_findings() -> None:
    text = (
        "make[1]: Entering directory '/w/build'\n"
        "[ 50%] Building CXX object CMakeFiles/app.dir/main.cpp.o\n"
        "\x00\x01 not utf-8 shaped at all }{)(*&^%$#@!\n"
        "#0 this looks like a frame but no report claims it a.cpp:1\n"
    )

    assert parse(text) == []


def test_truncated_report_does_not_raise() -> None:
    """A run killed mid-report still has to produce something usable."""
    text = "\n".join(golden("tsan_data_race.linux-gcc.txt").splitlines()[:5])

    finding = only(parse(text))

    assert finding.category == "data-race"
    assert finding.location == Location(file=DATA_RACE_CPP, line=11)
    assert len(finding.threads) == 1
    assert len(finding.threads[0].frames) == 2


def test_headline_without_a_pid_is_kept_whole() -> None:
    assert only(parse("WARNING: ThreadSanitizer: thread leak\n")).message == "thread leak"
    assert only(parse("WARNING: ThreadSanitizer: thread leak\n")).category == "thread-leak"


@pytest.mark.parametrize("name", CLEAN_GOLDENS)
def test_clean_goldens_yield_no_findings(name: str) -> None:
    text = golden(name)

    assert text == ""
    assert parse(text) == []


# ------------------------------------------------------------- forms the goldens miss


def test_main_thread_is_named_main() -> None:
    """No golden races on the main thread, but TSan writes `by main thread`."""
    text = (
        "WARNING: ThreadSanitizer: data race (pid=7)\n"
        "  Read of size 2 at 0x1 by main thread:\n"
        "    #0 main a.cpp:9:3\n"
    )

    access = only(parse(text)).threads[0]

    assert access.thread_id == "main"
    assert access.op is AccessOp.READ
    assert access.size == 2
    assert access.frames[0].location == Location(file="a.cpp", line=9, column=3)


def test_heap_block_allocation_site_is_recorded() -> None:
    """The goldens all race on a global; heap blocks name where they were allocated."""
    text = (
        "WARNING: ThreadSanitizer: data race (pid=8)\n"
        "  Write of size 4 at 0x1 by thread T1:\n"
        "    #0 store() a.cpp:20\n"
        "\n"
        "  Location is heap block of size 16 at 0x1 allocated by main thread:\n"
        "    #0 operator new <null> (libtsan.so+0x1)\n"
        "    #1 make_buffer() a.cpp:5:14\n"
    )

    finding = only(parse(text))

    assert finding.symbol is None
    assert finding.allocated_at == Location(file="a.cpp", line=5, column=14)
