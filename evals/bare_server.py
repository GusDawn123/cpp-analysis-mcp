"""The A/B control arm: the same tools with the teaching cut out of their descriptions.

Wraps the shipped server rather than flagging it -- the product has no "bare" mode and
never will, so the arm lives here. Each description keeps its first sentence, which is
what a tool would say if nobody had thought about how agents pick one.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver.tools.base import Tool

from cpp_analysis_mcp.context import Context
from cpp_analysis_mcp.server import build_server


def first_sentence(text: str) -> str:
    """Cut at the first period that a capital follows, so "e.g." does not end a sentence."""
    for index, char in enumerate(text):
        if char != ".":
            continue
        rest = text[index + 1 :].lstrip()
        if not rest or rest[0].isupper():
            return " ".join(text[: index + 1].split())
    return " ".join(text.split())


def build_bare_server() -> MCPServer[Context]:
    server = build_server()
    for tool in _registered(server):
        server.remove_tool(tool.name)
        server.add_tool(tool.fn, name=tool.name, description=first_sentence(tool.description))
    return server


def _registered(server: MCPServer[Context]) -> list[Tool]:
    # the SDK's public list_tools() is async and drops the callable; re-registering needs it
    return server._tool_manager.list_tools()


def main() -> None:
    build_bare_server().run("stdio")


if __name__ == "__main__":
    main()
