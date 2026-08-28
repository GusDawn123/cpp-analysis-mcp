"""The two perf invocations, kept together because record's flags decide what report can
say: no frame pointers means no call graph, no field separator means the parser reads
column positions that drift with symbol width. Kept apart, they silently drift out of
sync. Spawns nothing itself -- these are argument lists a Runner executes, which is what
lets Windows profile through the WSL bridge without this file knowing.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.parsers.perf import SEPARATOR

PERF = "perf"

# what perf writes and then reads back; named rather than left to perf's default so the
# recording lands in the build directory the caller owns instead of its current directory
DATA_NAME = "perf.data"

# 999, not 1000: a workload doing periodic work on the kernel's 1000Hz tick would sample
# the same phase every time, describing the tick rather than the program.
FREQUENCY = "999"

# rows below this share aren't reported -- under a percent is noise for a short run, and a
# program's long tail of tiny rows would otherwise dominate the response, saying nothing.
PERCENT_LIMIT = 0.5

# said back to the caller, because a threshold nobody was told about reads as completeness
TRUNCATION = (
    f"symbols under {PERCENT_LIMIT}% of the profile are not listed; a program spreading its "
    "time evenly over many small functions will show fewer hotspots than it has"
)

# frame pointers, not DWARF unwinding: the build already passes -fno-omit-frame-pointer,
# fp is nearly free to record, and DWARF copies a chunk of every thread's stack into the
# trace -- on a busy workload, that drops samples instead.
CALL_GRAPH = "fp"


def record_command(binary: Path, data: Path) -> list[str]:
    """Compose the recording run: sample the workload and write the trace beside its build."""
    return [
        PERF,
        "record",
        f"--call-graph={CALL_GRAPH}",
        "-F",
        FREQUENCY,
        "-o",
        str(data),
        # everything after this is the workload, so a binary whose name begins with a dash
        # is run rather than read as a flag
        "--",
        str(binary),
    ]


def report_command(data: Path) -> list[str]:
    """Compose the read-back: one flat table, delimited, with source lines and full paths."""
    return [
        PERF,
        "report",
        "-i",
        str(data),
        # non-interactive; without it perf opens its curses browser and waits for a keypress
        "--stdio",
        # the call graph was recorded to attribute cumulative time, not to be printed: the
        # tree is thousands of lines and the table already carries what it produced
        "-g",
        "none",
        "--sort",
        "symbol,srcline",
        "--full-source-path",
        "-t",
        SEPARATOR,
        "--percent-limit",
        str(PERCENT_LIMIT),
    ]
