"""clang: every analysis in the set works, and the sanitizers here are the reference build."""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.toolchains.base import Toolchain

# the compile-time lock analysis, which no other compiler implements
WARNING_FLAGS = ("-Wthread-safety",)


def toolchain(compiler: Path, version: str) -> Toolchain:
    """Describe a clang someone already found; nothing here runs it."""
    return Toolchain(
        family="clang",
        compiler=compiler,
        version=version,
        warning_flags=WARNING_FLAGS,
    )
