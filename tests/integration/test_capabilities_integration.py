"""Run the real probes against the real compilers on this machine.

The unit suite proves the classification; this proves the probes themselves -- that the
snippets still compile, that each sanitizer still reports the bug planted for it, and that
what capabilities.py concludes matches what scripts/fixtures.py says this OS supports. It
compiles and runs a handful of programs per compiler, so it takes tens of seconds and is
marked integration; `make test` excludes it and `make integration` runs it alone.
"""

from __future__ import annotations

import importlib.util
import platform as host_platform
import sys
from types import ModuleType
from typing import Any

import pytest
from helpers import FIXTURES_SCRIPT

from cpp_analysis_mcp import platforms
from cpp_analysis_mcp.capabilities import discover_toolchains, probe_all
from cpp_analysis_mcp.store.models import Analysis, CapabilityStatus
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration

Probed = list[tuple[Toolchain, dict[Analysis, CapabilityStatus]]]

# the capture script names its cases by tool; these are the same tools as analyses
ANALYSIS_FOR_TOOL = {
    "tsan": Analysis.TSAN,
    "asan": Analysis.ASAN,
    "lsan": Analysis.LSAN,
    "ubsan": Analysis.UBSAN,
}


def load_fixtures_script() -> ModuleType:
    """Import scripts/fixtures.py by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("fixtures_script", FIXTURES_SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {FIXTURES_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    # register before executing: @dataclass resolves annotations through sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SUPPORT: list[Any] = list(load_fixtures_script().SUPPORT)


def expected_here() -> dict[Analysis, bool]:
    """Read off the capture script which sanitizers this OS is supposed to support.

    A tool counts as supported when any one of its cases runs here: TSan's deadlock case is
    Linux-only, but its data race case runs everywhere, so TSan is supported on both.
    """
    here = host_platform.system().lower()
    expected: dict[Analysis, bool] = {}
    for case in SUPPORT:
        analysis = ANALYSIS_FOR_TOOL[case.tool]
        expected[analysis] = expected.get(analysis, False) or here in case.platforms
    return expected


@pytest.fixture(scope="module")
def probed() -> Probed:
    """Probe every compiler on this machine once, with the cache off so the probes really run."""
    host = platforms.detect()
    found = discover_toolchains()
    assert found, "no C++ compiler found; this suite needs a real toolchain"
    return [(chain, probe_all(chain, host, cache_dir=None)) for chain in found]


def test_discovery_finds_a_working_compiler(probed: Probed) -> None:
    for chain, _ in probed:
        assert chain.family in {"clang", "gcc"}
        assert chain.compiler.is_file()
        assert chain.version


def expected_for(chain: Toolchain) -> dict[Analysis, bool]:
    """This OS's expectations, less what this compiler cannot do here: MinGW gcc ships
    no sanitizer runtimes on Windows, so every sanitizer reads unavailable for it."""
    expected = expected_here()
    if host_platform.system().lower() == "windows" and chain.family == "gcc":
        return dict.fromkeys(expected, False)
    return expected


def test_the_sanitizers_agree_with_the_capture_script(probed: Probed) -> None:
    for chain, statuses in probed:
        for analysis, expected in expected_for(chain).items():
            status = statuses[analysis]
            where = f"{chain.family} {analysis}"

            assert status.available is expected, f"{where}: {status.reason}"
            if expected:
                assert status.verified_by, f"{where} is available but proved by nothing"
            else:
                assert status.reason, f"{where} is unavailable and says nothing"


def test_the_compile_time_lock_check_follows_the_compiler(probed: Probed) -> None:
    """clang has -Wthread-safety and gcc has no equivalent, so this one splits by family."""
    for chain, statuses in probed:
        status = statuses[Analysis.THREAD_SAFETY]
        where = f"{chain.family} {Analysis.THREAD_SAFETY}"

        assert status.available is (chain.family == "clang"), f"{where}: {status.reason}"
        if not status.available:
            assert status.reason, f"{where} is unavailable and says nothing"


def test_clang_tidy_is_either_working_or_explained(probed: Probed) -> None:
    for chain, statuses in probed:
        status = statuses[Analysis.CLANG_TIDY]
        where = f"{chain.family} {Analysis.CLANG_TIDY}"

        if status.available:
            assert status.verified_by, f"{where} is available but proved by nothing"
        else:
            assert status.reason, f"{where} is unavailable and says nothing"
            assert status.suggestion, f"{where} is unavailable with no way out"


def test_no_status_anywhere_is_silent(probed: Probed) -> None:
    for chain, statuses in probed:
        for analysis, status in statuses.items():
            if not status.available:
                assert status.reason, f"{chain.family} {analysis} went silent"


@pytest.mark.skipif(
    host_platform.system().lower() != "darwin", reason="the deadlock blind spot is macOS's"
)
def test_macos_reports_the_deadlock_blind_spot(probed: Probed) -> None:
    """TSan runs here, but its deadlock detector is inert -- silence must not read as clean."""
    for chain, statuses in probed:
        tsan = statuses[Analysis.TSAN]

        assert tsan.available, f"{chain.family}: {tsan.reason}"
        assert any("deadlock" in note for note in tsan.limitations)
