"""Race real binaries built by the real compiler, and require the ranking to be right.

The unit suite proves the methodology over a fake clock; this proves the whole loop on
real hardware. Two programs print the same line, one does five orders of magnitude more
work, and the race has to put the light one first with real statistics attached. Then a
variant that is instant but answers differently has to lose anyway, because the
same-answer rule is the tool's whole warranty.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cpp_analysis_mcp import platforms
from cpp_analysis_mcp.capabilities import discover_toolchains
from cpp_analysis_mcp.models import BenchmarkReport, Variant
from cpp_analysis_mcp.pipelines.benchmark import DIFFERS, race
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration

ANSWER_LINE = "done 42"

# a serially dependent multiply chain: -O2 cannot fold it away and cannot vectorize it,
# so iterations translate honestly into wall time. The sink keeps the loop alive.
HEAVY = """\
#include <cstdio>

int main() {
    volatile unsigned long long sink = 0;
    unsigned long long acc = 1;
    for (long i = 0; i < 150000000L; ++i) {
        acc = acc * 6364136223846793005ULL + 1442695040888963407ULL;
    }
    sink = acc;
    (void)sink;
    std::printf("done 42\\n");
    return 0;
}
"""

LIGHT = HEAVY.replace("150000000L", "1000L")

WRONG_AND_INSTANT = """\
#include <cstdio>

int main() {
    std::printf("done 43\\n");
    return 0;
}
"""


@pytest.fixture(scope="module")
def host() -> Platform:
    return platforms.detect()


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    """Any discovered compiler will do: a race needs no sanitizer runtime and no perf."""
    found = discover_toolchains()
    if not found:
        pytest.skip("no C++ compiler on this machine")
    return found[0]


def run_race(
    variants: list[Variant], *, toolchain: Toolchain, host: Platform, tmp_path: Path
) -> BenchmarkReport:
    outcome = race(
        variants,
        toolchain=toolchain,
        platform=host,
        build_dir=tmp_path / "race",
        repeats=2,
    )
    assert isinstance(outcome, BenchmarkReport), f"the race did not report: {outcome}"
    return outcome


def test_the_lighter_program_wins_with_real_times(
    toolchain: Toolchain, host: Platform, tmp_path: Path
) -> None:
    report = run_race(
        [Variant(name="heavy", code=HEAVY), Variant(name="light", code=LIGHT)],
        toolchain=toolchain,
        host=host,
        tmp_path=tmp_path,
    )

    assert [result.name for result in report.variants] == ["light", "heavy"]
    light, heavy = report.variants
    assert light.matches_baseline and heavy.matches_baseline
    assert light.mean_ms is not None and heavy.mean_ms is not None
    assert light.mean_ms < heavy.mean_ms
    assert light.runs == 2 and heavy.runs == 2
    assert light.stddev_ms is not None
    assert report.next_step is not None
    assert report.next_step.startswith("light won")


def test_an_instant_wrong_answer_still_loses(
    toolchain: Toolchain, host: Platform, tmp_path: Path
) -> None:
    """The warranty on real binaries: speed buys nothing once the output changed."""
    report = run_race(
        [Variant(name="heavy", code=HEAVY), Variant(name="cheat", code=WRONG_AND_INSTANT)],
        toolchain=toolchain,
        host=host,
        tmp_path=tmp_path,
    )

    cheat = next(result for result in report.variants if result.name == "cheat")
    assert cheat.rejected == DIFFERS
    assert cheat.mean_ms is None
    survivor = next(result for result in report.variants if result.name == "heavy")
    assert survivor.rejected is None
    assert survivor.runs == 2
