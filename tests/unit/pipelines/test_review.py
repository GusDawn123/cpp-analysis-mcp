"""The review gate end to end, with every subprocess faked and answered by command.

Dispatch runs the two plugins in parallel, so the fake routes by what was asked --
git by call order (sequential), each tool by its own command shape -- and the tests
tell the product's story: audit remembers, review subtracts.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp.pipelines.review import (
    N_DETAILED,
    AuditReport,
    ReviewReport,
    audit_project,
    review_project,
)
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store import runs
from cpp_analysis_mcp.store.models import Analysis, CapabilityStatus
from cpp_analysis_mcp.toolchains.base import Toolchain

ROOT = Path("/repo")

CLEAN = RunResult(exit_code=0, output="")

USE_AFTER_MOVE = "bugprone-use-after-move"
DANGLING = "bugprone-dangling-handle"


def at_root() -> RunResult:
    return RunResult(exit_code=0, output="/repo\n")


def tidy_line(source: Path, line: int, check: str) -> str:
    return f"{source}:{line}:5: warning: something worth hearing [{check}]\n"


@dataclass
class AnsweringRunner:
    """Answer git in call order and each tool by its command shape, thread-safely.

    The static tier runs both plugins in parallel, so a purely ordered script would
    race; git questions happen before dispatch and stay sequential.
    """

    git: list[RunResult]
    tidy: RunResult = CLEAN
    compiler: RunResult = CLEAN
    spawns: list[list[str]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        with self._lock:
            self.spawns.append(list(cmd))
            if cmd[0] == "git":
                return self.git.pop(0)
            if "-fsyntax-only" in cmd:
                return self.compiler
            return self.tidy


def a_clang() -> Toolchain:
    return Toolchain(
        family="clang",
        compiler=Path("/usr/bin/clang++"),
        version="clang version 21.0.0",
        warning_flags=("-Wthread-safety",),
    )


def a_platform(tmp_path: Path) -> Platform:
    """A platform whose tool directory holds a stand-in clang-tidy, PATH blinded."""
    tidy = tmp_path / "tools" / "clang-tidy"
    tidy.parent.mkdir(parents=True, exist_ok=True)
    if not tidy.exists():
        tidy.write_text("#!/bin/sh\n", encoding="utf-8")
    return Platform(name="linux", extra_tool_dirs=(tidy.parent,))


def working() -> dict[Analysis, CapabilityStatus]:
    return dict.fromkeys(Analysis, CapabilityStatus(available=True, verified_by="probe"))


def blind_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "git" if name == "git" else None)


def audit_git(files: str = "src/a.cpp\0") -> list[RunResult]:
    return [at_root(), RunResult(exit_code=0, output=files)]


def review_git(changed: str = "M\0src/a.cpp\0") -> list[RunResult]:
    return [
        at_root(),
        RunResult(exit_code=0, output=changed),
        RunResult(exit_code=0, output=""),
    ]


def reviewed(result: ReviewReport | CapabilityStatus) -> ReviewReport:
    assert isinstance(result, ReviewReport), f"expected a review report, got {result}"
    return result


def audited(result: AuditReport | CapabilityStatus) -> AuditReport:
    assert isinstance(result, AuditReport), f"expected an audit report, got {result}"
    return result


def test_review_without_a_baseline_reports_everything_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    source = ROOT / "src/a.cpp"
    runner = AnsweringRunner(
        git=review_git(), tidy=RunResult(exit_code=0, output=tidy_line(source, 3, USE_AFTER_MOVE))
    )

    report = reviewed(
        review_project(
            tmp_path,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    assert report.baseline_used is False
    assert any("audit" in note for note in report.notes)
    assert USE_AFTER_MOVE in [entry.category for entry in report.index]
    assert report.total_new == len(report.index)
    # both static plugins ran; the trace says so
    assert sorted(step.analyzer for step in report.steps) == ["clang-tidy", "compiler-warnings"]


def test_audit_remembers_and_review_reports_only_the_new_finding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The product's whole story: audit main, introduce a bug, review, see one finding."""
    blind_path(monkeypatch)
    source = ROOT / "src/a.cpp"
    platform = a_platform(tmp_path)
    cache = tmp_path / "cache"

    known = audited(
        audit_project(
            tmp_path,
            record_as="main",
            toolchain=a_clang(),
            platform=platform,
            capabilities=working(),
            cache_dir=cache,
            runner=AnsweringRunner(
                git=audit_git(),
                tidy=RunResult(exit_code=0, output=tidy_line(source, 3, USE_AFTER_MOVE)),
            ),
        )
    )
    assert known.recorded_as == "main"
    assert known.baseline_path

    report = reviewed(
        review_project(
            tmp_path,
            "main",
            toolchain=a_clang(),
            platform=platform,
            capabilities=working(),
            cache_dir=cache,
            runner=AnsweringRunner(
                git=review_git(),
                tidy=RunResult(
                    exit_code=0,
                    output=tidy_line(source, 3, USE_AFTER_MOVE) + tidy_line(source, 9, DANGLING),
                ),
            ),
        )
    )

    assert report.baseline_used is True
    assert report.total_new == 1
    assert [entry.category for entry in report.index] == [DANGLING]
    assert report.detailed[0].category == DANGLING
    # and the remembered run can answer a later get_finding
    assert runs.find(cache, Path(report.root), report.detailed[0].fingerprint) is not None


def test_a_changed_tool_retires_the_baseline_and_the_report_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    source = ROOT / "src/a.cpp"
    platform = a_platform(tmp_path)
    cache = tmp_path / "cache"
    audit_project(
        tmp_path,
        record_as="main",
        toolchain=a_clang(),
        platform=platform,
        capabilities=working(),
        cache_dir=cache,
        runner=AnsweringRunner(git=audit_git()),
    )

    upgraded = Toolchain(
        family="clang",
        compiler=Path("/usr/bin/clang++"),
        version="clang version 22.0.0",
        warning_flags=("-Wthread-safety",),
    )
    report = reviewed(
        review_project(
            tmp_path,
            "main",
            toolchain=upgraded,
            platform=platform,
            capabilities=working(),
            cache_dir=cache,
            runner=AnsweringRunner(
                git=review_git(),
                tidy=RunResult(exit_code=0, output=tidy_line(source, 3, USE_AFTER_MOVE)),
            ),
        )
    )

    assert report.baseline_used is False
    assert any("audit" in note for note in report.notes)


def test_review_passes_gits_refusal_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    complaint = "fatal: not a git repository (or any of the parent directories): .git"
    runner = AnsweringRunner(git=[RunResult(exit_code=128, output=f"{complaint}\n")])

    result = review_project(
        tmp_path,
        "main",
        toolchain=a_clang(),
        platform=a_platform(tmp_path),
        capabilities=working(),
        cache_dir=tmp_path / "cache",
        runner=runner,
    )

    assert isinstance(result, CapabilityStatus)
    assert result.reason == complaint


def test_the_index_lists_everything_and_detail_stops_at_the_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    source = ROOT / "src/a.cpp"
    many = "".join(tidy_line(source, line, USE_AFTER_MOVE) for line in range(1, 11))
    runner = AnsweringRunner(git=review_git(), tidy=RunResult(exit_code=0, output=many))

    report = reviewed(
        review_project(
            tmp_path,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    assert report.total_new == len(report.index)
    assert report.total_new > N_DETAILED
    assert len(report.detailed) == N_DETAILED
    assert report.truncated is True
