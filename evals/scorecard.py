"""Results as a markdown table, plus the one number the whole run comes down to.

Skipped tasks stay visible with their reason and stay out of the denominator: a
harness that scored an unrun task as a pass would be lying, and one that scored it as
a failure would punish an unset env var.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evals.grade import Result

HEADER = ("task", "result", "notes")


@dataclass(frozen=True)
class Score:
    passed: int
    graded: int
    skipped: int

    @property
    def percent(self) -> float:
        return 100.0 * self.passed / self.graded if self.graded else 0.0


def score(results: Sequence[Result]) -> Score:
    graded = [result for result in results if result.graded]
    return Score(
        passed=sum(1 for result in graded if result.passed),
        graded=len(graded),
        skipped=len(results) - len(graded),
    )


def render(results: Sequence[Result], *, arm: str, driver: str) -> str:
    tally = score(results)
    lines = [
        f"driver `{driver}` | arm `{arm}`",
        "",
        f"| {' | '.join(HEADER)} |",
        f"| {' | '.join('---' for _ in HEADER)} |",
    ]
    lines.extend(_row(result) for result in results)
    lines.extend(
        [
            "",
            f"**{tally.passed} / {tally.graded} tasks passed** "
            f"({tally.percent:.0f}%), {tally.skipped} skipped",
        ]
    )
    return "\n".join(lines)


def _row(result: Result) -> str:
    if not result.graded:
        return f"| {result.task_id} | skip | {result.skipped} |"
    if result.passed:
        return f"| {result.task_id} | pass | {result.title} |"
    notes = "; ".join(f"{verdict.check}: {verdict.detail}" for verdict in result.failures)
    return f"| {result.task_id} | fail | {notes} |"
