"""The review gate end to end, with every subprocess faked and answered by command.

Dispatch runs the two plugins in parallel, so the fake routes by what was asked --
git by call order (sequential), each tool by its own command shape -- and the tests
tell the product's story: audit remembers, review subtracts.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path

import pytest

from cpp_analysis_mcp import compile_db
from cpp_analysis_mcp.parsers.clang_tidy import parse as parse_tidy
from cpp_analysis_mcp.pipelines.review import (
    DETAILED_TIERS,
    N_DETAILED,
    AuditReport,
    IndexEntry,
    ReviewReport,
    _relocated,
    audit_project,
    review_project,
)
from cpp_analysis_mcp.planner.scope import relativizer
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store import runs
from cpp_analysis_mcp.store.fingerprints import compute_fingerprint, fingerprint_batch
from cpp_analysis_mcp.store.models import (
    AccessOp,
    Analysis,
    CapabilityStatus,
    Finding,
    Frame,
    Location,
    Severity,
    SuggestedFix,
    ThreadAccess,
)
from cpp_analysis_mcp.store.triage import Tier
from cpp_analysis_mcp.toolchains.base import Toolchain

ROOT = Path("/repo")

CLEAN = RunResult(exit_code=0, output="")

USE_AFTER_MOVE = "bugprone-use-after-move"
DANGLING = "bugprone-dangling-handle"
MAGIC_NUMBERS = "cppcoreguidelines-avoid-magic-numbers"
NULLPTR = "modernize-use-nullptr"
COPIED_PARAM = "performance-unnecessary-value-param"
MEMBER_INIT = "cppcoreguidelines-pro-type-member-init"
THREAD_SAFETY = "thread-safety-analysis"

# a dozen distinct lines, so a flagged one contributes real text to its fingerprint
SOURCE = "".join(f"int line_{number}() {{ return {number}; }}\n" for number in range(1, 13))


def at(root: Path) -> RunResult:
    return RunResult(exit_code=0, output=f"{root}\n")


def at_root() -> RunResult:
    return at(ROOT)


def tidy_line(source: Path | str, line: int, check: str) -> str:
    return f"{source}:{line}:5: warning: something worth hearing [{check}]\n"


def thread_safety_line(source: Path | str, line: int) -> str:
    warning = "writing needs a lock [-Wthread-safety-analysis]"
    return f"{source}:{line}:5: warning: {warning}\n"


def an_export(*, check: str, file: str, offset: int, length: int, text: str) -> str:
    """One diagnostic's fix-it, spelled the way clang-tidy's --export-fixes spells it."""
    return f"""\
---
MainSourceFile:  '{file}'
Diagnostics:
  - DiagnosticName:  '{check}'
    DiagnosticMessage:
      Message:         'something worth hearing'
      FilePath:        '{file}'
      FileOffset:      {offset}
      Replacements:
        - FilePath:        '{file}'
          Offset:          {offset}
          Length:          {length}
          ReplacementText: '{text}'
    Level:           Warning
...
"""


def a_checkout(tmp_path: Path) -> Path:
    """A real root on disk holding one real source file, for the paths that must be read."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.cpp").write_text(SOURCE, encoding="utf-8")
    return root


def offset_in(root: Path, text: str) -> int:
    """Where that text starts in the checked-out file, in bytes.

    Bytes, not characters: a fix-it's offsets index the file as the tool read it, and a
    checkout written through text mode holds CRLF on Windows.
    """
    return (root / "src" / "a.cpp").read_bytes().index(text.encode())


def a_database(root: Path, name: str) -> None:
    """A compilation database in one of the root's build trees, naming the one source."""
    build = root / name
    build.mkdir(parents=True, exist_ok=True)
    entry = {
        "directory": str(build),
        "file": str(root / "src" / "a.cpp"),
        "command": "clang++ -c ../src/a.cpp",
    }
    (build / "compile_commands.json").write_text(json.dumps([entry]), encoding="utf-8")


