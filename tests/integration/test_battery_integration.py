"""Run the whole battery against the real compiler and a fixture with a planted bug.

The unit suite proves the merge over faked processes; this proves the real composition:
capability probes decide what runs, six pipelines execute in parallel, and the planted
unguarded write must surface wherever thread-safety analysis is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from helpers import cpp_source

from cpp_analysis_mcp import battery, platforms, process
from cpp_analysis_mcp.capabilities import discover_toolchains, probe_all
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import Analysis, CapabilityStatus
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class NativeEngine:
    toolchain: Toolchain
    platform: Platform
    runner: Runner


@pytest.fixture(scope="module")
def host() -> Platform:
    return platforms.detect()


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    found = [chain for chain in discover_toolchains() if chain.family == "clang"]
    if not found:
        pytest.skip("no clang on this machine")
    return found[0]


@pytest.fixture(scope="module")
def capabilities(toolchain: Toolchain, host: Platform) -> dict[Analysis, CapabilityStatus]:
    return probe_all(toolchain, host, cache_dir=None)


def test_the_battery_catches_the_planted_unguarded_write(
    toolchain: Toolchain,
    host: Platform,
    capabilities: dict[Analysis, CapabilityStatus],
    tmp_path: Path,
) -> None:
    engine = NativeEngine(toolchain=toolchain, platform=host, runner=process.run)
    report = battery.check_file(
        cpp_source("unguarded_write"),
        engines=dict.fromkeys(battery.CORRECTNESS, engine),
        capabilities=capabilities,
        build_dir=tmp_path / "battery",
    )

    accounted = set(report.ran) | set(report.unavailable) | set(report.failed_builds)
    assert accounted == {analysis.value for analysis in battery.CORRECTNESS}
    assert len(report.ran) + len(report.unavailable) + len(report.failed_builds) == len(
        battery.CORRECTNESS
    )

    if "thread-safety" in report.ran:
        categories = {finding.category for finding in report.findings}
        assert "thread-safety-analysis" in categories
        assert report.next_step == battery.FIX_AND_RERUN
