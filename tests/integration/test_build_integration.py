"""Build the real fixtures with the real compilers on this machine, then run them.

The unit suite proves what command gets composed; this proves the loop that command exists
for -- compile a planted bug under AddressSanitizer, run the binary under exactly the
environment the build handed back, and require the sanitizer to report. A build whose
runtime_env was dropped or wrong would run clean here, which is the one failure this layer
is built to make impossible.

The second half covers the compile-time half of the same idea: -Wthread-safety fires while
building, so a successful clang build already carries findings, and a gcc build of the same
source carries none because gcc has no such analysis.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import cpp_source

from cpp_analysis_mcp import platforms, process
from cpp_analysis_mcp.build.single_file import compile_file
from cpp_analysis_mcp.capabilities import discover_toolchains
from cpp_analysis_mcp.models import BuiltBinary, SanitizerKind
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration

RUN_TIMEOUT_S = 30

ASAN_MARKER = "AddressSanitizer"
THREAD_SAFETY_CATEGORY = "thread-safety-analysis"

HEAP_OVERFLOW = "heap_overflow"
UNGUARDED_WRITE = "unguarded_write"


@pytest.fixture(scope="module")
def host() -> Platform:
    return platforms.detect()


@pytest.fixture(scope="module")
def toolchains() -> tuple[Toolchain, ...]:
    found = discover_toolchains()
    assert found, "no C++ compiler found; this suite needs a real toolchain"
    return found


def family(toolchains: tuple[Toolchain, ...], name: str) -> list[Toolchain]:
    return [chain for chain in toolchains if chain.family == name]


def can_sanitize(host: Platform, chain: Toolchain) -> bool:
    """MinGW gcc ships no sanitizer runtimes on Windows; every sanitized link fails there."""
    return not (host.name == "windows" and chain.family == "gcc")


def build(
    stem: str,
    *,
    toolchain: Toolchain,
    host: Platform,
    sanitizer: SanitizerKind | None,
    tmp_path: Path,
) -> BuiltBinary:
    """Compile one fixture, failing with what the compiler said when it did not build."""
    result = compile_file(
        cpp_source(stem),
        toolchain=toolchain,
        platform=host,
        sanitizer=sanitizer,
        # one directory per compiler: both write a binary named after the same source
        build_dir=tmp_path / toolchain.family,
    )
    assert isinstance(result, BuiltBinary), f"{toolchain.family} failed to build {stem}: {result}"
    return result


def test_an_asan_build_runs_and_reports_its_planted_bug(
    toolchains: tuple[Toolchain, ...], host: Platform, tmp_path: Path
) -> None:
    """The whole point of the layer: build, run under the environment it returned, detect."""
    for chain in toolchains:
        if not can_sanitize(host, chain):
            continue
        binary = build(
            HEAP_OVERFLOW,
            toolchain=chain,
            host=host,
            sanitizer=SanitizerKind.ADDRESS,
            tmp_path=tmp_path,
        )

        assert binary.path.is_file(), f"{chain.family}: no binary at {binary.path}"
        assert binary.sanitizer is SanitizerKind.ADDRESS

        result = process.run(
            [str(binary.path)],
            timeout_s=RUN_TIMEOUT_S,
            env=process.hygienic_env(dict(binary.runtime_env)),
        )

        assert ASAN_MARKER in result.output, f"{chain.family}: ASan said nothing: {result.output}"


def test_clang_reports_the_lock_bug_while_compiling(
    toolchains: tuple[Toolchain, ...], host: Platform, tmp_path: Path
) -> None:
    """A build that exited 0 and still found something: the warnings are the finding."""
    found = family(toolchains, "clang")
    if not found:
        pytest.skip("no clang on this machine")

    for chain in found:
        binary = build(
            UNGUARDED_WRITE, toolchain=chain, host=host, sanitizer=None, tmp_path=tmp_path
        )

        flagged = [
            warning for warning in binary.warnings if warning.category == THREAD_SAFETY_CATEGORY
        ]
        assert flagged, f"{chain.family} found no lock bug: {binary.warnings}"
        assert flagged[0].location is not None
        assert flagged[0].location.file.endswith(f"{UNGUARDED_WRITE}.cpp")


def test_gcc_finds_no_lock_bug_because_it_has_no_such_analysis(
    toolchains: tuple[Toolchain, ...], host: Platform, tmp_path: Path
) -> None:
    """Not a weaker version: gcc has no equivalent, so its silence means nothing was looked for."""
    found = family(toolchains, "gcc")
    if not found:
        pytest.skip("no gcc on this machine")

    for chain in found:
        binary = build(
            UNGUARDED_WRITE, toolchain=chain, host=host, sanitizer=None, tmp_path=tmp_path
        )

        assert [
            warning for warning in binary.warnings if "thread-safety" in warning.category
        ] == [], f"{chain.family} reported a lock analysis it does not have"
