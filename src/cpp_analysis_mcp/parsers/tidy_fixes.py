"""Read clang-tidy's --export-fixes file: the machine-readable edits behind its advice.

YAML because that is the format clang-tidy writes, parsed with safe_load only. Offsets
in it are byte offsets into the file the diagnostic named, so the bytes are needed to
say which line an edit lands on and what it would overwrite.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

import yaml

from cpp_analysis_mcp.store.models import SuggestedFix

__all__ = ["parse"]

# what a file's bytes are read through: the path clang-tidy wrote, None when it cannot
# be read at all -- a file edited or deleted since the check ran
ReadBytes = Callable[[str], bytes | None]


def parse(text: str, read_bytes: ReadBytes) -> tuple[SuggestedFix, ...]:
    """Return one suggestion per diagnostic that offered an applicable edit.

    Every disappointment is silent: a missing key, a malformed document, an offset past
    the end of a file someone has edited since. The finding this belongs to already
    stands on its own, and a fix sliced from the wrong bytes is worse than none.
    """
    try:
        document: Any = yaml.safe_load(text)
    except yaml.YAMLError:
        return ()
    diagnostics = document.get("Diagnostics") if isinstance(document, dict) else None
    if not isinstance(diagnostics, list):
        return ()

    sources = _sources(read_bytes)
    found = (_suggestion(entry, sources) for entry in diagnostics)
    return tuple(fix for fix in found if fix is not None)


@dataclass(frozen=True, slots=True)
class _Source:
    """One file's bytes and the offset of every newline in it.

    The scan happens once per file; each edit then finds its line by bisecting the
    offsets, so a file with many fix-its costs O(n + k log n) rather than a rescan each.
    """

    content: bytes
    breaks: tuple[int, ...]

    def line_of(self, offset: int) -> int:
        return bisect_left(self.breaks, offset) + 1


def _sources(read_bytes: ReadBytes) -> Callable[[str], _Source | None]:
    """Read each file at most once, misses remembered as misses."""
    cache: dict[str, _Source | None] = {}

    def source(file: str) -> _Source | None:
        if file not in cache:
            content = read_bytes(file)
            cache[file] = (
                None if content is None else _Source(content=content, breaks=_newlines(content))
            )
        return cache[file]

    return source


def _newlines(content: bytes) -> tuple[int, ...]:
    found: list[int] = []
    at = content.find(b"\n")
    while at != -1:
        found.append(at)
        at = content.find(b"\n", at + 1)
    return tuple(found)


def _suggestion(entry: Any, sources: Callable[[str], _Source | None]) -> SuggestedFix | None:
    """Turn one diagnostic's first applicable replacement into a suggestion."""
    if not isinstance(entry, dict):
        return None
    check = entry.get("DiagnosticName")
    message = entry.get("DiagnosticMessage")
    if not isinstance(check, str) or not isinstance(message, dict):
        return None
    file = message.get("FilePath")
    spoke_at = message.get("FileOffset")
    replacements = message.get("Replacements")
    if not isinstance(file, str) or not isinstance(replacements, list):
        return None
    # the diagnostic's own offset is the join key back to its finding; without it, an
    # edit could attach to any same-check sibling in the file, which is worse than none
    source = sources(file)
    if source is None or not isinstance(spoke_at, int) or spoke_at < 0:
        return None
    at = source.line_of(spoke_at)

    # only edits in the file the diagnostic named: a suggestion is paired to a finding by
    # that file, so an edit elsewhere cannot be presented under it
    for edit in replacements:
        if not isinstance(edit, dict) or not _same(edit, file):
            continue
        made = _edit(check, file, at, edit, source)
        if made is not None:
            return made
    return None


def _same(edit: dict[str, Any], file: str) -> bool:
    spelled = edit.get("FilePath")
    return isinstance(spelled, str) and PurePath(spelled) == PurePath(file)


def _edit(
    check: str, file: str, at: int, edit: dict[str, Any], source: _Source
) -> SuggestedFix | None:
    offset, length, text = edit.get("Offset"), edit.get("Length"), edit.get("ReplacementText")
    if not isinstance(offset, int) or not isinstance(length, int) or not isinstance(text, str):
        return None
    if offset < 0 or length < 0 or offset + length > len(source.content):
        return None
    return SuggestedFix(
        check=check,
        file=file,
        at=at,
        line=source.line_of(offset),
        replaced=source.content[offset : offset + length].decode("utf-8", errors="replace"),
        replacement=text,
    )
