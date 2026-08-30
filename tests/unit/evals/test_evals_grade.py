"""One verdict per check, and a task passes only when every one of them does.

Grading is pure: a task plus the calls an agent made, in, verdicts out. The stories
here are the ones the sample transcripts record, written small so each check fails
alone.
"""

from __future__ import annotations

from collections.abc import Sequence

from evals.grade import Check, Result, grade, skipped
from evals.tasks import Escalation, Expectations, Task
from evals.transcript import ToolCall


def task(expected: Expectations, *, ident: str = "t") -> Task:
    return Task(id=ident, title="A task", prompt="do it", needs_project=False, expected=expected)


def calls(*names: str) -> Sequence[ToolCall]:
    return [ToolCall(name=name, input={}) for name in names]


def verdict(result: Result, check: Check) -> bool:
    for each in result.verdicts:
        if each.check is check:
            return each.passed
    raise AssertionError(f"no verdict for {check}")


# ------------------------------------------------------------------ first_tool


def test_first_tool_passes_on_the_opening_call() -> None:
    result = grade(task(Expectations(first_tool="static_check_file")), calls("static_check_file"))

    assert verdict(result, Check.FIRST_TOOL)
    assert result.passed


def test_first_tool_fails_when_the_agent_opened_elsewhere() -> None:
    result = grade(
        task(Expectations(first_tool="static_check_file")),
        calls("sanitize_file", "static_check_file"),
    )

    assert not verdict(result, Check.FIRST_TOOL)
    assert "sanitize_file" in result.verdicts[0].detail


def test_first_tool_fails_when_the_agent_called_nothing() -> None:
    result = grade(task(Expectations(first_tool="static_check_file")), calls())

    assert not verdict(result, Check.FIRST_TOOL)


# ----------------------------------------------------------------------- calls


def test_calls_passes_when_each_required_tool_appears() -> None:
    expected = Expectations(calls=("static_check_file", "sanitize_file"))
    result = grade(task(expected), calls("static_check_file", "capabilities", "sanitize_file"))

    assert verdict(result, Check.CALLS)


def test_calls_names_what_never_showed_up() -> None:
    expected = Expectations(calls=("static_check_file", "sanitize_file"))
    result = grade(task(expected), calls("static_check_file"))

    assert not verdict(result, Check.CALLS)
    assert "sanitize_file" in result.verdicts[0].detail


# ----------------------------------------------------------------------- never


def test_never_passes_when_the_forbidden_tool_stayed_out() -> None:
    result = grade(task(Expectations(never=("sanitize_file",))), calls("profile_file"))

    assert verdict(result, Check.NEVER)


def test_never_fails_and_names_the_tool_that_appeared() -> None:
    result = grade(
        task(Expectations(never=("sanitize_file", "sanitize_project"))),
        calls("profile_file", "sanitize_file"),
    )

    assert not verdict(result, Check.NEVER)
    assert "sanitize_file" in result.verdicts[0].detail


# --------------------------------------------------- after_clean_escalates_to


def test_escalation_passes_when_the_next_rung_follows() -> None:
    expected = Expectations(
        after_clean_escalates_to=Escalation(after="static_check_file", escalates_to="sanitize_file")
    )
    result = grade(task(expected), calls("static_check_file", "sanitize_file"))

    assert verdict(result, Check.AFTER_CLEAN_ESCALATES_TO)


def test_escalation_fails_when_the_agent_stopped_at_the_cheap_rung() -> None:
    expected = Expectations(
        after_clean_escalates_to=Escalation(after="static_check_file", escalates_to="sanitize_file")
    )
    result = grade(task(expected), calls("static_check_file"))

    assert not verdict(result, Check.AFTER_CLEAN_ESCALATES_TO)


def test_escalation_fails_when_the_next_rung_came_first() -> None:
    # order is the whole point: a sanitizer before the cheap rung is not an escalation
    expected = Expectations(
        after_clean_escalates_to=Escalation(after="static_check_file", escalates_to="sanitize_file")
    )
    result = grade(task(expected), calls("sanitize_file", "static_check_file"))

    assert not verdict(result, Check.AFTER_CLEAN_ESCALATES_TO)


def test_escalation_fails_when_the_earlier_tool_never_ran() -> None:
    expected = Expectations(
        after_clean_escalates_to=Escalation(after="static_check_file", escalates_to="sanitize_file")
    )
    result = grade(task(expected), calls("sanitize_file"))

    assert not verdict(result, Check.AFTER_CLEAN_ESCALATES_TO)


# ------------------------------------------------------------------- max_calls


def test_max_calls_counts_every_call_in_the_transcript() -> None:
    result = grade(task(Expectations(max_calls=2)), calls("full_check_file", "Read", "Read"))

    assert not verdict(result, Check.MAX_CALLS)
    assert "3" in result.verdicts[0].detail


def test_max_calls_passes_at_the_budget() -> None:
    result = grade(task(Expectations(max_calls=2)), calls("full_check_file", "get_finding"))

    assert verdict(result, Check.MAX_CALLS)


# --------------------------------------------------------------- whole results


def test_verdicts_come_back_in_the_vocabulary_order() -> None:
    expected = Expectations(
        first_tool="static_check_file",
        calls=("sanitize_file",),
        never=("profile_file",),
        after_clean_escalates_to=Escalation(
            after="static_check_file", escalates_to="sanitize_file"
        ),
        max_calls=4,
    )
    result = grade(task(expected), calls("static_check_file", "sanitize_file"))

    assert [each.check for each in result.verdicts] == list(Check)
    assert result.passed


def test_one_failed_check_fails_the_task() -> None:
    expected = Expectations(first_tool="static_check_file", never=("sanitize_file",))
    result = grade(task(expected), calls("static_check_file", "sanitize_file"))

    assert not result.passed


def test_a_skipped_task_carries_its_reason_and_is_not_graded() -> None:
    result = skipped(task(Expectations(max_calls=1)), "EVAL_PROJECT is unset")

    assert result.skipped == "EVAL_PROJECT is unset"
    assert result.verdicts == ()
    assert not result.passed
