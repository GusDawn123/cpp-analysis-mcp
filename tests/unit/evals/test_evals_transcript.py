"""Normalizing a Claude Code stream-json transcript down to the calls it made. The samples
under transcripts/ are hand-written recordings that also feed the fake driver, so a parser
change that breaks them breaks the harness loudly rather than quietly grading nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.transcript import TranscriptError, parse_stream_json, read_transcript

TRANSCRIPTS = Path(__file__).parent / "transcripts"


def test_reads_calls_in_the_order_the_agent_made_them() -> None:
    calls = read_transcript(TRANSCRIPTS / "escalate-after-clean-static-race.json")

    assert [call.name for call in calls] == ["static_check_file", "sanitize_file"]


def test_strips_the_mcp_prefix_off_our_own_tools() -> None:
    calls = read_transcript(TRANSCRIPTS / "review-gate-flow.json")

    assert [call.name for call in calls] == ["audit", "review", "get_finding"]


def test_keeps_the_parsed_input_of_each_call() -> None:
    calls = read_transcript(TRANSCRIPTS / "escalate-after-clean-static-race.json")

    assert calls[1].input == {"source": "engine/src/OrderBook.cpp", "analysis": "tsan"}


def test_several_tool_uses_in_one_message_stay_in_block_order() -> None:
    calls = read_transcript(TRANSCRIPTS / "full-check-everything-about-this-file.json")

    assert [call.input.get("analysis") for call in calls[3:]] == ["asan", "lsan", "ubsan"]


def test_text_blocks_and_tool_results_are_not_calls() -> None:
    calls = read_transcript(TRANSCRIPTS / "why-slow-file-profiles-first.json")

    assert len(calls) == 2


def test_a_tool_the_server_does_not_own_keeps_its_name() -> None:
    line = (
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"a.cpp"}}]}}'
    )

    calls = parse_stream_json(line, source="inline")

    assert [call.name for call in calls] == ["Read"]


def test_blank_lines_between_events_are_ignored() -> None:
    text = (
        '{"type":"system","subtype":"init"}\n\n'
        '{"type":"assistant","message":{"role":"assistant","content":'
        '[{"type":"tool_use","id":"t1","name":"mcp__cpp-analysis__capabilities","input":{}}]}}\n\n'
    )

    assert [call.name for call in parse_stream_json(text, source="inline")] == ["capabilities"]


def test_a_malformed_line_names_the_source_and_the_line(tmp_path: Path) -> None:
    broken = tmp_path / "half-written.json"
    broken.write_text('{"type":"system"}\nnot json at all\n', encoding="utf-8")

    with pytest.raises(TranscriptError) as raised:
        read_transcript(broken)

    assert "half-written.json" in str(raised.value)
    assert "line 2" in str(raised.value)


def test_a_missing_transcript_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(TranscriptError, match=r"nowhere\.json"):
        read_transcript(tmp_path / "nowhere.json")


def test_a_tool_use_block_without_a_name_is_refused() -> None:
    line = '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use"}]}}'

    with pytest.raises(TranscriptError, match="unnamed tool_use"):
        parse_stream_json(line, source="inline")
