"""Hold the compiler tables to what scripts/fixtures.py actually used to capture the goldens.

The flags and pinned options in toolchains/ are a second copy of what the capture script
passes. These tests cross-check the copies, so a change to one that is not made in the other
fails here rather than showing up as goldens that no longer reproduce.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from helpers import FIXTURES_SCRIPT

from cpp_analysis_mcp.store.models import Analysis, SanitizerKind
from cpp_analysis_mcp.toolchains import clang, gcc
from cpp_analysis_mcp.toolchains.base import (
    BASE_FLAGS,
    PINNED_RUNTIME_ENV,
    Toolchain,
    sanitize_flag,
)


def load_fixtures_script() -> ModuleType:
    """Import scripts/fixtures.py by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("fixtures_script", FIXTURES_SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {FIXTURES_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    # register before executing: @dataclass resolves annotations through sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FIXTURES = load_fixtures_script()
SUPPORT: list[Any] = list(FIXTURES.SUPPORT)

SANITIZER_ANALYSES = (Analysis.TSAN, Analysis.ASAN, Analysis.LSAN, Analysis.UBSAN)


def compile_case_body() -> ast.Module:
    return ast.parse(inspect.getsource(FIXTURES.compile_case))


def compile_case_literals() -> set[str]:
    """Return every string literal in the capture script's compile_case."""
    return {
        node.value
        for node in ast.walk(compile_case_body())
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def compile_case_flag_list() -> list[str]:
    """Return the flags in compile_case's `cmd = [...]`, in order.

    Only the literal ones: the compiler name is a variable and -fsanitize= is an f-string,
    which is what leaves exactly the flags every compile gets regardless of case or OS.
    """
    for node in ast.walk(compile_case_body()):
        named_cmd = (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "cmd"
        )
        if named_cmd and isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            return [
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
    raise AssertionError(f"no `cmd = [...]` found in {FIXTURES_SCRIPT} compile_case")


def a_clang() -> Toolchain:
    return clang.toolchain(Path("/usr/bin/clang++"), "Apple clang version 17.0.0")


def a_gcc() -> Toolchain:
    return gcc.toolchain(Path("/usr/bin/g++"), "g++ (Debian 13.2.0-25) 13.2.0")


# ------------------------------------------------------------------ cross-checks vs the script


def test_base_flags_are_the_flags_the_capture_script_used() -> None:
    """Exact, both ways: the goldens were captured under these flags and no others."""
    assert compile_case_flag_list() == list(BASE_FLAGS)


def test_base_flags_carry_no_platform_flag() -> None:
    """-pthread is Linux's, so it belongs to the platform table and not to every compile."""
    assert "-pthread" not in BASE_FLAGS
    assert "-pthread" not in compile_case_flag_list()
    # the script still passes it, just per-OS
    assert "-pthread" in compile_case_literals()


def test_sanitize_flag_matches_every_case_in_the_support_matrix() -> None:
    for case in SUPPORT:
        kind = SanitizerKind(case.sanitizer)

        assert sanitize_flag(kind) == f"-fsanitize={case.sanitizer}"


def test_pinned_runtime_env_matches_the_capture_script() -> None:
    """These exact options were set when the goldens were captured; a run must reproduce them."""
    pinned = FIXTURES.PINNED_ENV

    assert PINNED_RUNTIME_ENV[SanitizerKind.THREAD]["TSAN_OPTIONS"] == pinned["TSAN_OPTIONS"]
    assert PINNED_RUNTIME_ENV[SanitizerKind.UNDEFINED]["UBSAN_OPTIONS"] == pinned["UBSAN_OPTIONS"]


def test_address_and_leak_pin_nothing() -> None:
    """ASan and LSan ran on their defaults, so pinning anything would drift from the goldens."""
    assert PINNED_RUNTIME_ENV[SanitizerKind.ADDRESS] == {}
    assert PINNED_RUNTIME_ENV[SanitizerKind.LEAK] == {}
    assert "ASAN_OPTIONS" not in FIXTURES.PINNED_ENV
    assert "LSAN_OPTIONS" not in FIXTURES.PINNED_ENV


def test_every_sanitizer_has_an_entry() -> None:
    assert set(PINNED_RUNTIME_ENV) == set(SanitizerKind)


# ------------------------------------------------------------------------------------ clang


def test_clang_offers_the_compile_time_lock_analysis() -> None:
    toolchain = a_clang()

    assert "-Wthread-safety" in toolchain.warning_flags
    assert toolchain.unsupported_reason(Analysis.THREAD_SAFETY) is None


def test_clang_supports_every_analysis() -> None:
    toolchain = a_clang()

    assert [analysis for analysis in Analysis if toolchain.unsupported_reason(analysis)] == []


def test_clang_records_what_the_caller_discovered() -> None:
    toolchain = a_clang()

    assert toolchain.family == "clang"
    assert toolchain.compiler == Path("/usr/bin/clang++")
    assert toolchain.version == "Apple clang version 17.0.0"


# -------------------------------------------------------------------------------------- gcc


def test_gcc_has_no_lock_analysis_and_says_what_to_do() -> None:
    reason = a_gcc().unsupported_reason(Analysis.THREAD_SAFETY)

    assert reason is not None
    assert "-Wthread-safety" in reason
    assert "clang" in reason


def test_gcc_offers_no_warning_flags() -> None:
    assert a_gcc().warning_flags == ()


def test_gcc_still_runs_every_sanitizer() -> None:
    """gcc vendors LLVM's runtime, so the sanitizer analyses cost nothing to support."""
    toolchain = a_gcc()

    for analysis in SANITIZER_ANALYSES:
        assert toolchain.unsupported_reason(analysis) is None


def test_gcc_still_runs_clang_tidy() -> None:
    """clang-tidy reads compile_commands.json; it does not care who compiled."""
    assert a_gcc().unsupported_reason(Analysis.CLANG_TIDY) is None


# ------------------------------------------------------------------------------------ flags


def test_sanitize_flags_are_the_base_flags_plus_the_sanitizer() -> None:
    flags = a_clang().sanitize_flags(SanitizerKind.THREAD)

    assert flags[: len(BASE_FLAGS)] == BASE_FLAGS
    assert flags[-1] == "-fsanitize=thread"
    assert len(flags) == len(BASE_FLAGS) + 1


def test_both_families_build_the_same_sanitizer_flags() -> None:
    """The flag syntax is shared; only -Wthread-safety separates the two."""
    for kind in SanitizerKind:
        assert a_clang().sanitize_flags(kind) == a_gcc().sanitize_flags(kind)


def test_the_unsupported_table_cannot_be_edited() -> None:
    """Both constructors pass shared module-level tables; a writable mapping on one
    Toolchain would silently corrupt every one constructed after it."""
    chain = gcc.toolchain(Path("/usr/bin/g++"), version="g++ (Ubuntu) 13.2.0")

    try:
        chain.unsupported[Analysis.TSAN] = "no"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("Toolchain.unsupported accepted a write")
    # and the source table survived the attempt
    assert Analysis.TSAN not in gcc.UNSUPPORTED
