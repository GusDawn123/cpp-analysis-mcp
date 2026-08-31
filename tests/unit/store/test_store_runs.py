"""The remembered last run: findings written whole, read back through find() -- the memory
behind the get_finding tool. A run that cannot be read answers None like a run that never
happened; the miss explains itself upstream instead of crashing here.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.store.models import Confirmation, Finding, Location, Severity
from cpp_analysis_mcp.store.runs import find, save


def a_finding(fingerprint: str = "e56adf7bdc0bf0a3") -> Finding:
    return Finding(
        id="tidy-0001",
        tool="clang-tidy",
        severity=Severity.WARNING,
        category="bugprone-use-after-move",
        message="'order' used after it was moved",
        location=Location(file="src/order_book.cpp", line=40, column=14),
        occurrences=3,
        fingerprint=fingerprint,
        fingerprint_scheme=1,
        confirmations=(Confirmation(tool="compiler", finding_id="warn-0002"),),
    )


def test_a_saved_finding_reads_back_whole(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, (a_finding(),))

    assert find(tmp_path, root, "e56adf7bdc0bf0a3") == a_finding()


def test_an_unknown_fingerprint_answers_none(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, (a_finding(),))

    assert find(tmp_path, root, "0000000000000000") is None


def test_no_remembered_run_answers_none(tmp_path: Path) -> None:
    assert find(tmp_path, tmp_path / "proj", "e56adf7bdc0bf0a3") is None


def test_a_corrupt_run_answers_none(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    path = save(tmp_path, root, (a_finding(),))
    path.write_text("{not json", encoding="utf-8")

    assert find(tmp_path, root, "e56adf7bdc0bf0a3") is None


def test_each_project_remembers_its_own_run(tmp_path: Path) -> None:
    save(tmp_path, tmp_path / "ours", (a_finding("aaaaaaaaaaaaaaaa"),))
    save(tmp_path, tmp_path / "theirs", (a_finding("bbbbbbbbbbbbbbbb"),))

    assert find(tmp_path, tmp_path / "ours", "bbbbbbbbbbbbbbbb") is None
    assert find(tmp_path, tmp_path / "theirs", "bbbbbbbbbbbbbbbb") is not None
