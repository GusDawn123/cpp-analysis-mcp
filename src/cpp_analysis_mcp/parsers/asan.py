"""Turn AddressSanitizer output into findings.

One report -- headline, error stack, and the allocation stack when ASan kept one --
becomes one Finding. The frame formats differ between clang and gcc and between
macOS and Linux, so the goldens in tests/fixtures/golden are the specification.
"""

from __future__ import annotations

import re

from ..store.models import Finding, Location, Severity

TOOL = "asan"

# anchored: a program printing headline-shaped text mid-line must not fake a report
HEADLINE = re.compile(r"^(?:=+\d+=+\s*)?ERROR: AddressSanitizer: (?P<message>.+?)\s*$")
FRAME = re.compile(r"^\s*#\d+\s+0x[0-9a-f]+\s+(?P<rest>.*)$")

# a frame carries source only when the symbolizer resolved it, and then it trails
# the line: `#1 0x... in main /w/heap_overflow.cpp:8:19`. Paths containing spaces
# truncate here: the format is unquoted and function names carry spaces too, so
# the boundary between them cannot be recovered.
FRAME_SOURCE = re.compile(r"(?P<file>[^\s():]+):(?P<line>\d+)(?::(?P<column>\d+))?$")

# covers both spellings: `allocated by thread T0 here:` and `previously allocated ...`
ALLOCATION_HEADER = re.compile(r"allocated by thread .* here:")

# gcc resolves its own new/delete interceptors, so an allocation stack can open on
# libsanitizer's source. Skip those frames -- the caller's frame is the useful one.
RUNTIME_SOURCE = re.compile(r"(?:^|/)(?:libsanitizer|compiler-rt|sanitizer_common)/")

KIND_WORD = re.compile(r"[a-z0-9]+")


def parse(text: str) -> list[Finding]:
    """Return one finding per AddressSanitizer report, in the order they were printed."""
    reports = _reports(text.splitlines())
    return [
        _finding(number, message, body) for number, (message, body) in enumerate(reports, start=1)
    ]


def _reports(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split the output into one (headline message, body) pair per report."""
    headlines: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = HEADLINE.match(line)
        if match is not None:
            headlines.append((match.group("message"), index))

    reports: list[tuple[str, list[str]]] = []
    for position, (message, start) in enumerate(headlines):
        end = headlines[position + 1][1] if position + 1 < len(headlines) else len(lines)
        reports.append((message, lines[start:end]))
    return reports


def _finding(number: int, message: str, body: list[str]) -> Finding:
    return Finding(
        id=f"{TOOL}-{number}",
        tool=TOOL,
        severity=Severity.ERROR,
        category=_category(message),
        message=message,
        location=_first_source_frame(_error_stack(body)),
        allocated_at=_first_source_frame(_allocation_stack(body)),
    )


def _category(message: str) -> str:
    """Name the report kind: the words the headline puts before the address."""
    head, matched, _ = message.partition(" on address")
    kind = head if matched else message.split(" ", 1)[0]
    return "-".join(KIND_WORD.findall(kind.lower()))


def _error_stack(body: list[str]) -> list[str]:
    """Return the lines up to the first freed-by or allocated-by block."""
    for index, line in enumerate(body):
        if line.rstrip().endswith("here:"):
            return body[:index]
    return body


def _allocation_stack(body: list[str]) -> list[str]:
    """Return the frames under the allocated-by header, empty when there is none."""
    for index, line in enumerate(body):
        if ALLOCATION_HEADER.search(line):
            return _frames_after(body, index)
    return []


def _frames_after(body: list[str], index: int) -> list[str]:
    """Collect the run of frame lines that follows a block header."""
    frames: list[str] = []
    for line in body[index + 1 :]:
        if FRAME.match(line) is None:
            break
        frames.append(line)
    return frames


def _first_source_frame(lines: list[str]) -> Location | None:
    """Return the source position of the first frame that resolved to user code."""
    for line in lines:
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
