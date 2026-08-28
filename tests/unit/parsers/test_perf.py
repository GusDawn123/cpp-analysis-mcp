"""Read a real perf table, and every shape it uses to say it does not know something.

REPORT below is not written by hand: it is what `perf report --stdio -g none --sort
symbol,srcline --full-source-path -t ';'` printed for a small C++ program on Ubuntu 26.04
under WSL2, trimmed only in the length of the paths. That matters more here than for the
sanitizer parsers, because perf's table is padded to the widest demangled symbol in the
profile and an approximation would be padded differently -- a parser that passed against a
tidy hand-written sample would meet template-heavy C++ and read the wrong columns.
"""

from __future__ import annotations

from cpp_analysis_mcp.parsers.perf import header, parse
from cpp_analysis_mcp.store.models import Hotspot

# captured output. The interesting rows, in order: a hot function with a source line; main;
# three frames perf could not place; and an inlined standard library frame with no self time
REPORT = """\
# To display the perf.data header info, please use --header/--header-only options.
#
#
# Total Lost Samples: 0
#
# Samples: 286  of event 'cpu/cycles/P'
# Event count (approx.): 1137778350
#
# Children;    Self;Symbol           ;Source:Line          ;IPC   [IPC Coverage]
 188.57%; 89.72% ;[.] Book::AddOrder(int, int)  ;/work/probe2.cpp:6  ;-      -
 100.31%; 0.73%  ;[.] main                      ;/work/probe2.cpp:8  ;-      -
 99.58% ; 0.00%  ;[.] __libc_start_main         ;__libc_start_main+128590492467336 ;-      -
 99.58% ; 0.00%  ;[.] _start                    ;??:0                ;-      -
 99.58% ; 0.00%  ;[.] 0x000074f3cea2a601        ;libc.so.6[74f3cea2a601] ;-      -
 8.21%  ; 4.10%  ;[.] std::map<int, int>::operator[](int const&) (inlined) ;/c++/stl_map.h:532 ;- -
 2.15%  ; 2.15%  ;[k] __softirqentry_text_start ;??:0                ;-      -
 1.02%  ; 1.02%  ;[.] Book::AddOrder(int, int)  ;/c++/stl_tree.h:0   ;-      -
"""

EMPTY = """\
# To display the perf.data header info, please use --header/--header-only options.
#
# Samples: 0  of event 'cpu-clock'
#
"""


def by_name(text: str, name: str) -> Hotspot:
    return next(spot for spot in parse(text) if spot.function == name)


def test_the_header_carries_what_makes_the_table_readable() -> None:
    """Both numbers decide whether the percentages under them mean anything."""
    assert header(REPORT) == (286, "cpu/cycles/P")


def test_a_software_event_is_reported_as_itself() -> None:
    """A host with no hardware counters profiles a timer, and the table looks identical.

    Only the event name distinguishes them, so it must survive rather than be normalized.
    """
    assert header(EMPTY) == (0, "cpu-clock")


def test_an_abbreviated_sample_count_is_expanded() -> None:
    """perf shortens large counts in the header; a reader comparing runs needs the number."""
    assert header("# Samples: 12K  of event 'cpu-clock'\n") == (12_000, "cpu-clock")
    assert header("# Samples: 3M  of event 'cpu-clock'\n") == (3_000_000, "cpu-clock")


def test_output_with_no_header_says_so_rather_than_guessing() -> None:
    """What a report over a truncated trace prints. Zero samples is the honest answer."""
    assert header("") == (0, "")
    assert parse("") == ()


def test_the_hottest_self_time_leads_rather_than_the_deepest_stack() -> None:
    """perf ranks by cumulative time, which puts _start and main on top of every profile.

    Those are true and never the answer. Self time names the code actually executing, so
    the ranking is rebuilt on it -- otherwise the first thing a reader sees is the runtime.
    """
    spots = parse(REPORT)

    assert spots[0].function == "Book::AddOrder(int, int)"
    assert spots[0].self_pct == 89.72
    assert spots[0].total_pct == 188.57
    assert [spot.self_pct for spot in spots] == sorted(
        (spot.self_pct for spot in spots), reverse=True
    )


def test_a_resolved_row_carries_the_line_to_open() -> None:
    """The whole reason to sort by srcline: a function name says where to look, a line says
    where to look inside it."""
    spot = parse(REPORT)[0]

    assert spot.location is not None
    assert spot.location.file == "/work/probe2.cpp"
    assert spot.location.line == 6
    assert spot.location.column is None


def test_every_shape_perf_uses_for_unknown_becomes_no_location() -> None:
    """Four of them, and a Location built from any would point somewhere unopenable.

    Line 0 is the subtle one: perf resolved the object but no line inside it, so the file
    is real and the position is not.
    """
    for name in ("__libc_start_main", "_start", "0x000074f3cea2a601"):
        assert by_name(REPORT, name).location is None, name

    line_zero = [spot for spot in parse(REPORT) if spot.self_pct == 1.02]
    assert line_zero and line_zero[0].location is None


def test_an_inlined_frame_says_so_and_keeps_its_real_name() -> None:
    """Its self time is 0 by construction -- the instructions belong to whoever inlined it.

    A reader who does not know that reads the row as a function that costs nothing.
    """
    spot = by_name(REPORT, "std::map<int, int>::operator[](int const&)")

    assert spot.note is not None
    assert "inlined" in spot.note
    assert spot.total_pct == 8.21


def test_a_cumulative_share_over_100_is_explained_rather_than_clamped() -> None:
    """Recursion and repeated inlining both count a subtree more than once. Real output,
    and a reader meeting 188% with no note would read the tool as broken."""
    spot = parse(REPORT)[0]

    assert spot.note is not None
    assert "recursion" in spot.note


def test_kernel_frames_are_marked_as_kernel() -> None:
    """Time in the kernel is time the source in front of you cannot explain."""
    spot = by_name(REPORT, "__softirqentry_text_start")

    assert spot.note is not None
    assert "kernel" in spot.note


def test_the_origin_prefix_never_reaches_the_function_name() -> None:
    """perf prefixes every symbol with where the code lives, and the prefix is not part of
    the name: a caller matching these against source would match nothing at all."""
    assert all(not spot.function.startswith("[") for spot in parse(REPORT))


def test_lines_that_are_not_rows_are_dropped_rather_than_guessed_at() -> None:
    """A profile is a ranking, and a row invented from an unparsed line would compete with
    measured ones for the top of it."""
    noise = "\n".join(
        [
            "# a comment",
            "",
            "   ",
            "not;a;table;row",
            "12.5;oops;[.] fn;a.cpp:1",
            " 50.00%; 50.00% ;[.] real_one;a.cpp:1;-  -",
        ]
    )

    spots = parse(noise)

    assert [spot.function for spot in spots] == ["real_one"]
