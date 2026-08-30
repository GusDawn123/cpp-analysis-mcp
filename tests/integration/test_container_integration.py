"""The container engine against this machine's real Docker: compile, run, and lint inside
the toolbox. Skipped with discovery's own reason wherever Docker or the image is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cpp_analysis_mcp import container, process

COMPILE_TIMEOUT_S = 120

NULLPTR_BAIT = "int main() { int* p = 0; return p == 0 ? 0 : 1; }\n"


@pytest.fixture
def bridge(tmp_path: Path) -> container.Bridge:
    settled = container.discover(workspace=tmp_path, runner=process.run)
    if isinstance(settled, container.Absence):
        pytest.skip(settled.reason)
    return settled


def test_a_compile_and_run_inside_the_toolbox(bridge: container.Bridge, tmp_path: Path) -> None:
    source = tmp_path / "hello.cpp"
    source.write_text('#include <cstdio>\nint main() { std::puts("from the toolbox"); }\n')
    binary = tmp_path / "hello"

    compiled = bridge.runner(
        ["clang++", str(source), "-o", str(binary)], timeout_s=COMPILE_TIMEOUT_S
    )
    assert compiled.exit_code == 0, compiled.output

    ran = bridge.runner([str(binary)], timeout_s=30)
    assert ran.exit_code == 0
    assert "from the toolbox" in ran.output


def test_tidy_reports_inside_the_toolbox_in_host_spelling(
    bridge: container.Bridge, tmp_path: Path
) -> None:
    source = tmp_path / "bait.cpp"
    source.write_text(NULLPTR_BAIT)

    checked = bridge.runner(
        ["clang-tidy", "--checks=-*,modernize-use-nullptr", str(source), "--", "-std=c++20"],
        timeout_s=COMPILE_TIMEOUT_S,
    )

    assert "use nullptr" in checked.output
    # the runner's own back-translation: diagnostics name the host's native path, not /mnt/ws
    assert str(source) in checked.output
