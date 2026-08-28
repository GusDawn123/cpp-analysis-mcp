"""Run the sanitize chain through the real WSL bridge, on a machine that has one.

The unit suite proves the bridge composes the right commands; this proves they still work:
a real distro, TSan/LSan against real planted bugs, blamed from inside a Linux the caller
never sees. Skips without a clang-capable distro -- all of CI -- where unit fakes cover it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import bug_line, cpp_source

from cpp_analysis_mcp import wsl
from cpp_analysis_mcp.capabilities import probe_all
from cpp_analysis_mcp.context import resolve
from cpp_analysis_mcp.pipelines.sanitize import analyze_file
from cpp_analysis_mcp.store.models import Analysis, AnalysisReport, CapabilityStatus

pytestmark = pytest.mark.integration

Capabilities = dict[Analysis, CapabilityStatus]

DATA_RACE = "data_race"
LEAK = "leak"
CLEAN = "clean"

RACE_CATEGORY = "data-race"
LEAK_CATEGORY = "direct-leak"


@pytest.fixture(scope="module")
def bridge() -> wsl.Bridge:
    found = wsl.discover()
    if found is None:
        pytest.skip("no WSL distro with clang on this machine")
    return found


@pytest.fixture(scope="module")
def capabilities(bridge: wsl.Bridge) -> Capabilities:
    """Probe through the bridge once for the module; the cache is off so these are today's
    answers from the real distro, not a file some earlier run wrote."""
    return probe_all(bridge.toolchain, bridge.platform, cache_dir=None, runner=bridge.runner)


def require(capabilities: Capabilities, analysis: Analysis) -> None:
    status = capabilities[analysis]
    if not status.available:
        pytest.skip(f"{analysis} is unavailable through this bridge: {status.reason}")


def analyzed(
    stem: str,
    analysis: Analysis,
    *,
    bridge: wsl.Bridge,
    capabilities: Capabilities,
    tmp_path: Path,
) -> AnalysisReport:
    result = analyze_file(
        cpp_source(stem),
        analysis,
        toolchain=bridge.toolchain,
        platform=bridge.platform,
        capabilities=capabilities,
        build_dir=tmp_path / analysis.value,
        runner=bridge.runner,
    )
    assert isinstance(result, AnalysisReport), f"the bridged build failed: {result}"
    return result


def test_tsan_blames_the_fixtures_line_from_inside_the_distro(
    bridge: wsl.Bridge, capabilities: Capabilities, tmp_path: Path
) -> None:
    """The whole point of the bridge, end to end: a Windows path in, a race report out,
    with the planted line blamed and the paths readable in their /mnt/ spelling."""
    require(capabilities, Analysis.TSAN)

    report = analyzed(
        DATA_RACE, Analysis.TSAN, bridge=bridge, capabilities=capabilities, tmp_path=tmp_path
    )

    races = [finding for finding in report.findings if finding.category == RACE_CATEGORY]
    assert races, f"TSan reported no race: exit {report.exit_code}"
    assert races[0].location is not None
    assert races[0].location.line == bug_line(DATA_RACE)
    # the limitation that explains the path spelling travels on the report itself
    assert any("/mnt/" in note for note in report.limitations)


def test_lsan_finds_the_leak_from_inside_the_distro(
    bridge: wsl.Bridge, capabilities: Capabilities, tmp_path: Path
) -> None:
    require(capabilities, Analysis.LSAN)

    report = analyzed(
        LEAK, Analysis.LSAN, bridge=bridge, capabilities=capabilities, tmp_path=tmp_path
    )

    assert LEAK_CATEGORY in [finding.category for finding in report.findings]


def test_resolve_routes_tsan_onto_this_bridge_end_to_end(tmp_path: Path) -> None:
    """resolve() must discover this bridge on its own and reroute onto it -- the seam the
    direct tests above skip. A routing regression would pass them and fail only here."""
    context = resolve(cache_dir=None, workspace=tmp_path / "workspace")
    engine = context.engines[Analysis.TSAN]
    if engine.platform.name != "wsl":
        pytest.skip("resolve() found no bridge on this machine")

    result = analyze_file(
        cpp_source(DATA_RACE),
        Analysis.TSAN,
        toolchain=engine.toolchain,
        platform=engine.platform,
        capabilities=context.capabilities,
        build_dir=tmp_path / "routed",
        runner=engine.runner,
    )

    assert isinstance(result, AnalysisReport), f"the routed build failed: {result}"
    assert RACE_CATEGORY in [finding.category for finding in result.findings]


def test_a_clean_program_comes_back_clean_through_the_bridge(
    bridge: wsl.Bridge, capabilities: Capabilities, tmp_path: Path
) -> None:
    """Only means something next to the tests above: this chain reports when it should, so
    an empty finding list from it is an all-clear rather than a broken pipe."""
    require(capabilities, Analysis.TSAN)

    report = analyzed(
        CLEAN, Analysis.TSAN, bridge=bridge, capabilities=capabilities, tmp_path=tmp_path
    )

    assert report.findings == ()
    assert report.exit_code == 0
    assert report.timed_out is False
