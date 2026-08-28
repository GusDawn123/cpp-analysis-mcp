"""Run the whole sanitize chain against the real compiler and the real fixtures.

The unit suite replays captured output through a fake process; this proves the real loop
end to end -- a dropped runtime environment, the wrong binary, or the wrong parser would
all look like the same empty report. The clean fixture proves that all-clear is earned.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from helpers import FIXTURES_DIR, bug_line, cpp_source

from cpp_analysis_mcp import platforms
from cpp_analysis_mcp.capabilities import discover_toolchains, probe_all
from cpp_analysis_mcp.pipelines.sanitize import analyze_file, analyze_project, analyze_snippet
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.store.models import Analysis, AnalysisReport, CapabilityStatus
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration

Capabilities = dict[Analysis, CapabilityStatus]

CMAKE_PROJECT = FIXTURES_DIR / "cmake_project"

DATA_RACE = "data_race"
CLEAN = "clean"
SIGNED_OVERFLOW = "signed_overflow"
HEAP_OVERFLOW = "heap_overflow"
LEAK = "leak"

RACE_CATEGORY = "data-race"
OVERFLOW_CATEGORY = "signed-integer-overflow"
HEAP_OVERFLOW_CATEGORY = "heap-buffer-overflow"
# what lsan.parse names a Direct leak record, pinned the way tests/unit/parsers/test_lsan.py
# pins it
LEAK_CATEGORY = "direct-leak"

# a racy program with no fixture file behind it, so the snippet path has to materialize it
RACY_SNIPPET = """\
#include <thread>

namespace {

int counter = 0;

void bump() {
    for (int i = 0; i < 100000; ++i) {
        ++counter;
    }
}

}  // namespace

