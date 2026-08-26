"""Pin the coz parser against a committed real capture.

The golden came from a program built for this test: two threads per work item, a heavy
loop on the critical path and a light one off it. The physics is the assertion. Virtual
speedup on the heavy line must convert into program speedup, and the same treatment on
the light line must convert into roughly nothing, because the heavy thread still decides
when each item finishes.
"""

from __future__ import annotations

from helpers import GOLDEN_DIR

from cpp_analysis_mcp.models import CausalPoint, Location
from cpp_analysis_mcp.parsers import coz

GOLDEN = (GOLDEN_DIR / "coz_critical_path.linux-clang.txt").read_text(encoding="utf-8")

HEAVY_LINE = 13
LIGHT_LINES = (20, 21)


def heavy_points() -> list[CausalPoint]:
    return [point for point in coz.parse(GOLDEN) if point.location.line == HEAVY_LINE]


def test_the_heavy_line_converts_virtual_speedup_into_program_speedup() -> None:
    best = max(heavy_points(), key=lambda point: point.virtual_speedup_pct)

    assert best.virtual_speedup_pct == 95.0
    # the heavy loop carries most of each item's critical path, so most of the virtual
    # speedup must show up end to end; the band is wide because one capture is one sample
    assert 40.0 < best.program_speedup_pct < 96.0


def test_the_light_line_moves_the_program_almost_not_at_all() -> None:
    light = [point for point in coz.parse(GOLDEN) if point.location.line in LIGHT_LINES]

    assert light
    for point in light:
        assert abs(point.program_speedup_pct) < 20.0


def test_more_virtual_speedup_on_the_heavy_line_means_more_program_speedup() -> None:
    ordered = sorted(heavy_points(), key=lambda point: point.virtual_speedup_pct)

    assert len(ordered) >= 4
    assert ordered[-1].program_speedup_pct > ordered[0].program_speedup_pct


def test_the_trust_numbers_come_from_the_capture() -> None:
    assert coz.experiment_count(GOLDEN) == 40
    assert coz.baseline_count(GOLDEN) >= 3
    assert coz.progress_points(GOLDEN) == ("item",)


def test_statements_price_each_line_at_its_largest_tried_speedup() -> None:
    lines = coz.statements(coz.parse(GOLDEN))

    heavy = [line for line in lines if f":{HEAVY_LINE}" in line]
    assert len(heavy) == 1
    assert "sped the whole program up" in heavy[0]
    flat = [line for line in lines if "not on the critical path" in line]
    assert flat


def test_locations_are_split_into_file_and_line() -> None:
    point = heavy_points()[0]

    assert point.location == Location(file="/tmp/causal_demo.cpp", line=HEAVY_LINE)


def test_an_empty_or_baseline_free_capture_yields_no_points() -> None:
    assert coz.parse("") == ()
    only_experiment = (
        '{"type":"experiment","selected":"a.cpp:1","speedup":0.50,"duration":100,'
        '"selected_samples":5}\n{"type":"throughput_point","name":"item","delta":10}\n'
    )
    assert coz.parse(only_experiment) == ()


def test_a_trailing_unmeasured_experiment_is_dropped() -> None:
    text = (
        '{"type":"experiment","selected":"a.cpp:1","speedup":0.00,"duration":100,'
        '"selected_samples":5}\n{"type":"throughput_point","name":"item","delta":10}\n'
        '{"type":"experiment","selected":"a.cpp:1","speedup":0.50,"duration":100,'
        '"selected_samples":5}\n'
    )

    assert coz.experiment_count(text) == 1
    assert coz.parse(text) == ()
