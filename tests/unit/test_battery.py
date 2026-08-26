"""Drive the whole battery with no compiler and no child process anywhere.

The runner answers by command content rather than call order, because the six analyses
run in parallel and their order is not promised. The pipelines, the capability gates and
the parsers underneath are all the real code; two of the replies are committed goldens.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from helpers import GOLDEN_DIR

from cpp_analysis_mcp import battery
from cpp_analysis_mcp.models import Analysis, CapabilityStatus, FullCheckReport
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.toolchains.base import Toolchain

TIDY_PATH = str(Path("/usr/bin/clang-tidy"))

TSAN_GOLDEN = (GOLDEN_DIR / "tsan_data_race.darwin-clang.txt").read_text(encoding="utf-8")
THREAD_SAFETY_GOLDEN = (GOLDEN_DIR / "thread_safety_unguarded_write.darwin-clang.txt").read_text(
    encoding="utf-8"
)

# one diagnostics-shaped warning, planted in every sanitizer compile to prove deduplication
REPEATED_WARNING = (
    "widget.cpp:21:5: warning: writing variable 'count' requires holding mutex 'lock' "
    "exclusively [-Wthread-safety-analysis]"
)


@dataclass
class BatteryRunner:
    """Answer each spawned command by what it is, whatever order the threads pick."""

    sanitizer_compile_output: str = ""
    ubsan_compile_fails: bool = False
    tsan_run: RunResult = field(default_factory=lambda: RunResult(exit_code=0, output=""))
    compile_targets: list[str] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        listed = list(cmd)
        if Path(listed[0]).name == "clang-tidy":
            return RunResult(exit_code=0, output="")
        if "-fsyntax-only" in listed:
            return RunResult(exit_code=0, output=THREAD_SAFETY_GOLDEN)
        sanitize_flag = next((arg for arg in listed if arg.startswith("-fsanitize=")), None)
        if sanitize_flag is not None:
            self.compile_targets.append(listed[listed.index("-o") + 1])
            if self.ubsan_compile_fails and sanitize_flag == "-fsanitize=undefined":
                return RunResult(exit_code=1, output="/usr/bin/ld: cannot find -lubsan")
            return RunResult(exit_code=0, output=self.sanitizer_compile_output)
        kind = Path(listed[0]).name.rsplit(".", 1)[-1]
        if kind == "thread":
            return self.tsan_run
        return RunResult(exit_code=0, output="")


def a_clang() -> Toolchain:
    return Toolchain(
        family="clang",
        compiler=Path("/usr/bin/clang++"),
        version="Apple clang version 21.0.0",
        warning_flags=("-Wthread-safety",),
    )


@dataclass(frozen=True)
class FakeEngine:
    toolchain: Toolchain
    platform: Platform
    runner: BatteryRunner


def run_battery(
    tmp_path: Path,
    runner: BatteryRunner,
    monkeypatch: pytest.MonkeyPatch,
    capabilities: dict[Analysis, CapabilityStatus] | None = None,
) -> FullCheckReport:
    monkeypatch.setattr(shutil, "which", lambda name: TIDY_PATH)
    source = tmp_path / "widget.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")
    engine = FakeEngine(a_clang(), Platform(name="darwin"), runner)
    return battery.check_file(
        source,
        engines=dict.fromkeys(battery.CORRECTNESS, engine),
        capabilities=capabilities
        or dict.fromkeys(Analysis, CapabilityStatus(available=True, verified_by="probed")),
        build_dir=tmp_path / "battery",
    )


def test_every_detector_reports_into_one_merged_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BatteryRunner(tsan_run=RunResult(exit_code=66, output=TSAN_GOLDEN))
    report = run_battery(tmp_path, runner, monkeypatch)

    categories = {finding.category for finding in report.findings}
    assert "data-race" in categories
    assert "thread-safety-analysis" in categories
    assert set(report.ran) == {analysis.value for analysis in battery.CORRECTNESS}
    assert dict(report.unavailable) == {}
    assert dict(report.failed_builds) == {}


def test_the_same_compile_warning_from_four_builds_lands_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BatteryRunner(sanitizer_compile_output=REPEATED_WARNING + "\n")
    report = run_battery(tmp_path, runner, monkeypatch)

    repeated = [finding for finding in report.findings if "mutex 'lock'" in finding.message]
    assert len(repeated) == 1


def test_an_unavailable_analysis_is_listed_and_the_rest_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capabilities = dict.fromkeys(Analysis, CapabilityStatus(available=True, verified_by="probed"))
    capabilities[Analysis.LSAN] = CapabilityStatus(
        available=False, reason="LeakSanitizer is Linux-only; it does not run on macOS arm64"
    )
    report = run_battery(tmp_path, BatteryRunner(), monkeypatch, capabilities)

    assert dict(report.unavailable) == {"lsan": capabilities[Analysis.LSAN].reason}
    assert "lsan" not in report.ran
    assert len(report.ran) == len(battery.CORRECTNESS) - 1


def test_one_failed_build_does_not_stop_the_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = run_battery(tmp_path, BatteryRunner(ubsan_compile_fails=True), monkeypatch)

    assert "ubsan" in report.failed_builds
    assert "ubsan" not in report.ran
    assert len(report.ran) == len(battery.CORRECTNESS) - 1


def test_a_clean_battery_says_nothing_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = run_battery(tmp_path, BatteryRunner(), monkeypatch)

    clean = [finding for finding in report.findings if finding.category == "data-race"]
    assert clean == []
    # thread-safety golden still reports its planted warning, so next_step stays set;
    # a battery with findings must always point back at the rerun
    assert report.next_step == battery.FIX_AND_RERUN


def test_each_sanitizer_builds_in_its_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BatteryRunner()
    run_battery(tmp_path, runner, monkeypatch)

    parents = {Path(target).parent.name for target in runner.compile_targets}
    assert parents == {"tsan", "asan", "lsan", "ubsan"}
