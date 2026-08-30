"""Task files: what the agent is asked, and what its calls have to look like.

Five check kinds and no more -- a vocabulary small enough that every task says
something an agent could plausibly get wrong. Loading is strict: an unknown key or a
tool name the server does not have is refused, because a check nobody implements
grades green forever.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TASK_DIR = Path(__file__).parent / "tasks"

# the eval's own copy of the tool surface, deliberately not imported from the server:
# a harness that silently follows a rename stops measuring the thing it was written for
TOOL_NAMES = frozenset(
    {
        "audit",
        "benchmark_variants",
        "capabilities",
        "full_check_file",
        "get_finding",
        "profile_file",
        "profile_project",
        "review",
        "sanitize_file",
        "sanitize_project",
        "sanitize_snippet",
        "static_check_file",
        "static_check_snippet",
    }
)

TASK_KEYS = frozenset({"id", "title", "prompt", "needs_project", "expected"})
CHECK_KINDS = ("first_tool", "calls", "never", "after_clean_escalates_to", "max_calls")
ESCALATION_KEYS = frozenset({"after", "escalates_to"})

# tasks written against the maintainer's orderbook corpus spell its root this way
PROJECT_TOKEN = "{project}"


class TaskError(ValueError):
    """A task file that cannot be trusted. Always names the file."""


@dataclass(frozen=True)
class Escalation:
    after: str
    escalates_to: str


@dataclass(frozen=True)
class Expectations:
    first_tool: str | None = None
    calls: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    after_clean_escalates_to: Escalation | None = None
    max_calls: int | None = None


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    prompt: str
    needs_project: bool
    expected: Expectations = field(default_factory=Expectations)

    def prompt_for(self, project: Path | None) -> str:
        """Fill the project token by substitution, never str.format -- prompts hold braces."""
        if project is None:
            return self.prompt
        return self.prompt.replace(PROJECT_TOKEN, project.as_posix())


def load_tasks(directory: Path) -> tuple[Task, ...]:
    """Every .yaml in the directory, sorted by id so runs and scorecards line up."""
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise TaskError(f"no task files under {directory}")
    return tuple(sorted((load_task(path) for path in paths), key=lambda task: task.id))


def load_task(path: Path) -> Task:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as failure:
        raise TaskError(f"{path.name}: cannot be read as YAML: {failure}") from failure
    if not isinstance(raw, Mapping):
        raise TaskError(f"{path.name}: a task file is a mapping, not {type(raw).__name__}")

    unknown = sorted(set(raw) - TASK_KEYS)
    if unknown:
        raise TaskError(f"{path.name}: unknown key(s) {', '.join(unknown)}")
    for required in ("id", "title", "prompt", "expected"):
        if required not in raw:
            raise TaskError(f"{path.name}: missing '{required}'")

    ident = _text(raw["id"], key="id", path=path)
    if ident != path.stem:
        raise TaskError(f"{path.name}: id '{ident}' does not match the file name")

    prompt = _text(raw["prompt"], key="prompt", path=path)
    needs_project = bool(raw.get("needs_project", False))
    if PROJECT_TOKEN in prompt and not needs_project:
        raise TaskError(f"{path.name}: prompt names {PROJECT_TOKEN} but needs_project is false")

    return Task(
        id=ident,
        title=_text(raw["title"], key="title", path=path),
        prompt=prompt,
        needs_project=needs_project,
        expected=_expectations(raw["expected"], path=path),
    )


def _expectations(raw: Any, *, path: Path) -> Expectations:
    if not isinstance(raw, Mapping):
        raise TaskError(f"{path.name}: 'expected' is a mapping of checks")
    unknown = sorted(set(raw) - set(CHECK_KINDS))
    if unknown:
        raise TaskError(f"{path.name}: unknown check(s) {', '.join(unknown)}")
    if not raw:
        raise TaskError(f"{path.name}: 'expected' needs at least one check")

    return Expectations(
        first_tool=_tool(raw["first_tool"], key="first_tool", path=path)
        if "first_tool" in raw
        else None,
        calls=_tools(raw.get("calls", ()), key="calls", path=path),
        never=_tools(raw.get("never", ()), key="never", path=path),
        after_clean_escalates_to=_escalation(raw["after_clean_escalates_to"], path=path)
        if "after_clean_escalates_to" in raw
        else None,
        max_calls=_max_calls(raw["max_calls"], path=path) if "max_calls" in raw else None,
    )


def _escalation(raw: Any, *, path: Path) -> Escalation:
    if not isinstance(raw, Mapping) or set(raw) != ESCALATION_KEYS:
        raise TaskError(f"{path.name}: after_clean_escalates_to needs 'after' and 'escalates_to'")
    return Escalation(
        after=_tool(raw["after"], key="after", path=path),
        escalates_to=_tool(raw["escalates_to"], key="escalates_to", path=path),
    )


def _max_calls(raw: Any, *, path: Path) -> int:
    # bool is an int in Python, and `max_calls: true` is a typo, not a budget
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise TaskError(f"{path.name}: max_calls is a positive int, got {raw!r}")
    return raw


def _tools(raw: Any, *, key: str, path: Path) -> tuple[str, ...]:
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise TaskError(f"{path.name}: '{key}' is a list of tool names")
    return tuple(_tool(name, key=key, path=path) for name in raw)


def _tool(raw: Any, *, key: str, path: Path) -> str:
    name = _text(raw, key=key, path=path)
    if name not in TOOL_NAMES:
        raise TaskError(f"{path.name}: '{key}' names '{name}', which is not a server tool")
    return name


def _text(raw: Any, *, key: str, path: Path) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise TaskError(f"{path.name}: '{key}' is a non-empty string, got {raw!r}")
    return raw.strip()
