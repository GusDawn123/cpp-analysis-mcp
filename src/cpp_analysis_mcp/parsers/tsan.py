"""Turn ThreadSanitizer console output into Findings.

TSan prints one report per race or lock-order inversion, each opening with a
`WARNING: ThreadSanitizer:` headline and closing with a `SUMMARY:` line. Inside a
race report every thread that touched the memory gets an access block -- a header
naming the op, the size and any mutexes that thread held, followed by its stack.
Text in, Findings out: nothing here reads a file or runs a process.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from cpp_analysis_mcp.store.models import AccessOp, Finding, Frame, Location, Severity, ThreadAccess

TOOL = "tsan"

# the symbolizer writes this when it cannot place a frame in a source file
NO_SOURCE = "<null>"

# WARNING: ThreadSanitizer: data race (pid=1234)
HEADLINE = re.compile(r"^\s*WARNING: ThreadSanitizer: (?P<headline>.+?)\s*$")
# the report ends here; the "reported N warnings" tail that follows belongs to no report
SUMMARY = re.compile(r"^\s*SUMMARY: ThreadSanitizer:")
# the pid changes every run, so keeping it would make two runs of one bug read differently
PID_SUFFIX = re.compile(r"\s*\(pid=\d+\)\s*$")

# Previous write of size 4 at 0xab5e401138e8 by thread T1 (mutexes: write M0):
ACCESS = re.compile(
    r"^\s*(?:Previous\s+)?(?:[Aa]tomic\s+)?(?P<op>[Ww]rite|[Rr]ead)\s+of size (?P<size>\d+)"
    r"\s+at\s+0x[0-9a-fA-F]+\s+by\s+(?P<who>.+?):\s*$"
)
THREAD_ID = re.compile(r"^thread (?P<id>T\d+)\b")
MUTEXES = re.compile(r"\(mutexes:(?P<ids>[^)]*)\)")
MUTEX_ID = re.compile(r"\bM\d+\b")

#     #0 bump() data_race.cpp:12:9 (data_race_tsan+0xf48d0) (BuildId: f85b1b0d)
FRAME = re.compile(r"^\s*#\d+\s+(?P<body>.+?)\s*$")
BUILD_ID = re.compile(r"\s*\(BuildId: [^)]*\)\s*$")
MODULE_OFFSET = re.compile(r"\s*\([^()]*\+0x[0-9a-fA-F]+\)\s*$")
FILE_LINE_COLUMN = re.compile(r"^(?P<file>.+):(?P<line>\d+):(?P<column>\d+)$")
FILE_LINE = re.compile(r"^(?P<file>.+):(?P<line>\d+)$")

# Location is global '(anonymous namespace)::counter' of size 4 at 0xab3a5d730020 (bin+0x20020)
GLOBAL_LOCATION = re.compile(r"^\s*Location is global '(?P<name>.*?)'")
# Location is heap block of size 16 at 0x1 allocated by main thread:
HEAP_LOCATION = re.compile(r"^\s*Location is heap block .*allocated by")


def parse(text: str) -> list[Finding]:
    """Return one Finding per `WARNING: ThreadSanitizer:` report in text."""
    return [
        _finding(number, headline, body)
        for number, (headline, body) in enumerate(_split_reports(text), start=1)
    ]


def _split_reports(text: str) -> list[tuple[str, list[str]]]:
    """Cut the output into one (headline, body) pair per report.

    A report runs from its WARNING line to its SUMMARY line, or to the next WARNING
    when a run was killed before TSan printed the summary.
    """
    reports: list[tuple[str, list[str]]] = []
    headline: str | None = None
    body: list[str] = []

    for line in text.splitlines():
        opening = HEADLINE.match(line)
        if opening is not None:
            if headline is not None:
                reports.append((headline, body))
            headline, body = opening.group("headline"), []
        elif headline is None:
            continue
        elif SUMMARY.match(line) is not None:
            reports.append((headline, body))
            headline, body = None, []
        else:
            body.append(line)

    if headline is not None:
        reports.append((headline, body))
    return reports


def _finding(number: int, headline: str, body: Sequence[str]) -> Finding:
    """Build the Finding for one report."""
    message = PID_SUFFIX.sub("", headline)
    accesses = _accesses(body)
    return Finding(
        id=f"{TOOL}-{number}",
        tool=TOOL,
        severity=Severity.ERROR,
        category=_category(message),
        message=message,
        location=_report_location(body, accesses),
        symbol=_global_symbol(body),
        threads=accesses,
        allocated_at=_allocation_site(body),
    )


def _category(message: str) -> str:
    """Kebab-case the headline up to its parenthetical: "data race" -> "data-race"."""
    return message.split(" (")[0].strip().lower().replace(" ", "-")


def _accesses(body: Sequence[str]) -> tuple[ThreadAccess, ...]:
    """Read one ThreadAccess per access block, in the order the report lists them."""
    found: list[ThreadAccess] = []
    for index, line in enumerate(body):
        header = ACCESS.match(line)
        if header is None:
            continue
        who = header.group("who")
        found.append(
            ThreadAccess(
                thread_id=_thread_id(who),
                op=_op(header.group("op")),
                size=int(header.group("size")),
                locks_held=_locks_held(who),
                frames=_frames(body[index + 1 :]),
            )
        )
    return tuple(found)


def _op(word: str) -> AccessOp:
    """Map both `Write` and `Previous write` onto the same op."""
    return AccessOp.WRITE if word.lower() == "write" else AccessOp.READ


def _thread_id(who: str) -> str:
    """Name the thread: `thread T3` -> T3. TSan's only other form is `main thread`."""
    match = THREAD_ID.match(who)
    return match.group("id") if match is not None else "main"