@dataclass
class AnsweringRunner:
    """Answer git in call order and each tool by its command shape, thread-safely.

    The static tier runs both plugins in parallel, so a purely ordered script would
    race; git questions happen before dispatch and stay sequential.
    """

    git: list[RunResult]
    tidy: RunResult = CLEAN
    compiler: RunResult = CLEAN
    # what tidy writes to --export-fixes, for the runs that pretend a check offered one
    fixes: str = ""
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
            self._export(cmd)
            return self.tidy

    def _export(self, cmd: Sequence[str]) -> None:
        """Write the fix-it file where the command said to, as clang-tidy would have."""
        for arg in cmd:
            if self.fixes and arg.startswith("--export-fixes="):
                Path(arg.removeprefix("--export-fixes=")).write_text(self.fixes, encoding="utf-8")


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


def audit_git(files: str = "src/a.cpp\0", *, root: Path = ROOT) -> list[RunResult]:
    return [at(root), RunResult(exit_code=0, output=files)]


def review_git(changed: str = "M\0src/a.cpp\0", *, root: Path = ROOT) -> list[RunResult]:
    return [
        at(root),
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
    assert report.detailed[0].finding.category == DANGLING
    # and the remembered run can answer a later get_finding
    identity = report.detailed[0].finding.fingerprint
    assert runs.find(cache, Path(report.root), identity) is not None


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


def test_the_report_counts_every_indexed_finding_by_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    source = ROOT / "src/a.cpp"
    output = (
        tidy_line(source, 3, USE_AFTER_MOVE)
        + tidy_line(source, 5, COPIED_PARAM)
        + tidy_line(source, 7, NULLPTR)
        + tidy_line(source, 9, MAGIC_NUMBERS)
    )
    runner = AnsweringRunner(git=review_git(), tidy=RunResult(exit_code=0, output=output))

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

    # every tier is a key, zero counts included: an absent row would read as unknown
    assert set(report.tiers) == set(Tier)
    assert sum(report.tiers.values()) == report.total_new == len(report.index)
    assert report.tiers[Tier.CRITICAL] == 0
    assert report.tiers[Tier.MAJOR] == 1
    assert report.tiers[Tier.MINOR] == 1
    assert report.tiers[Tier.STYLE] == 2


def test_style_is_counted_but_never_takes_a_detail_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of the split: a pile of opinions about how code looks must not
    push the one use-after-move out of the detail a reader actually gets."""
    blind_path(monkeypatch)
    source = ROOT / "src/a.cpp"
    output = (
        "".join(tidy_line(source, line, NULLPTR) for line in range(1, 7))
        + tidy_line(source, 8, COPIED_PARAM)
        + tidy_line(source, 9, USE_AFTER_MOVE)
    )
    runner = AnsweringRunner(git=review_git(), tidy=RunResult(exit_code=0, output=output))

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

    assert report.tiers[Tier.STYLE] == 6
    # danger order, not report order: the major finding was printed last
    assert [detail.finding.category for detail in report.detailed] == [
        USE_AFTER_MOVE,
        COPIED_PARAM,
    ]
    assert NULLPTR in [entry.category for entry in report.index]
    assert report.truncated is True


def test_only_the_actionable_tiers_are_ever_expanded() -> None:
    """The policy is data, so the tiers that earn detail are readable rather than traced."""
    assert DETAILED_TIERS == (Tier.CRITICAL, Tier.MAJOR, Tier.MINOR)


def test_the_index_speaks_root_relative_posix_whatever_the_tool_spelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    # what a tool prints on Windows: a drive letter and both separators in one path
    spelled = f"{root}/src/a.cpp"
    output = tidy_line(spelled, 3, USE_AFTER_MOVE) + tidy_line(spelled, 9, DANGLING)
    cache = tmp_path / "cache"
    runner = AnsweringRunner(git=review_git(root=root), tidy=RunResult(exit_code=0, output=output))

    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=cache,
            runner=runner,
        )
    )

    assert {entry.file for entry in report.index} == {"src/a.cpp"}
    places = {d.finding.location.file for d in report.detailed if d.finding.location}
    assert places == {"src/a.cpp"}
    # and the remembered run answers get_finding in the same language
    remembered = runs.find(cache, root, report.detailed[0].finding.fingerprint)
    assert isinstance(remembered, Finding)
    assert remembered.location is not None
    assert remembered.location.file == "src/a.cpp"


def test_rewriting_the_paths_leaves_every_fingerprint_where_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0002 is frozen: identity already hashed the canonical path, so making the
    visible path match it must move nothing. The line text still comes from the file the
    tool named, which is the half of the hash a rewritten path could quietly break."""
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    spelled = f"{root}/src/a.cpp"
    output = tidy_line(spelled, 3, USE_AFTER_MOVE) + tidy_line(spelled, 9, DANGLING)
    runner = AnsweringRunner(git=review_git(root=root), tidy=RunResult(exit_code=0, output=output))

    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    def read_as_the_tool_spelled_it(file: str, line: int) -> str:
        lines = Path(file).read_text(encoding="utf-8").splitlines()
        return lines[line - 1] if 1 <= line <= len(lines) else ""

    before = fingerprint_batch(
        parse_tidy(output), read_as_the_tool_spelled_it, canonical=relativizer(root)
    )
    identities = {entry.fingerprint for entry in report.index}
    assert {finding.fingerprint for finding in before} <= identities
    # known-answer, straight from the encoding: the flagged line's own text is in the hash,
    # so a rewrite that broke the read would fall out here instead of passing silently
    flagged = SOURCE.splitlines()[2]
    assert compute_fingerprint(USE_AFTER_MOVE, "src/a.cpp", flagged, 0) in identities
    assert compute_fingerprint(USE_AFTER_MOVE, "src/a.cpp", "", 0) not in identities


def test_the_report_names_the_compilation_database_that_decided_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    a_database(root, "build")
    runner = AnsweringRunner(
        git=review_git(root=root),
        tidy=RunResult(exit_code=0, output=tidy_line(f"{root}/src/a.cpp", 3, USE_AFTER_MOVE)),
    )

    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    assert report.database == "build/compile_commands.json"
    assert not [note for note in report.notes if "compilation database" in note]


def test_a_project_with_no_database_says_so_rather_than_naming_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    runner = AnsweringRunner(git=review_git())

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

    assert report.database is None


def test_several_build_trees_get_a_note_saying_which_one_was_chosen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Several build directories is the ordinary case, and they do not describe the same
    build; a report that never says which one it read explains none of its findings."""
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    a_database(root, "build")
    a_database(root, "build-debug")
    runner = AnsweringRunner(git=review_git(root=root))

    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    chosen = compile_db.find_under(root)
    assert chosen is not None
    assert report.database == chosen.relative_to(root).as_posix()
    (note,) = [note for note in report.notes if "compilation database" in note]
    assert report.database in note
    assert "2" in note


def test_an_audit_reports_the_same_contract_as_a_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    a_database(root, "build")
    spelled = f"{root}/src/a.cpp"
    output = tidy_line(spelled, 3, USE_AFTER_MOVE) + tidy_line(spelled, 7, NULLPTR)
    runner = AnsweringRunner(git=audit_git(root=root), tidy=RunResult(exit_code=0, output=output))

    report = audited(
        audit_project(
            root,
            record_as="main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    assert report.database == "build/compile_commands.json"
    assert set(report.tiers) == set(Tier)
    assert sum(report.tiers.values()) == report.total == len(report.index)
    assert report.tiers[Tier.MAJOR] == 1
    assert report.tiers[Tier.STYLE] == 1
    assert {entry.file for entry in report.index} == {"src/a.cpp"}
    assert [detail.finding.category for detail in report.detailed] == [USE_AFTER_MOVE]
    assert report.notes == ()


def test_every_index_entry_carries_the_tier_that_ranked_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    source = ROOT / "src/a.cpp"
    output = tidy_line(source, 3, USE_AFTER_MOVE) + tidy_line(source, 7, NULLPTR)
    runner = AnsweringRunner(git=review_git(), tidy=RunResult(exit_code=0, output=output))

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

    by_category = {entry.category: entry.tier for entry in report.index}
    assert by_category[USE_AFTER_MOVE] is Tier.MAJOR
    assert by_category[NULLPTR] is Tier.STYLE


def test_relocation_reaches_allocated_at_and_every_stack_frame(tmp_path: Path) -> None:
    """The contract is every location a caller reads, and a dynamic finding carries
    three kinds: its own, the allocation site, and each racing thread's frames."""
    raced = Finding(
        id="tsan-race",
        tool="tsan",
        severity=Severity.ERROR,
        category="data-race",
        message="race",
        location=Location(file=str(tmp_path / "src/a.cpp"), line=3),
        allocated_at=Location(file=str(tmp_path / "src/pool.cpp"), line=40),
        threads=(
            ThreadAccess(
                thread_id="T1",
                op=AccessOp.WRITE,
                size=8,
                locks_held=(),
                frames=(
                    Frame(
                        function="f", location=Location(file=str(tmp_path / "src/a.cpp"), line=9)
                    ),
                ),
            ),
        ),
    )

    (moved,) = _relocated([raced], relativizer(tmp_path))

    assert moved.location is not None and moved.location.file == "src/a.cpp"
    assert moved.allocated_at is not None and moved.allocated_at.file == "src/pool.cpp"
    assert moved.threads[0].frames[0].location is not None
    assert moved.threads[0].frames[0].location.file == "src/a.cpp"


def test_an_edited_nested_config_retires_the_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """clang-tidy answers to the nearest config above each file, so a nested one is as
    load-bearing as the root's -- editing it must retire the baseline, not slip past it."""
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    (root / "src" / ".clang-tidy").write_text("Checks: 'bugprone-*'\n", encoding="utf-8")
    source = root / "src/a.cpp"
    cache = tmp_path / "cache"

    audited(
        audit_project(
            root,
            record_as="main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=cache,
            runner=AnsweringRunner(
                git=audit_git(root=root),
                tidy=RunResult(exit_code=0, output=tidy_line(source, 3, USE_AFTER_MOVE)),
            ),
        )
    )

    (root / "src" / ".clang-tidy").write_text("Checks: 'modernize-*'\n", encoding="utf-8")
    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=cache,
            runner=AnsweringRunner(
                git=review_git(root=root),
                tidy=RunResult(exit_code=0, output=tidy_line(source, 3, USE_AFTER_MOVE)),
            ),
        )
    )

    assert report.baseline_used is False


