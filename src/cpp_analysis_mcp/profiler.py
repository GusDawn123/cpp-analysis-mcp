"""The two perf invocations, composed in one place because two layers need the same pair.

Profiling is a record step and a report step, and the flags of one decide what the other can
say: recording without frame pointers leaves the report with no call graph to attribute
cumulative time through, and reporting without a field separator leaves the parser reading
column positions that move with the widest symbol in the profile. Kept apart, the two drift
and the failure is silent -- a report that parses into fewer hotspots than were measured.

Both the capability probe and the profile pipeline run this pair. The probe sits below
pipelines, so the commands live here rather than in the pipeline that is their main caller.

Spawns nothing itself: these are lists of words, and whoever holds a Runner decides where
they run. That is what lets Windows profile through the WSL bridge without this file knowing.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.parsers.perf import SEPARATOR

PERF = "perf"

# what perf writes and then reads back; named rather than left to perf's default so the
# recording lands in the build directory the caller owns instead of its current directory
DATA_NAME = "perf.data"

# samples per second. 999 rather than 1000 on purpose: a workload doing periodic work on the
# kernel's 1000Hz tick would be sampled at the same phase every time and the profile would
# describe the tick rather than the program.
FREQUENCY = "999"

# rows below this share of the profile are not reported. Under a percent is within the noise
# of any run short enough to wait for, and a real program has a long tail of them that would
# otherwise dominate the response by volume while saying nothing.
PERCENT_LIMIT = 0.5

# said back to the caller, because a threshold nobody was told about reads as completeness
TRUNCATION = (
    f"symbols under {PERCENT_LIMIT}% of the profile are not listed; a program spreading its "
    "time evenly over many small functions will show fewer hotspots than it has"
)

# frame pointers rather than DWARF unwinding: the build already passes
# -fno-omit-frame-pointer, fp costs almost nothing to record, and DWARF mode copies a chunk
# of every thread's stack into the trace, which on a busy workload drops samples instead
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
        # the symbol alone answers "which function"; the source line answers "which line in
        # it", which is the question worth having a profiler for
        "--sort",
        "symbol,srcline",
        "--full-source-path",
        "-t",
        SEPARATOR,
        "--percent-limit",
        str(PERCENT_LIMIT),
    ]
