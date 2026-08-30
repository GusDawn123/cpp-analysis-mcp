"""Where a transcript comes from: a recording on disk, or a headless Claude Code session.

The fake driver replays and costs nothing, so every test uses it. The real driver
composes the whole `claude -p` command and can run it, but only through `spend=True`
-- an eval that spends the maintainer's usage by accident is a bug, not a feature.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from evals.tasks import TOOL_NAMES, Task
from evals.transcript import MCP_PREFIX, ToolCall, parse_stream_json, read_transcript

Arm = Literal["full", "bare"]

REPO_ROOT = Path(__file__).resolve().parent.parent

# the arms differ in one thing only -- how the server describes its tools -- so the
# server name, and therefore every tool name in the transcript, stays the same
SERVER_NAME = "cpp-analysis"

# Read and Glob let the agent look at the corpus; Bash is left out on purpose, since an
# agent that can shell out to clang-tidy is no longer choosing between our tools
ALLOWED_TOOLS = (
    *(f"{MCP_PREFIX}{name}" for name in sorted(TOOL_NAMES)),
    "Read",
    "Glob",
)

# a wedged session must not hold up nineteen others; short tasks, short leash
MAX_TURNS = 12
SESSION_TIMEOUT_S = 900


class SpendRefusedError(RuntimeError):
    """The real driver was asked for a session without being told it may spend."""


@dataclass(frozen=True)
class Skip:
    reason: str


class Driver(Protocol):
    def transcript_for(self, task: Task) -> tuple[ToolCall, ...] | Skip: ...


@dataclass(frozen=True)
class FakeDriver:
    """Replays `<transcripts>/<task id>.json`; a task with no recording is skipped.

    Knows nothing about arms: a recording is whatever it was recorded against, so the
    operator points --transcripts at the directory belonging to the arm they mean.
    """

    transcripts: Path

    def transcript_for(self, task: Task) -> tuple[ToolCall, ...] | Skip:
        recording = self.transcripts / f"{task.id}.json"
        if not recording.is_file():
            return Skip(f"no recorded transcript for {task.id}")
        return read_transcript(recording)


@dataclass(frozen=True)
class RealDriver:
    """Composes -- and, given spend, runs -- one headless Claude Code session per task."""

    project: Path | None
    arm: Arm = "full"
    spend: bool = False

    def argv(self, task: Task) -> tuple[str, ...]:
        return (
            "claude",
            "-p",
            task.prompt_for(self.project),
            "--output-format",
            "stream-json",
            # stream-json under -p refuses to emit without it
            "--verbose",
            "--strict-mcp-config",
            "--mcp-config",
            self.mcp_config(),
            "--allowedTools",
            ",".join(ALLOWED_TOOLS),
            "--max-turns",
            str(MAX_TURNS),
        )

    def mcp_config(self) -> str:
        """The one server this session may talk to, inline so no temp file is left behind."""
        launch = (
            ["python", "-m", "evals.bare_server"] if self.arm == "bare" else ["cpp-analysis-mcp"]
        )
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "command": "uv",
                    "args": ["run", "--directory", str(REPO_ROOT), *launch],
                }
            }
        }
        return json.dumps(config, sort_keys=True)

    def transcript_for(self, task: Task) -> tuple[ToolCall, ...] | Skip:
        if task.needs_project and self.project is None:
            return Skip(f"{task.id} needs a corpus; EVAL_PROJECT is unset")
        return self.session(task, spend=self.spend)

    def session(self, task: Task, *, spend: bool = False) -> tuple[ToolCall, ...]:
        """Spawn one session. Refuses without spend=True: this is the only paid path here."""
        if not spend:
            raise SpendRefusedError(
                f"{task.id} would start a real Claude Code session; pass spend=True to allow it"
            )
        # a list, never a shell string: nothing in a task prompt reaches a shell
        finished = subprocess.run(
            self.argv(task),
            cwd=self.project or REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=SESSION_TIMEOUT_S,
            check=False,
        )
        return parse_stream_json(finished.stdout, source=f"claude -p [{task.id}]")