def test_detail_carries_the_offered_fix_and_the_check_that_would_witness_the_defect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both extras ride beside the finding rather than on it: the Finding schema is frozen,
    and a fix is what a tool offers about a finding, not part of what it observed."""
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    spelled = f"{root}/src/a.cpp"
    runner = AnsweringRunner(
        git=review_git(root=root),
        tidy=RunResult(
            exit_code=0,
            output=tidy_line(spelled, 3, USE_AFTER_MOVE) + tidy_line(spelled, 5, MEMBER_INIT),
        ),
        compiler=RunResult(exit_code=0, output=thread_safety_line(spelled, 7)),
        fixes=an_export(
            check=MEMBER_INIT,
            file=spelled,
            offset=offset_in(root, "int line_5"),
            length=len("int"),
            text="long",
        ),
    )

    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    detail = {entry.finding.category: entry for entry in report.detailed}
    assert detail[MEMBER_INIT].suggested_fix == SuggestedFix(
        check=MEMBER_INIT, file="src/a.cpp", at=5, line=5, replaced="int", replacement="long"
    )
    # no runtime tool watches for an uninitialized member, so nothing is named
    assert detail[MEMBER_INIT].verify_with is None
    # the check offered no fix for either of these, and their findings still stand
    assert detail[USE_AFTER_MOVE].suggested_fix is None
    # a moved-from object is alive and reading it is legal C++: nothing runtime traps it
    assert detail[USE_AFTER_MOVE].verify_with is None
    assert detail[THREAD_SAFETY].suggested_fix is None
    assert detail[THREAD_SAFETY].verify_with == "tsan"


def test_two_diagnostics_of_one_check_in_one_file_each_keep_their_own_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The trap is misattribution: keyed by file and check alone, the second finding
    would wear the first finding's edit, which is worse than carrying none."""
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    spelled = f"{root}/src/a.cpp"
    first, second = offset_in(root, "int line_3"), offset_in(root, "int line_9")
    # one document, two entries: how tidy really writes it, and what safe_load reads
    both = an_export(
        check=MEMBER_INIT, file=spelled, offset=first, length=len("int"), text="long"
    ).replace(
        "    Level:           Warning",
        f"""  - DiagnosticName:  '{MEMBER_INIT}'
    DiagnosticMessage:
      Message:         'something worth hearing'
      FilePath:        '{spelled}'
      FileOffset:      {second}
      Replacements:
        - FilePath:        '{spelled}'
          Offset:          {second}
          Length:          {len("int")}
          ReplacementText: 'short'
    Level:           Warning""",
    )
    runner = AnsweringRunner(
        git=review_git(root=root),
        tidy=RunResult(
            exit_code=0,
            output=tidy_line(spelled, 3, MEMBER_INIT) + tidy_line(spelled, 9, MEMBER_INIT),
        ),
        fixes=both,
    )

    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    by_line = {
        entry.finding.location.line: entry.suggested_fix
        for entry in report.detailed
        if entry.finding.location is not None and entry.finding.category == MEMBER_INIT
    }
    assert by_line[3] is not None and by_line[3].replacement == "long"
    assert by_line[9] is not None and by_line[9].replacement == "short"