int main() {
    std::thread first(bump);
    std::thread second(bump);
    first.join();
    second.join();
    return 0;
}
"""


@pytest.fixture(scope="module")
def host() -> Platform:
    return platforms.detect()


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    """clang, because the assertions below pin the line a report blames.

    gcc attributes the same planted race to a different line -- the committed goldens show
    it naming the for-statement where clang names the increment -- so a suite that took
    either compiler would have to stop pinning the thing it is here to check. clang is also
    the one compiler present on all three target platforms.
    """
    found = [chain for chain in discover_toolchains() if chain.family == "clang"]
    if not found:
        pytest.skip("no clang on this machine")
    return found[0]


@pytest.fixture(scope="module")
def capabilities(toolchain: Toolchain, host: Platform) -> Capabilities:
    """Probe once for the module, with the cache off so these are today's answers, not a file
    some earlier run wrote."""
    return probe_all(toolchain, host, cache_dir=None)


def require(capabilities: Capabilities, analysis: Analysis) -> None:
    """Skip rather than fail when this machine cannot do the analysis under test."""
    status = capabilities[analysis]
    if not status.available:
        pytest.skip(f"{analysis} is unavailable here: {status.reason}")


def analyze(
    stem: str,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    host: Platform,
    capabilities: Capabilities,
    tmp_path: Path,
) -> AnalysisReport | CapabilityStatus:
    result = analyze_file(
        cpp_source(stem),
        analysis,
        toolchain=toolchain,
        platform=host,
        capabilities=capabilities,
        build_dir=tmp_path / analysis.value,
    )
    assert isinstance(result, AnalysisReport | CapabilityStatus), f"the build failed: {result}"
    return result


def reported(result: AnalysisReport | CapabilityStatus) -> AnalysisReport:
    assert isinstance(result, AnalysisReport), f"expected an AnalysisReport, got {result}"
    return result


def categories(report: AnalysisReport) -> list[str]:
    return [finding.category for finding in report.findings]


def test_tsan_finds_the_race_on_the_line_the_fixture_marks(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """The whole chain: gate, build, run under the pinned options, parse, blame a line."""
    require(capabilities, Analysis.TSAN)

    report = reported(
        analyze(
            DATA_RACE,
            Analysis.TSAN,
            toolchain=toolchain,
            host=host,
            capabilities=capabilities,
            tmp_path=tmp_path,
        )
    )

    races = [finding for finding in report.findings if finding.category == RACE_CATEGORY]
    assert races, f"TSan reported no race: exit {report.exit_code}, {categories(report)}"
    assert races[0].location is not None
    assert races[0].location.line == bug_line(DATA_RACE)
    assert report.verified_by, "a report with no proof behind it"


def test_a_clean_program_produces_a_trustworthy_all_clear(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """Only means something because the test above showed this chain reports when it should."""
    require(capabilities, Analysis.TSAN)

    report = reported(
        analyze(
            CLEAN,
            Analysis.TSAN,
            toolchain=toolchain,
            host=host,
            capabilities=capabilities,
            tmp_path=tmp_path,
        )
    )

    assert report.findings == ()
    assert report.exit_code == 0
    assert report.timed_out is False


def test_ubsan_reports_while_the_program_exits_zero(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """UBSan prints and lets the program finish, so exit 0 is not "nothing to read"."""
    require(capabilities, Analysis.UBSAN)

    report = reported(
        analyze(
            SIGNED_OVERFLOW,
            Analysis.UBSAN,
            toolchain=toolchain,
            host=host,
            capabilities=capabilities,
            tmp_path=tmp_path,
        )
    )

    assert OVERFLOW_CATEGORY in categories(report)
    assert report.exit_code == 0


def test_asan_reports_the_overflow_and_the_program_dies(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    require(capabilities, Analysis.ASAN)

    report = reported(
        analyze(
            HEAP_OVERFLOW,
            Analysis.ASAN,
            toolchain=toolchain,
            host=host,
            capabilities=capabilities,
            tmp_path=tmp_path,
        )
    )

    assert [category for category in categories(report) if HEAP_OVERFLOW_CATEGORY in category]
    assert report.exit_code != 0


def test_a_snippet_is_compiled_and_analyzed_like_a_file(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """Text in, findings out: nothing about the source existed before the call."""
    require(capabilities, Analysis.TSAN)
    build_dir = tmp_path / "snippet"

    result = analyze_snippet(
        RACY_SNIPPET,
        Analysis.TSAN,
        toolchain=toolchain,
        platform=host,
        capabilities=capabilities,
        build_dir=build_dir,
    )

    assert isinstance(result, AnalysisReport), f"the snippet did not build: {result}"
    assert RACE_CATEGORY in categories(result)
    # the file stays behind, because every frame in that report names it
    assert (build_dir / "snippet.cpp").is_file()


def test_the_fixture_project_builds_and_reports_its_planted_bug(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """cmake's File API names the binary, and the chain runs that one, not a guess."""
    require(capabilities, Analysis.ASAN)
    if shutil.which("cmake") is None:
        pytest.skip("no cmake on this machine")

    result = analyze_project(
        CMAKE_PROJECT,
        Analysis.ASAN,
        toolchain=toolchain,
        platform=host,
        capabilities=capabilities,
        build_dir=tmp_path / "project",
    )

    assert isinstance(result, AnalysisReport), f"the project did not build: {result}"
    assert [category for category in categories(result) if HEAP_OVERFLOW_CATEGORY in category]
    assert result.exit_code != 0


def test_lsan_either_finds_the_leak_or_says_why_it_cannot(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """macOS arm64 has no LeakSanitizer, and saying so is the honest answer, not silence.

    Both branches are the same guarantee from opposite sides: a caller never gets an empty
    finding list from a detector that was not running.
    """
    status = capabilities[Analysis.LSAN]

    result = analyze(
        LEAK,
        Analysis.LSAN,
        toolchain=toolchain,
        host=host,
        capabilities=capabilities,
        tmp_path=tmp_path,
    )

    if not status.available:
        assert isinstance(result, CapabilityStatus), f"a denied analysis still ran: {result}"
        assert result.reason == status.reason
        assert result.reason, "unavailable and saying nothing"
        return

    report = reported(result)
    assert LEAK_CATEGORY in categories(report), f"LSan reported no leak: {categories(report)}"
