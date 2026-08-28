"""Read `perf report`'s delimited table into Hotspots. Pure: no subprocess, no filesystem.

Splits on a chosen delimiter rather than column widths, because the Symbol column is
padded to the widest demangled C++ name in the profile -- a template-heavy program makes
column positions move with the very profile being read. Anything that fails to parse is
dropped rather than guessed at, so an invented row never competes with a measured one.
"""

from __future__ import annotations

import re

from cpp_analysis_mcp.store.models import Hotspot, Location

# what the pipeline passes to `perf report -t`; the parser and the command that produces its
# input have to agree on this, so it lives here and the pipeline imports it
SEPARATOR = ";"

# "# Samples: 282  of event 'cpu/cycles/P'" -- both numbers a reader needs to weigh the
# table: a ranking built on a few dozen samples is noise wearing a decimal point, and a
# host with no hardware counters silently profiles a timer instead, with the table
# looking the same either way.
HEADER = re.compile(r"^#\s*Samples:\s*([\dKMG]+)\s+of\s+event\s+'([^']*)'", re.MULTILINE)

# perf abbreviates large sample counts in the header; the table itself is percentages, so
# this only ever affects how the count is reported
SCALE = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}

# the symbol column is prefixed with where the code lives: "[.]" userspace, "[k]" kernel.
# Only the ones that are not ordinary userspace are worth telling a reader about.
ORIGIN = re.compile(r"^\[([.kgHu])\]\s*")
ORIGIN_NOTE = {
    "k": "kernel code",
    "g": "guest kernel code",
    "u": "guest userspace code",
    "H": "hypervisor code",
}

# perf marks a frame the optimizer folded into its caller. Such a frame has no self time by
# construction -- the instructions are attributed to whoever inlined it -- so a reader who
# does not know it was inlined sees a 0.00% entry and concludes the function is free.
INLINED = " (inlined)"
INLINED_NOTE = "inlined into its caller, so its cost appears in the caller's self time"

# "probe2.cpp:6". Unresolved locations come back as "??:0", as "symbol+0x1234" when there is
# no source information at all, or as "libc.so.6[7f...]" for a stripped shared object.
SOURCE_LINE = re.compile(r"^(.*):(\d+)$")

# a cumulative percentage over 100 is not a bug in the tool: a recursive function's frames
# each count the whole subtree below them, and so do repeated inlinings of one callee
RECURSION_NOTE = (
    "cumulative share exceeds 100%, which means recursion or repeated inlining counted "
    "parts of this subtree more than once"
)

# children, self, symbol, source:line -- perf appends more (IPC and its coverage) and those
# are read off a hardware feature that virtualized hosts do not have, so they are ignored
FIELDS = 4


def parse(text: str) -> tuple[Hotspot, ...]:
    """Read every table row, hottest self time first.

    Sorted here rather than left in perf's order on purpose. perf ranks by cumulative time,
    which puts `_start` and `main` at the top of every profile ever taken -- true, and never
    the answer to where the time went. Self time is what names the code actually executing.
    """
    found = [spot for line in text.splitlines() if (spot := _row(line)) is not None]
    # descending self, then descending cumulative, then by name so equal rows keep one order
    found.sort(key=lambda spot: (-spot.self_pct, -spot.total_pct, spot.function))
    return tuple(found)


def header(text: str) -> tuple[int, str]:
    """Return how many samples the profile holds and which event they counted.

    (0, "") when the header is absent, which is what a report over an empty or truncated
    perf.data prints. Reported rather than raised: an empty profile is an ordinary outcome
    for a program that exited before the first sample landed.
    """
    matched = HEADER.search(text)
    if matched is None:
        return 0, ""
    return _count(matched.group(1)), matched.group(2)


def _count(digits: str) -> int:
    """Read a sample count, expanding perf's K/M/G suffix when it abbreviated one."""
    if digits[-1] in SCALE:
        return int(digits[:-1]) * SCALE[digits[-1]]
    return int(digits)


def _row(line: str) -> Hotspot | None:
    """Read one table row, or None for headers, blank lines and anything unrecognized."""
    if line.startswith("#") or not line.strip():
        return None
    fields = [field.strip() for field in line.split(SEPARATOR)]
    if len(fields) < FIELDS:
        return None

    percentages = _percentages(fields[0], fields[1])
    if percentages is None:
        return None
    total_pct, self_pct = percentages

    function, notes = _symbol(fields[2])
    if not function:
        return None
    if total_pct > 100.0:
        notes.append(RECURSION_NOTE)

    return Hotspot(
        function=function,
        self_pct=self_pct,
        total_pct=total_pct,
        location=_location(fields[3]),
        note="; ".join(notes) or None,
    )


def _percentages(children: str, own: str) -> tuple[float, float] | None:
    """Read the two percentage columns, or None when this line is not a table row."""
    if not children.endswith("%") or not own.endswith("%"):
        return None
    try:
        return float(children[:-1]), float(own[:-1])
    except ValueError:
        return None


def _symbol(field: str) -> tuple[str, list[str]]:
    """Strip perf's origin prefix and inlined marker off a symbol, keeping both as notes."""
    notes: list[str] = []
    matched = ORIGIN.match(field)
    if matched is not None:
        field = field[matched.end() :]
        note = ORIGIN_NOTE.get(matched.group(1))
        if note is not None:
            notes.append(note)
    if field.endswith(INLINED):
        field = field[: -len(INLINED)]
        notes.append(INLINED_NOTE)
    return field.strip(), notes


def _location(field: str) -> Location | None:
    """Read "file:line", or None for every shape perf uses to mean it does not know.

    Line 0 is one of those shapes: perf prints it when it resolved the object a symbol came
    from but no line inside it, and a location a reader cannot open is worse than none.
    """
    matched = SOURCE_LINE.match(field)
    if matched is None:
        return None
    file, line = matched.group(1), int(matched.group(2))
    if not file or file.startswith("?") or line == 0:
        return None
    return Location(file=file, line=line)
