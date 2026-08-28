"""Turn compiler diagnostic lines into Findings.

gcc and clang both print `file:line:col: severity: message [-Wflag]`, column and flag
each optional. This is where -Wthread-safety findings come from: reported while
compiling, so the build layer parses its own output rather than waiting for a run that
would never mention them. A `note:` elaborates the diagnostic above it and is skipped.
"""

from __future__ import annotations

import re

from cpp_analysis_mcp.store.models import Finding, Location, Severity

TOOL = "compiler"

# what a diagnostic with no [-Wflag] suffix is filed under: gcc omits the flag on
# diagnostics no flag controls, and every finding still needs a category
UNFLAGGED = "diagnostic"

# unguarded_write.cpp:19:5: warning: writing variable 'counter' ... [-Wthread-safety-analysis]
DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: "
    r"(?P<severity>warning|error): (?P<message>.*?)\s*$"
)
# the compiler names the flag that produced the diagnostic, in brackets, at the end
FLAG_SUFFIX = re.compile(r"\s*\[-W(?P<flag>[^\]]+)\]$")

SEVERITIES = {"warning": Severity.WARNING, "error": Severity.ERROR}

# what makes two diagnostic lines the same one: file, line, column, severity, message, flag
Key = tuple[str, int, int | None, Severity, str, str]


def parse(text: str) -> tuple[Finding, ...]:
    """Return one Finding per distinct diagnostic, in the order they were first printed."""
    # a header compiled into several translation units repeats its diagnostic verbatim once
    # per unit; reporting it N times would read as N problems
    counted: dict[Key, int] = {}
    for line in text.splitlines():
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
    flag = FLAG_SUFFIX.search(body)
    column = match.group("column")
    return (
        match.group("file"),
        int(match.group("line")),
        int(column) if column is not None else None,
        SEVERITIES[match.group("severity")],
        # the flag suffix is the category's, not the message's
        FLAG_SUFFIX.sub("", body),
        # -Werror=thread-safety-analysis promotes the severity, but the category is still
        # the flag's: filtering by category must find the finding either way
        flag.group("flag").removeprefix("error=") if flag is not None else UNFLAGGED,
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
