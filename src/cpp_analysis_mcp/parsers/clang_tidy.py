"""Turn clang-tidy output into Findings. Shares its line shape with parsers.diagnostics
but disagrees about the trailing bracket -- a compiler names the flag, clang-tidy the check
(no `-W`, sometimes several) -- so it is its own module, not a shared reader guessing.
"""

from __future__ import annotations

import re

from cpp_analysis_mcp.store.models import Finding, Location, Severity

TOOL = "clang-tidy"

# what a diagnostic with no bracket is filed under: clang-tidy names the check on almost
# every line, but a finding still needs a category on the lines where it does not
UNNAMED = "diagnostic"

# /w/tests/fixtures/cpp/nullptr_zero.cpp:5:14: warning: use nullptr [modernize-use-nullptr]
DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: "
    r"(?P<severity>warning|error): (?P<message>.*?)\s*$"
)
CHECK_SUFFIX = re.compile(r"\s*\[(?P<checks>[^\]]+)\]$")

# clang-tidy quotes the offending source under each diagnostic, then a caret line, then the
# replacement text a fix-it would write -- all three behind the same `<number> |` gutter.
# Quoted source holding a colon and the word error parses as a diagnostic without this.
CARET_BLOCK = re.compile(r"^\s*\d*\s*\|")

# "error" on purpose: compile errors in checked code arrive as clang-diagnostic-error lines
# and stay findings -- an error carrying a file and a line beats the same text in a blob
SEVERITIES = {"warning": Severity.WARNING, "error": Severity.ERROR}

# what makes two diagnostic lines the same one: file, line, column, severity, message, checks
Key = tuple[str, int, int | None, Severity, str, str]


def parse(text: str) -> tuple[Finding, ...]:
    """Return one Finding per distinct diagnostic, in the order they were first printed."""
    # a header checked from several translation units repeats its diagnostic verbatim once
    # per unit; reporting it N times would read as N problems
    counted: dict[Key, int] = {}
    for line in text.splitlines():
        if CARET_BLOCK.match(line):
            continue
        match = DIAGNOSTIC.match(line)
        if match is None:
            continue
        key = _key(match)
        counted[key] = counted.get(key, 0) + 1

    # dicts keep insertion order, so first-seen order survives the counting
    return tuple(
        _finding(number, key, occurrences)
        for number, (key, occurrences) in enumerate(counted.items(), start=1)
    )


def _key(match: re.Match[str]) -> Key:
    """Split one diagnostic into the fields that decide whether two lines are the same."""
    body = match.group("message")
    checks = CHECK_SUFFIX.search(body)
    column = match.group("column")
    return (
        match.group("file"),
        int(match.group("line")),
        int(column) if column is not None else None,
        SEVERITIES[match.group("severity")],
        # the bracket is the category's, not the message's
        CHECK_SUFFIX.sub("", body),
        # kept whole when it lists several: one line tripped all of them, and splitting
        # would report several findings where clang-tidy observed one thing
        checks.group("checks") if checks is not None else UNNAMED,
    )


def _finding(number: int, key: Key, occurrences: int) -> Finding:
    file, line, column, severity, message, category = key
    return Finding(
        id=f"{TOOL}-{number}",
        tool=TOOL,
        severity=severity,
        category=category,
        message=message,
        location=Location(file=file, line=line, column=column),
        occurrences=occurrences,
    )
