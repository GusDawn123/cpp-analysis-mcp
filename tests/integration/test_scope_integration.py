"""Prove changed_since against a real git and a real repository, not a script."""

from __future__ import annotations

from pathlib import Path

import pytest

from cpp_analysis_mcp import process
from cpp_analysis_mcp.planner.scope import ChangedScope, changed_since

pytestmark = pytest.mark.integration


def git(directory: Path, *args: str) -> None:
    result = process.run(["git", "-C", str(directory), *args], timeout_s=30)
    assert result.exit_code == 0, result.output


def test_a_real_repo_answers_with_its_root_and_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    tracked = repo / "sub" / "a.cpp"
    tracked.write_text("int x = 1;\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=ci@example.com", "-c", "user.name=CI", "commit", "-q", "-m", "seed")
    tracked.write_text("int x = 2;\n", encoding="utf-8")
    (repo / "fresh.cpp").write_text("int y = 0;\n", encoding="utf-8")

    # asked from a subdirectory on purpose: -C plus rev-parse must still find the root
    scope = changed_since(repo / "sub", "HEAD", runner=process.run)

    assert isinstance(scope, ChangedScope), scope
    assert scope.root.resolve() == repo.resolve()
    assert scope.files == ("sub/a.cpp", "fresh.cpp")
