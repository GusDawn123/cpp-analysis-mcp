"""The scorecard is arithmetic over verdicts, so the arithmetic is what gets tested."""

from __future__ import annotations

from evals.grade import Check, Result, Verdict
from evals.scorecard import render, score


def result(ident: str, *, passed: bool) -> Result:
    return Result(
        task_id=ident,
        title=f"{ident} title",
        verdicts=(Verdict(check=Check.FIRST_TOOL, passed=passed, detail="opened with review"),),
    )


def test_score_counts_only_graded_tasks() -> None:
    tally = score(
        [
            result("a", passed=True),
            result("b", passed=False),
            Result(task_id="c", title="c title", skipped="no recorded transcript"),
        ]
    )

    assert (tally.passed, tally.graded, tally.skipped) == (1, 2, 1)


def test_score_of_nothing_does_not_divide_by_zero() -> None:
    assert score([]).percent == 0.0


def test_every_task_gets_a_row() -> None:
    table = render(
        [result("alpha", passed=True), result("beta", passed=False)], arm="full", driver="fake"
    )

    assert "| alpha |" in table
    assert "| beta |" in table


def test_a_failing_row_names_the_checks_that_failed() -> None:
    failing = Result(
        task_id="beta",
        title="beta title",
        verdicts=(
            Verdict(check=Check.FIRST_TOOL, passed=False, detail="opened with sanitize_file"),
            Verdict(check=Check.MAX_CALLS, passed=False, detail="6 calls, budget 3"),
        ),
    )

    table = render([failing], arm="full", driver="fake")

    assert "first_tool" in table
    assert "max_calls" in table


def test_a_skipped_row_carries_its_reason() -> None:
    table = render(
        [Result(task_id="gamma", title="gamma title", skipped="EVAL_PROJECT is unset")],
        arm="full",
        driver="fake",
    )

    assert "EVAL_PROJECT is unset" in table


def test_the_header_says_which_arm_and_driver_produced_it() -> None:
    table = render([result("alpha", passed=True)], arm="bare", driver="real")

    assert "bare" in table
    assert "real" in table


def test_the_score_line_reports_passed_over_graded() -> None:
    table = render(
        [
            result("a", passed=True),
            result("b", passed=False),
            Result(task_id="c", title="c title", skipped="no recorded transcript"),
        ],
        arm="full",
        driver="fake",
    )

    assert "1 / 2" in table
    assert "1 skipped" in table
