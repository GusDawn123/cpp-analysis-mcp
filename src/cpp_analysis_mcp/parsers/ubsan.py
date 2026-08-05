"""Turn UndefinedBehaviorSanitizer output into findings.

UBSan reports one line per trip -- `file:line:col: runtime error: <what happened>` --
optionally followed by a stack. The reporting line carries everything a finding needs,
so the stack below it is not read.
"""

from __future__ import annotations

import re

from ..models import Finding, Location, Severity

TOOL = "ubsan"

RUNTIME_ERROR = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: runtime error: (?P<message>.+?)\s*$"
)

PHRASE_WORD = re.compile(r"[a-z0-9]+")


def parse(text: str) -> list[Finding]:
    """Return one finding per runtime error, in the order they were printed."""
    findings: list[Finding] = []
    for line in text.splitlines():
        match = RUNTIME_ERROR.match(line)
        if match is None:
            continue
        message = match.group("message")
        findings.append(
            Finding(
                id=f"{TOOL}-{len(findings) + 1}",
                tool=TOOL,
                severity=Severity.ERROR,
                category=_category(message),
                message=message,
                location=_location(match),
            )
        )
    return findings


def _category(message: str) -> str:
    """Name the check that fired: the phrase before the colon, kebab-cased."""
    phrase = message.split(":", 1)[0]
    return "-".join(PHRASE_WORD.findall(phrase.lower()))


def _location(match: re.Match[str]) -> Location:
    column = match.group("column")
    return Location(
        file=match.group("file"),
        line=int(match.group("line")),
        column=int(column) if column is not None else None,
    )
