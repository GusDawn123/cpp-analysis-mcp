"""Turn LeakSanitizer output into findings.

A run reports one record per leaked allocation site, each a `Direct leak`/`Indirect
leak` headline followed by the allocation stack. Each record becomes one Finding.
"""

from __future__ import annotations

import re

from ..store.models import Finding, Location, Severity

TOOL = "lsan"

RECORD = re.compile(r"^(?P<kind>Direct|Indirect) leak of\b")
FRAME = re.compile(r"^\s*#\d+\s+0x[0-9a-f]+\s+(?P<rest>.*)$")

# a frame carries source only when the symbolizer resolved it, and then it trails
# the line: `#1 0x... in main /w/leak.cpp:6:28`. Paths containing spaces truncate
# here: the format is unquoted and function names carry spaces too, so the
# boundary between them cannot be recovered. A drive letter's own colon is the one
# colon a file may keep.
FRAME_SOURCE = re.compile(r"(?P<file>(?:[A-Za-z]:)?[^\s():]+):(?P<line>\d+)(?::(?P<column>\d+))?$")

# gcc resolves its own new/delete interceptors, so a leak stack can open on
# libsanitizer's source. Skip those frames -- the caller's frame is the useful one.
RUNTIME_SOURCE = re.compile(r"(?:^|[/\\])(?:libsanitizer|compiler-rt|sanitizer_common)[/\\]")


def parse(text: str) -> list[Finding]:
    """Return one finding per leak record, in the order they were printed."""
    lines = text.splitlines()
    findings: list[Finding] = []
    for index, line in enumerate(lines):
        headline = line.strip()
        record = RECORD.match(headline)
        if record is None:
            continue
        location = _first_source_frame(_frames_after(lines, index))
        findings.append(
            Finding(
                id=f"{TOOL}-{len(findings) + 1}",
                tool=TOOL,
                severity=Severity.ERROR,
                category=f"{record.group('kind').lower()}-leak",
                message=headline,
                location=location,
                allocated_at=location,
            )
        )
    return findings


def _frames_after(lines: list[str], index: int) -> list[str]:
    """Collect the run of frame lines that follows a record headline."""
    frames: list[str] = []
    for line in lines[index + 1 :]:
        if FRAME.match(line) is None:
            break
        frames.append(line)
    return frames


def _first_source_frame(frames: list[str]) -> Location | None:
    """Return the source position of the first frame that resolved to user code."""
    for line in frames:
        frame = FRAME.match(line)
        if frame is None:
            continue
        location = _location(frame.group("rest"))
        if location is not None:
            return location
    return None


def _location(rest: str) -> Location | None:
    match = FRAME_SOURCE.search(rest)
    if match is None or RUNTIME_SOURCE.search(match.group("file")):
        return None
    column = match.group("column")
    return Location(
        file=match.group("file"),
        line=int(match.group("line")),
        column=int(column) if column is not None else None,
    )