def _locks_held(who: str) -> tuple[str, ...]:
    """Read `(mutexes: write M10, read M5)` as bare ids, dropping the qualifiers."""
    match = MUTEXES.search(who)
    if match is None:
        return ()
    return tuple(MUTEX_ID.findall(match.group("ids")))


def _frames(lines: Sequence[str]) -> tuple[Frame, ...]:
    """Take the run of `#N ...` lines at the top of lines; the stack ends at the first gap."""
    frames: list[Frame] = []
    for line in lines:
        match = FRAME.match(line)
        if match is None:
            break
        frames.append(_frame(match.group("body")))
    return tuple(frames)


def _every_frame(body: Sequence[str]) -> tuple[Frame, ...]:
    """Read every stack line in the report, whichever section it sits under."""
    matches = (FRAME.match(line) for line in body)
    return tuple(_frame(match.group("body")) for match in matches if match is not None)


def _frame(body: str) -> Frame:
    """Split `bump() a.cpp:12:9 (bin+0xf48d0) (BuildId: f8)` into function and source point.

    The function name carries spaces, parens and colons of its own, so the file token is
    taken from the end once the module and build-id suffixes are peeled off.
    """
    trimmed = MODULE_OFFSET.sub("", BUILD_ID.sub("", body))
    function, _, tail = trimmed.rpartition(" ")
    if function and tail == NO_SOURCE:
        return Frame(function=function)

    location = _source_point(tail) if function else None
    if location is None:
        return Frame(function=trimmed)
    return Frame(function=function, location=location)


def _source_point(token: str) -> Location | None:
    """Read `a.cpp:12:9` or `a.cpp:12`; anything else is not a source point.

    clang reports a column and gcc does not, so both forms appear in the goldens.
    """
    with_column = FILE_LINE_COLUMN.match(token)
    if with_column is not None:
        return Location(
            file=with_column.group("file"),
            line=int(with_column.group("line")),
            column=int(with_column.group("column")),
        )

    bare = FILE_LINE.match(token)
    if bare is not None:
        return Location(file=bare.group("file"), line=int(bare.group("line")))
    return None


def _report_location(body: Sequence[str], accesses: Sequence[ThreadAccess]) -> Location | None:
    """Blame the first access for a race, and the first frame naming a file otherwise."""
    if accesses:
        return _first_source_point(accesses[0].frames)
    return _first_source_point(_every_frame(body))


def _first_source_point(frames: Sequence[Frame]) -> Location | None:
    """Return the first frame the symbolizer managed to place in a source file."""
    for frame in frames:
        if frame.location is not None:
            return frame.location
    return None


def _global_symbol(body: Sequence[str]) -> str | None:
    """Name the racing variable from `Location is global 'counter' ...`."""
    for line in body:
        match = GLOBAL_LOCATION.match(line)
        if match is not None:
            return match.group("name")
    return None


def _allocation_site(body: Sequence[str]) -> Location | None:
    """Point at where a raced-on heap block was allocated, when the report says so."""
    for index, line in enumerate(body):
        if HEAP_LOCATION.match(line) is not None:
            return _first_source_point(_frames(body[index + 1 :]))
    return None
