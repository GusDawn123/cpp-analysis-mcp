"""One canonical spelling per path, so identity cannot depend on who spelled it.

The resolver feeds fingerprinting: paths under the root come back project-relative
POSIX, paths outside it stay whole, and relative spellings pass through untouched
because only the tool that printed them knows what they were relative to. The git
half is driven through a scripted runner; real git answers in the integration suite.
"""

from __future__ import annotations

import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp.planner.scope import (
    GIT_TIMEOUT_S,
    ChangedScope,
    changed_since,
    relativizer,
)
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store.models import CapabilityStatus


def test_an_absolute_path_under_the_root_becomes_relative_posix(tmp_path: Path) -> None:
    canonical = relativizer(tmp_path)

    assert canonical(str(tmp_path / "src" / "a.cpp")) == "src/a.cpp"


def test_nested_directories_keep_their_shape(tmp_path: Path) -> None:
    canonical = relativizer(tmp_path)

    assert canonical(str(tmp_path / "src" / "core" / "deep" / "x.hpp")) == "src/core/deep/x.hpp"


def test_dot_segments_resolve_away(tmp_path: Path) -> None:
    # tools print paths like build/../src/a.cpp; the identity must be the settled file
    spelled = tmp_path / "build" / ".." / "src" / "a.cpp"

    assert relativizer(tmp_path)(str(spelled)) == "src/a.cpp"


def test_a_path_outside_the_root_stays_whole(tmp_path: Path) -> None:
    """A caller-named file may live anywhere; truncating it to a basename would
    collide two same-named files in different projects."""
    root = tmp_path / "proj"
    elsewhere = tmp_path / "vendor" / "b.cpp"

    result = relativizer(root)(str(elsewhere))

    assert result == elsewhere.resolve().as_posix()
    assert result.endswith("vendor/b.cpp")


def test_a_relative_spelling_passes_through_untouched(tmp_path: Path) -> None:
    """A relative path is relative to some tool's working directory, which this
    process's own cwd knows nothing about -- resolving it here would invent a lie."""
    canonical = relativizer(tmp_path)

    assert canonical("src/a.cpp") == "src/a.cpp"
    assert canonical("./src/a.cpp") == "./src/a.cpp"


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive filesystems only")
def test_case_differences_in_a_real_files_spelling_agree(tmp_path: Path) -> None:
    """Windows tools print whatever case they were handed; the file on disk has one."""
    file = tmp_path / "Src" / "a.cpp"
    file.parent.mkdir(parents=True)
    file.write_text("int x;\n", encoding="utf-8")

    canonical = relativizer(tmp_path)

    assert canonical(str(file).upper()) == canonical(str(file)) == "Src/a.cpp"


def test_ten_thousand_spellings_canonicalize_in_under_a_second(tmp_path: Path) -> None:
    """The latency gate: findings repeat files, and repeats must be dict hits.

    200 distinct paths cycled 50 times models a large run; only the first sight of
    each spelling may touch the filesystem. The bound is generous on purpose -- its
    one job is catching a resolve() slipping into the per-call path.
    """
    canonical = relativizer(tmp_path)
    spellings = [str(tmp_path / "src" / f"file_{index}.cpp") for index in range(200)]

    started = time.perf_counter()
    for _repeat in range(50):
        for spelled in spellings:
            canonical(spelled)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"10k canonicalizations took {elapsed:.3f}s"


# ------------------------------------------------------------- what changed since a ref


@dataclass
class GitScript:
    """Answer scripted results in call order, recording each spawn whole."""

    script: list[RunResult]
    spawns: list[list[str]] = field(default_factory=list)
    timeouts: list[int] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        self.spawns.append(list(cmd))
        self.timeouts.append(timeout_s)
        return self.script[len(self.spawns) - 1]


def no_spawns(
    cmd: Sequence[str],
    *,
    timeout_s: int,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> RunResult:
    raise AssertionError(f"nothing may be spawned without git, but got {list(cmd)}")


def with_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "git")


def at_root(output: str = "/repo\n") -> RunResult:
    return RunResult(exit_code=0, output=output)


def refused(result: ChangedScope | CapabilityStatus) -> CapabilityStatus:
    assert isinstance(result, CapabilityStatus), f"expected a refusal, got {result}"
    assert result.available is False
    return result


def resolved(result: ChangedScope | CapabilityStatus) -> ChangedScope:
    assert isinstance(result, ChangedScope), f"expected a scope, got {result}"
    return result


