"""Loading and validating the task files.

A task that loads but means nothing -- a typo'd tool name, a check kind nobody
implements -- would grade green forever, so the loader refuses instead of shrugging.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.tasks import TASK_DIR, TaskError, load_task, load_tasks

VALID = """\
id: sample-task
title: A sample task
prompt: Is engine/src/OrderBook.cpp wrong?
needs_project: false
expected:
  first_tool: static_check_file
  calls: [static_check_file, sanitize_file]
  never: [profile_project]
  after_clean_escalates_to:
    after: static_check_file
    escalates_to: sanitize_file
  max_calls: 8
"""


def write(tmp_path: Path, text: str, *, name: str = "sample-task.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_reads_every_field(tmp_path: Path) -> None:
    task = load_task(write(tmp_path, VALID))

    assert task.id == "sample-task"
    assert task.title == "A sample task"
    assert task.needs_project is False
    assert task.expected.first_tool == "static_check_file"
    assert task.expected.calls == ("static_check_file", "sanitize_file")
    assert task.expected.never == ("profile_project",)
    assert task.expected.max_calls == 8


def test_reads_the_two_ends_of_an_escalation(tmp_path: Path) -> None:
    escalation = load_task(write(tmp_path, VALID)).expected.after_clean_escalates_to

    assert escalation is not None
    assert (escalation.after, escalation.escalates_to) == ("static_check_file", "sanitize_file")


def test_needs_project_defaults_to_false(tmp_path: Path) -> None:
    text = VALID.replace("needs_project: false\n", "")

    assert load_task(write(tmp_path, text)).needs_project is False


def test_the_id_must_match_the_file_name(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match=r"other-name\.yaml"):
        load_task(write(tmp_path, VALID, name="other-name.yaml"))


def test_an_unknown_top_level_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match="notes"):
        load_task(write(tmp_path, VALID + "notes: leftover\n"))


def test_an_unknown_check_kind_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match="answers_in_english"):
        load_task(write(tmp_path, VALID + "  answers_in_english: true\n"))


def test_a_missing_required_key_is_refused(tmp_path: Path) -> None:
    text = VALID.replace("title: A sample task\n", "")

    with pytest.raises(TaskError, match="title"):
        load_task(write(tmp_path, text))


def test_expectations_may_not_be_empty(tmp_path: Path) -> None:
    text = VALID.split("expected:")[0] + "expected: {}\n"

    with pytest.raises(TaskError, match="at least one check"):
        load_task(write(tmp_path, text))


def test_a_tool_the_server_does_not_have_is_refused(tmp_path: Path) -> None:
    text = VALID.replace("first_tool: static_check_file", "first_tool: sanitize_fil")

    with pytest.raises(TaskError, match="sanitize_fil"):
        load_task(write(tmp_path, text))


def test_max_calls_must_be_a_positive_int(tmp_path: Path) -> None:
    text = VALID.replace("max_calls: 8", "max_calls: 0")

    with pytest.raises(TaskError, match="max_calls"):
        load_task(write(tmp_path, text))


def test_a_prompt_naming_the_project_must_declare_it(tmp_path: Path) -> None:
    text = VALID.replace("Is engine", "Is {project}/engine")

    with pytest.raises(TaskError, match="needs_project"):
        load_task(write(tmp_path, text))


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match="mapping"):
        load_task(write(tmp_path, "- just\n- a list\n"))


def test_python_object_tags_do_not_construct(tmp_path: Path) -> None:
    # safe_load only: a task file is data the harness reads, never code it runs
    with pytest.raises(TaskError):
        load_task(write(tmp_path, "id: !!python/object/apply:os.system ['echo hi']\n"))


def test_a_directory_loads_sorted_by_id(tmp_path: Path) -> None:
    write(tmp_path, VALID.replace("sample-task", "zulu"), name="zulu.yaml")
    write(tmp_path, VALID.replace("sample-task", "alpha"), name="alpha.yaml")

    assert [task.id for task in load_tasks(tmp_path)] == ["alpha", "zulu"]


def test_an_empty_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TaskError, match="no task files"):
        load_tasks(tmp_path)


def test_every_checked_in_task_round_trips() -> None:
    tasks = load_tasks(TASK_DIR)

    assert len(tasks) >= 20
    assert len({task.id for task in tasks}) == len(tasks)
