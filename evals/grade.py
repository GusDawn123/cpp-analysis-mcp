"""Verdicts: a task's expectations against the calls one session actually made. Pure list
scans -- no I/O, no clock, no driver. Every check explains itself in words, because a
scorecard row that only says "fail" tells the maintainer nothing about which habit slipped.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from evals.tasks import Expectations, Task
from evals.transcript import ToolCall


class Check(StrEnum):
    """The whole vocabulary, in the order a scorecard reads them."""

    FIRST_TOOL = "first_tool"
    CALLS = "calls"
    NEVER = "never"
    AFTER_CLEAN_ESCALATES_TO = "after_clean_escalates_to"
    MAX_CALLS = "max_calls"


@dataclass(frozen=True)
class Verdict:
    check: Check
    passed: bool
    detail: str


@dataclass(frozen=True)
class Result:
    task_id: str
    title: str
    verdicts: tuple[Verdict, ...] = ()
    skipped: str | None = None

    @property
    def graded(self) -> bool:
        return self.skipped is None

    @property
    def passed(self) -> bool:
        return self.graded and all(verdict.passed for verdict in self.verdicts)

    @property
    def failures(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if not verdict.passed)


def skipped(task: Task, reason: str) -> Result:
    return Result(task_id=task.id, title=task.title, skipped=reason)


def grade(task: Task, calls: Sequence[ToolCall]) -> Result:
    names = [call.name for call in calls]
    verdicts = [
        verdict
        for verdict in (
            _first_tool(task.expected, names),
            _calls(task.expected, names),
            _never(task.expected, names),
            _escalation(task.expected, names),
            _max_calls(task.expected, names),
        )
        if verdict is not None
    ]
    return Result(task_id=task.id, title=task.title, verdicts=tuple(verdicts))


def _first_tool(expected: Expectations, names: Sequence[str]) -> Verdict | None:
    if expected.first_tool is None:
        return None
    opened = names[0] if names else None
    return Verdict(
        check=Check.FIRST_TOOL,
        passed=opened == expected.first_tool,
        detail=f"opened with {opened or 'no tool at all'}, wanted {expected.first_tool}",
    )


def _calls(expected: Expectations, names: Sequence[str]) -> Verdict | None:
    if not expected.calls:
        return None
    made = set(names)
    missing = [tool for tool in expected.calls if tool not in made]
    return Verdict(
        check=Check.CALLS,
        passed=not missing,
        detail=f"never called {', '.join(missing)}" if missing else "called all of them",
    )


def _never(expected: Expectations, names: Sequence[str]) -> Verdict | None:
    if not expected.never:
        return None
    made = set(names)
    trespass = [tool for tool in expected.never if tool in made]
    return Verdict(
        check=Check.NEVER,
        passed=not trespass,
        detail=f"called {', '.join(trespass)}" if trespass else "stayed away from all of them",
    )


def _escalation(expected: Expectations, names: Sequence[str]) -> Verdict | None:
    escalation = expected.after_clean_escalates_to
    if escalation is None:
        return None
    if escalation.after not in names:
        return Verdict(
            check=Check.AFTER_CLEAN_ESCALATES_TO,
            passed=False,
            detail=f"never called {escalation.after}, so nothing could follow it",
        )
    # order carries the meaning: the next rung has to come after the cheap one, not before
    rung = names.index(escalation.after)
    escalated = escalation.escalates_to in names[rung + 1 :]
    return Verdict(
        check=Check.AFTER_CLEAN_ESCALATES_TO,
        passed=escalated,
        detail=(
            f"followed {escalation.after} with {escalation.escalates_to}"
            if escalated
            else f"stopped at {escalation.after}, never reached {escalation.escalates_to}"
        ),
    )


def _max_calls(expected: Expectations, names: Sequence[str]) -> Verdict | None:
    if expected.max_calls is None:
        return None
    return Verdict(
        check=Check.MAX_CALLS,
        passed=len(names) <= expected.max_calls,
        detail=f"{len(names)} calls, budget {expected.max_calls}",
    )
