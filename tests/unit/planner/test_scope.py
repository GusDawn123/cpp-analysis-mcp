"""One canonical spelling per path, so identity cannot depend on who spelled it.

The resolver feeds fingerprinting: paths under the root come back project-relative
POSIX, paths outside it stay whole, and relative spellings pass through untouched
because only the tool that printed them knows what they were relative to.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from cpp_analysis_mcp.planner.scope import relativizer


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
