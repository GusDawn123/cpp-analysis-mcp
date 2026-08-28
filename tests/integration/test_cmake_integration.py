"""Build the real fixture project with the real cmake on this machine, then run it.

The unit suite fakes the File API reply; this proves the codemodel, binary path, and
linked sanitizer flags are all real (a dropped flag yields a clean, silent binary). The
fixture ships one library plus one executable so target selection has something to filter.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from helpers import FIXTURES_DIR

from cpp_analysis_mcp import platforms, process
from cpp_analysis_mcp.build.cmake import build_project
from cpp_analysis_mcp.capabilities import discover_toolchains
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.store.models import BuiltBinary, SanitizerKind
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration

# every test here shells out to cmake, so without it there is nothing to skip around
if shutil.which("cmake") is None:
    pytest.skip("no cmake on this machine", allow_module_level=True)

PROJECT_DIR = FIXTURES_DIR / "cmake_project"

RUN_TIMEOUT_S = 30

ASAN_MARKER = "AddressSanitizer"
OVERFLOW_MARKER = "heap-buffer-overflow"

# the fixture's one executable, and the library that must not be mistaken for it
EXECUTABLE_TARGET = "overflow_app"
LIBRARY_TARGET = "helper"


@pytest.fixture(scope="module")
def host() -> Platform:
    return platforms.detect()


@pytest.fixture(scope="module")
def toolchains() -> tuple[Toolchain, ...]:
    found = discover_toolchains()
    assert found, "no C++ compiler found; this suite needs a real toolchain"
    return found


def can_sanitize(host: Platform, chain: Toolchain) -> bool:
    """MinGW gcc ships no sanitizer runtimes on Windows; every sanitized link fails there."""
    return not (host.name == "windows" and chain.family == "gcc")


def build(toolchain: Toolchain, host: Platform, tmp_path: Path) -> BuiltBinary:
    """Build the fixture under ASan without naming a target."""
    result = build_project(
        PROJECT_DIR,
        toolchain=toolchain,
        platform=host,
        sanitizer=SanitizerKind.ADDRESS,
        # one directory per compiler: a shared build tree would be reconfigured underneath
        build_dir=tmp_path / toolchain.family,
    )
    assert isinstance(result, BuiltBinary), f"{toolchain.family} failed to build: {result}"
    return result


def test_the_project_builds_and_reports_its_planted_bug(
    toolchains: tuple[Toolchain, ...], host: Platform, tmp_path: Path
) -> None:
    for chain in toolchains:
        if not can_sanitize(host, chain):
            continue
        binary = build(chain, host, tmp_path)

        assert binary.path.is_file(), f"{chain.family}: the File API named {binary.path}"
        assert binary.sanitizer is SanitizerKind.ADDRESS
        assert binary.compile_commands is not None, f"{chain.family}: no compilation database"
        assert binary.compile_commands.is_file()

        result = process.run(
            [str(binary.path)],
            timeout_s=RUN_TIMEOUT_S,
            env=process.hygienic_env(dict(binary.runtime_env)),
        )

        assert ASAN_MARKER in result.output, f"{chain.family}: ASan said nothing: {result.output}"
        assert OVERFLOW_MARKER in result.output, f"{chain.family}: wrong bug: {result.output}"


def test_the_executable_is_found_without_being_named(
    toolchains: tuple[Toolchain, ...], host: Platform, tmp_path: Path
) -> None:
    for chain in toolchains:
        if not can_sanitize(host, chain):
            continue
        binary = build(chain, host, tmp_path)

        # cmake appends the platform's own executable suffix, .exe on Windows
        assert binary.path.name == EXECUTABLE_TARGET + host.executable_suffix
        assert LIBRARY_TARGET not in binary.path.name
