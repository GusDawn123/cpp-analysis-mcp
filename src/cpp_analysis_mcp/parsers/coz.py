"""Turn a coz profile into measured cause-and-effect points.

The file is JSON lines. Each experiment row says which source line coz virtually sped up,
by how much, and for how long; the throughput_point row after it says how many work items
completed in that window. Progress rate against the zero-speedup baseline is the whole
analysis: a line on the critical path converts virtual speedup into program speedup, and
a hot line off it converts to nothing.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterator, Sequence

from cpp_analysis_mcp.models import CausalPoint, Location

# an estimate resting on one experiment is an anecdote; coz repeats settled lines
MIN_BASELINE = 1

FLAT_STATEMENT = (
    "speeding up {where} by {virtual:.0f}% moved the whole program by {program:.0f}%; "
    "the time it burns is not on the critical path"
)
PAYS_STATEMENT = "speeding up {where} by {virtual:.0f}% sped the whole program up by {program:.0f}%"

# below this measured program effect a line's best case is indistinguishable from noise
FLAT_PCT = 5.0


def parse(text: str) -> tuple[CausalPoint, ...]:
    """Group experiments by (line, virtual speedup) and price each against the baseline."""
    pairs = list(_paired(text))
    baseline_periods = [
        duration / delta for _, speedup, duration, delta in pairs if speedup == 0.0 and delta > 0
    ]
    if len(baseline_periods) < MIN_BASELINE:
        return ()
    baseline = statistics.fmean(baseline_periods)

    grouped: dict[tuple[str, float], list[float]] = {}
    for selected, speedup, duration, delta in pairs:
        if speedup == 0.0 or delta <= 0:
            continue
        grouped.setdefault((selected, speedup), []).append(duration / delta)

    points = (
        CausalPoint(
            location=_location(selected),
            virtual_speedup_pct=round(speedup * 100, 1),
            program_speedup_pct=round((1 - statistics.fmean(periods) / baseline) * 100, 1),
            experiments=len(periods),
        )
        for (selected, speedup), periods in grouped.items()
    )
    return tuple(
        sorted(
            points,
            key=lambda point: (point.location.file, point.location.line, point.virtual_speedup_pct),
        )
    )


def baseline_count(text: str) -> int:
    return sum(1 for _, speedup, _, delta in _paired(text) if speedup == 0.0 and delta > 0)


def experiment_count(text: str) -> int:
    return sum(1 for _ in _paired(text))


def progress_points(text: str) -> tuple[str, ...]:
    names: list[str] = []
    for row in _rows(text):
        name = row.get("name")
        if row.get("type") == "throughput_point" and isinstance(name, str) and name not in names:
            names.append(name)
    return tuple(names)


def statements(points: Sequence[CausalPoint]) -> tuple[str, ...]:
    """One sentence per line, at the largest virtual speedup that was tried on it."""
    best: dict[tuple[str, int], CausalPoint] = {}
    for point in points:
        key = (point.location.file, point.location.line)
        held = best.get(key)
        if held is None or point.virtual_speedup_pct > held.virtual_speedup_pct:
            best[key] = point

    lines = []
    for point in best.values():
        shape = FLAT_STATEMENT if point.program_speedup_pct < FLAT_PCT else PAYS_STATEMENT
        lines.append(
            shape.format(
                where=f"{point.location.file}:{point.location.line}",
                virtual=point.virtual_speedup_pct,
                program=point.program_speedup_pct,
            )
        )
    return tuple(sorted(lines))


def _paired(text: str) -> Iterator[tuple[str, float, int, int]]:
    """Yield (selected, speedup, duration, delta): each experiment with the progress row
    that follows it. An experiment the file ends on has no measurement and is dropped."""
    held: dict[str, object] | None = None
    for row in _rows(text):
        kind = row.get("type")
        if kind == "experiment":
            held = row
        elif kind == "throughput_point" and held is not None:
            selected = held.get("selected")
            speedup = held.get("speedup")
            duration = held.get("duration")
            delta = row.get("delta")
            held = None
            if (
                isinstance(selected, str)
                and isinstance(speedup, int | float)
                and isinstance(duration, int)
                and isinstance(delta, int)
            ):
                yield selected, float(speedup), duration, delta


def _rows(text: str) -> Iterator[dict[str, object]]:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def _location(selected: str) -> Location:
    path, _, line = selected.rpartition(":")
    return Location(file=path, line=int(line) if line.isdigit() else 0)
