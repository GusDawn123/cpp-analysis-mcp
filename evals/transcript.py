"""One Claude Code session, reduced to the tool calls it made, in order. stream-json emits
one JSON object per line; only assistant events carry tool_use blocks. Our own tools arrive
MCP-prefixed and lose the prefix here, so tasks name tools the way the server does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MCP_PREFIX = "mcp__cpp-analysis__"


class TranscriptError(ValueError):
    """A transcript that cannot be read as a session. Always names its source."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: Mapping[str, Any]


def read_transcript(path: Path) -> tuple[ToolCall, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as failure:
        raise TranscriptError(f"cannot read transcript {path}: {failure}") from failure
    return parse_stream_json(text, source=str(path))


def parse_stream_json(text: str, *, source: str) -> tuple[ToolCall, ...]:
    """Walk the stream line by line: blank lines are skipped, and a line that is not JSON
    raises TranscriptError naming the source and line number."""
    calls: list[ToolCall] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as failure:
            raise TranscriptError(f"{source}: line {number} is not JSON: {failure}") from failure
        if isinstance(event, dict):
            calls.extend(_calls_in(event, source=source, number=number))
    return tuple(calls)


def _calls_in(event: Mapping[str, Any], *, source: str, number: int) -> Sequence[ToolCall]:
    if event.get("type") != "assistant":
        return ()
    message = event.get("message")
    if not isinstance(message, Mapping):
        return ()
    content = message.get("content")
    if not isinstance(content, Sequence) or isinstance(content, str):
        return ()
    return [
        _call(block, source=source, number=number)
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]


def _call(block: Mapping[str, Any], *, source: str, number: int) -> ToolCall:
    name = block.get("name")
    if not isinstance(name, str) or not name:
        raise TranscriptError(f"{source}: line {number} has an unnamed tool_use block")
    arguments = block.get("input")
    return ToolCall(
        name=name.removeprefix(MCP_PREFIX),
        input=arguments if isinstance(arguments, Mapping) else {},
    )
