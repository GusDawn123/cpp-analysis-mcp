"""Where a transcript comes from: a recording on disk, or a session nobody here starts.
The real driver is exercised only as far as the argv it composes. Nothing in this
suite spawns `claude`, and the spend gate below is what keeps it that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.drivers import ALLOWED_TOOLS, FakeDriver, RealDriver, Skip, SpendRefusedError
from evals.tasks import Expectations, Task

TRANSCRIPTS = Path(__file__).parent / "transcripts"


def task(ident: str, *, prompt: str = "do it", needs_project: bool = False) -> Task:
    return Task(
        id=ident,
        title="A task",
        prompt=prompt,
        needs_project=needs_project,
        expected=Expectations(first_tool="review"),
    )


# ------------------------------------------------------------------ fake driver


def test_the_fake_driver_replays_the_recording_named_for_the_task() -> None:
    calls = FakeDriver(transcripts=TRANSCRIPTS).transcript_for(task("review-gate-flow"))

    assert not isinstance(calls, Skip)
    assert [call.name for call in calls] == ["audit", "review", "get_finding"]


def test_a_task_with_no_recording_is_skipped_by_name() -> None:
    outcome = FakeDriver(transcripts=TRANSCRIPTS).transcript_for(task("never-recorded"))

    assert isinstance(outcome, Skip)
    assert "never-recorded" in outcome.reason


def test_the_fake_driver_does_not_care_about_the_project() -> None:
    # the recording already happened; the corpus it ran against is not needed again
    outcome = FakeDriver(transcripts=TRANSCRIPTS).transcript_for(
        task("review-gate-flow", needs_project=True)
    )

    assert not isinstance(outcome, Skip)


# ------------------------------------------------------------------ real driver


def test_the_argv_is_a_headless_stream_json_session() -> None:
    argv = RealDriver(project=None).argv(task("t", prompt="check the book"))

    assert argv[0] == "claude"
    assert "-p" in argv
    assert "check the book" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv


def test_the_argv_allows_exactly_the_servers_tools() -> None:
    argv = RealDriver(project=None).argv(task("t"))

    allowed = argv[argv.index("--allowedTools") + 1].split(",")

    assert set(allowed) == set(ALLOWED_TOOLS)


def test_the_argv_pins_the_session_to_our_server_alone() -> None:
    argv = RealDriver(project=None).argv(task("t"))

    assert "--strict-mcp-config" in argv
    assert "cpp-analysis" in argv[argv.index("--mcp-config") + 1]


def test_the_full_arm_launches_the_shipped_server() -> None:
    argv = RealDriver(project=None, arm="full").argv(task("t"))

    config = argv[argv.index("--mcp-config") + 1]

    assert "cpp-analysis-mcp" in config
    assert "evals.bare_server" not in config


def test_the_bare_arm_launches_the_stripped_wrapper() -> None:
    argv = RealDriver(project=None, arm="bare").argv(task("t"))

    assert "evals.bare_server" in argv[argv.index("--mcp-config") + 1]


def test_the_project_path_lands_in_the_prompt(tmp_path: Path) -> None:
    argv = RealDriver(project=tmp_path).argv(
        task("t", prompt="look at {project}/engine/src/OrderBook.cpp", needs_project=True)
    )

    assert str(tmp_path.as_posix()) in argv[argv.index("-p") + 1]
    assert "{project}" not in argv[argv.index("-p") + 1]


def test_a_task_needing_a_project_is_skipped_when_there_is_none() -> None:
    outcome = RealDriver(project=None).transcript_for(task("t", needs_project=True))

    assert isinstance(outcome, Skip)
    assert "EVAL_PROJECT" in outcome.reason


def test_a_session_without_spend_refuses_rather_than_spawning() -> None:
    with pytest.raises(SpendRefusedError, match="spend=True"):
        RealDriver(project=None).session(task("t"))


def test_the_driver_defaults_to_not_spending() -> None:
    with pytest.raises(SpendRefusedError):
        RealDriver(project=None).transcript_for(task("t"))
