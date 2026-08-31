"""The A/B arm: the same tools, with the teaching taken out of their descriptions.
Nothing here starts a server or probes a machine -- registration is all that is
asserted, because registration is the whole difference between the two arms.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver.tools.base import Tool

from cpp_analysis_mcp.context import Context
from cpp_analysis_mcp.server import build_server
from evals.bare_server import build_bare_server, first_sentence


def tools(server: MCPServer[Context]) -> dict[str, Tool]:
    return {tool.name: tool for tool in server._tool_manager.list_tools()}


def test_the_bare_arm_registers_the_same_surface() -> None:
    assert set(tools(build_bare_server())) == set(tools(build_server()))


def test_every_description_is_cut_to_its_first_sentence() -> None:
    for name, tool in tools(build_bare_server()).items():
        assert tool.description.count(".") == 1, name
        assert "\n" not in tool.description, name


def test_the_first_sentence_still_says_what_the_tool_does() -> None:
    bare = tools(build_bare_server())["static_check_file"].description
    full = tools(build_server())["static_check_file"].description

    assert full.startswith(bare)
    assert len(bare) < len(full)


def test_the_ladder_is_what_the_stripping_removes() -> None:
    bare = tools(build_bare_server())["sanitize_file"].description

    assert "static_check_file" not in bare


def test_a_description_with_no_period_survives_whole() -> None:
    assert first_sentence("no period here") == "no period here"


def test_an_abbreviation_does_not_end_the_sentence() -> None:
    # "e.g." and friends would cut a description mid-clause on a naive split
    assert first_sentence("Races 2 to 5 programs, e.g. two rewrites. Then reports.") == (
        "Races 2 to 5 programs, e.g. two rewrites."
    )
