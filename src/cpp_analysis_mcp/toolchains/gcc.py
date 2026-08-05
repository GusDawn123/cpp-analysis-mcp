"""gcc: the sanitizers work, since gcc vendors LLVM's runtime rather than writing its own.

The one real loss is the compile-time lock analysis, reported as unavailable with the reason
rather than as a run that found nothing -- silence would read as "your locking is fine".
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.models import Analysis
from cpp_analysis_mcp.toolchains.base import Toolchain

NO_THREAD_SAFETY = (
    "gcc has no equivalent of clang's -Wthread-safety, not a weaker version. "
    "Installing clang enables this check while the build stays on gcc."
)

UNSUPPORTED = {Analysis.THREAD_SAFETY: NO_THREAD_SAFETY}


def toolchain(compiler: Path, version: str) -> Toolchain:
    """Describe a gcc someone already found; nothing here runs it."""
    return Toolchain(
        family="gcc",
        compiler=compiler,
        version=version,
        unsupported=UNSUPPORTED,
    )