def test_a_machine_without_git_refuses_in_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    result = refused(changed_since(Path("/repo"), "HEAD", runner=no_spawns))

    assert result.reason is not None
    assert "git is not on PATH" in result.reason


def test_a_directory_outside_any_repo_refuses_with_gits_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_git(monkeypatch)
    complaint = "fatal: not a git repository (or any of the parent directories): .git"
    runner = GitScript([RunResult(exit_code=128, output=f"{complaint}\n")])

    result = refused(changed_since(tmp_path, "HEAD", runner=runner))

    assert result.reason == complaint
    assert len(runner.spawns) == 1  # the refusal stops the chain before diff and ls-files


def test_a_bad_ref_refuses_with_gits_words(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with_git(monkeypatch)
    runner = GitScript([at_root(), RunResult(exit_code=128, output="fatal: bad revision 'nope'\n")])

    result = refused(changed_since(tmp_path, "nope", runner=runner))

    assert result.reason == "fatal: bad revision 'nope'"
    assert len(runner.spawns) == 2


def test_a_killed_git_reports_itself_rather_than_scoping_blind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_git(monkeypatch)
    killed = RunResult(exit_code=None, output=f"\n[killed after {GIT_TIMEOUT_S}s timeout]\n")
    runner = GitScript([killed])

    result = refused(changed_since(tmp_path, "HEAD", runner=runner))

    assert result.reason is not None
    assert "killed" in result.reason


def test_changed_files_come_back_repo_relative_in_gits_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """-z framing is the point: a name with spaces survives whole."""
    with_git(monkeypatch)
    runner = GitScript(
        [
            at_root(),
            RunResult(exit_code=0, output="M\0src/a.cpp\0A\0src/sub dir/b.cpp\0"),
            RunResult(exit_code=0, output=""),
        ]
    )

    scope = resolved(changed_since(tmp_path, "HEAD", runner=runner))

    assert scope.root == Path("/repo")
    assert scope.files == ("src/a.cpp", "src/sub dir/b.cpp")


def test_a_rename_counts_as_its_new_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with_git(monkeypatch)
    runner = GitScript(
        [
            at_root(),
            RunResult(exit_code=0, output="R100\0old/name.cpp\0new/name.cpp\0"),
            RunResult(exit_code=0, output=""),
        ]
    )

    scope = resolved(changed_since(tmp_path, "HEAD", runner=runner))

    assert scope.files == ("new/name.cpp",)


def test_a_deleted_file_is_not_in_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # nothing on disk to analyze; its findings leave the picture via baseline subtraction
    with_git(monkeypatch)
    runner = GitScript(
        [
            at_root(),
            RunResult(exit_code=0, output="D\0gone.cpp\0M\0kept.cpp\0"),
            RunResult(exit_code=0, output=""),
        ]
    )

    scope = resolved(changed_since(tmp_path, "HEAD", runner=runner))

    assert scope.files == ("kept.cpp",)


def test_untracked_files_join_after_the_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A brand-new file's bugs are the review gate's whole point."""
    with_git(monkeypatch)
    runner = GitScript(
        [
            at_root(),
            RunResult(exit_code=0, output="M\0a.cpp\0"),
            RunResult(exit_code=0, output="fresh.cpp\0also/new.hpp\0"),
        ]
    )

    scope = resolved(changed_since(tmp_path, "HEAD", runner=runner))

    assert scope.files == ("a.cpp", "fresh.cpp", "also/new.hpp")


def test_the_three_git_questions_are_pinned_with_their_timeouts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with_git(monkeypatch)
    runner = GitScript([at_root(), RunResult(exit_code=0, output=""), at_root("")])

    changed_since(tmp_path, "main", runner=runner)

    # rev-parse runs where the caller pointed; the other two run at the root it named,
    # because ls-files answers cwd-limited and diff.relative can shrink diff's paths
    asked = ["git", "-C", str(tmp_path)]
    at_repo = ["git", "-C", str(Path("/repo"))]
    assert runner.spawns == [
        [*asked, "rev-parse", "--show-toplevel"],
        [*at_repo, "diff", "--name-status", "-z", "main"],
        [*at_repo, "ls-files", "--others", "--exclude-standard", "-z"],
    ]
    assert runner.timeouts == [GIT_TIMEOUT_S] * 3
