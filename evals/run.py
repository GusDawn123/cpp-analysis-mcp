"""The harness entry point: load tasks, get a transcript for each, print a scorecard.

The scorecard is a measurement, not a gate, so a failing task does not fail the
command -- reading the table is the point. Only --driver real with --spend ever starts
a session; everything else is free.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from evals.drivers import Driver, FakeDriver, RealDriver, Skip, SpendRefusedError
from evals.grade import Result, grade, skipped
from evals.scorecard import render
from evals.tasks import TASK_DIR, Task, TaskError, load_tasks
from evals.transcript import TranscriptError

# the sample recordings live with the grader tests, which are their first reader
DEFAULT_TRANSCRIPTS = Path(__file__).resolve().parent.parent / "tests/unit/evals/transcripts"

PROJECT_ENV = "EVAL_PROJECT"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        project = _project()
        tasks = _selected(load_tasks(args.tasks), args.task)
    except (TaskError, ValueError) as failure:
        print(failure, file=sys.stderr)
        return 2

    if args.show_argv:
        for task in tasks:
            print(f"# {task.id}")
            print(shlex.join(RealDriver(project=project, arm=args.arm).argv(task)))
        return 0

    driver: Driver = (
        FakeDriver(transcripts=args.transcripts)
        if args.driver == "fake"
        else RealDriver(project=project, arm=args.arm, spend=args.spend)
    )
    try:
        results = [_result(driver, task) for task in tasks]
    except (TranscriptError, SpendRefusedError) as failure:
        print(failure, file=sys.stderr)
        return 2

    print(render(results, arm=args.arm, driver=args.driver))
    return 0


def _result(driver: Driver, task: Task) -> Result:
    outcome = driver.transcript_for(task)
    if isinstance(outcome, Skip):
        return skipped(task, outcome.reason)
    return grade(task, outcome)


def _project() -> Path | None:
    raw = os.environ.get(PROJECT_ENV)
    if not raw:
        return None
    project = Path(raw)
    if not project.is_dir():
        raise ValueError(f"{PROJECT_ENV} is {raw!r}, which is not a directory")
    return project


def _selected(tasks: Sequence[Task], wanted: str | None) -> tuple[Task, ...]:
    if wanted is None:
        return tuple(tasks)
    chosen = tuple(task for task in tasks if task.id == wanted)
    if not chosen:
        raise ValueError(f"no task with id {wanted!r}")
    return chosen


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Grade how an agent uses the cpp-analysis tools.",
    )
    parser.add_argument("--driver", choices=("fake", "real"), default="fake")
    parser.add_argument("--arm", choices=("full", "bare"), default="full")
    parser.add_argument("--tasks", type=Path, default=TASK_DIR)
    parser.add_argument("--transcripts", type=Path, default=DEFAULT_TRANSCRIPTS)
    parser.add_argument("--task", help="grade one task by id instead of all of them")
    parser.add_argument(
        "--spend",
        action="store_true",
        help="allow the real driver to start sessions, which costs usage",
    )
    parser.add_argument(
        "--show-argv",
        action="store_true",
        help="print the real driver's command (POSIX quoting) for each task and exit",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