def test_an_export_naming_a_check_nobody_reported_attaches_to_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Suggestions are matched, never assumed: a fix with no finding of its own must not
    be hung on the finding that happens to sit in the detail slot."""
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    spelled = f"{root}/src/a.cpp"
    runner = AnsweringRunner(
        git=review_git(root=root),
        tidy=RunResult(exit_code=0, output=tidy_line(spelled, 3, USE_AFTER_MOVE)),
        fixes=an_export(check=MEMBER_INIT, file=spelled, offset=0, length=len("int"), text="long"),
    )

    report = reviewed(
        review_project(
            root,
            "main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    (detail,) = report.detailed
    assert detail.finding.category == USE_AFTER_MOVE
    assert detail.suggested_fix is None


def test_an_audits_detail_carries_the_same_extras_a_reviews_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blind_path(monkeypatch)
    root = a_checkout(tmp_path)
    spelled = f"{root}/src/a.cpp"
    runner = AnsweringRunner(
        git=audit_git(root=root),
        tidy=RunResult(exit_code=0, output=tidy_line(spelled, 5, MEMBER_INIT)),
        fixes=an_export(
            check=MEMBER_INIT,
            file=spelled,
            offset=offset_in(root, "int line_5"),
            length=len("int"),
            text="long",
        ),
    )

    report = audited(
        audit_project(
            root,
            record_as="main",
            toolchain=a_clang(),
            platform=a_platform(tmp_path),
            capabilities=working(),
            cache_dir=tmp_path / "cache",
            runner=runner,
        )
    )

    (detail,) = report.detailed
    assert detail.finding.category == MEMBER_INIT
    assert detail.suggested_fix is not None
    assert detail.suggested_fix.file == "src/a.cpp"


def test_the_index_stays_thin_while_the_detail_grows() -> None:
    """The index lists everything, so an extra field there is paid for once per finding --
    which is the bloat the index/detail split exists to prevent."""
    assert {entry.name for entry in fields(IndexEntry)} == {
        "fingerprint",
        "tier",
        "severity",
        "category",
        "file",
        "line",
        "occurrences",
    }
