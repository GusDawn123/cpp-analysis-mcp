"""Windows: no TSan or LSan runtime exists here at all, and LLVM installs off PATH.

Both denials are structural facts about the platform, not missing packages: no compiler ships
a ThreadSanitizer or LeakSanitizer runtime for Windows, so there is nothing to install and
the honest suggestion is a different operating system. WSL is that answer without leaving
the machine.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.models import Analysis
from cpp_analysis_mcp.platforms.base import Denial, Platform

NAME = "windows"

# the LLVM installer's default home; its PATH checkbox is off by default, so this is
# where clang-tidy lives on most machines that have it
LLVM_DIR = Path(r"C:\Program Files\LLVM\bin")

WSL_SUGGESTION = "run this check under WSL (a Linux environment inside Windows), where it works"

NO_THREAD_SANITIZER = Denial(
    reason="no compiler ships a ThreadSanitizer runtime for Windows; races need Linux or macOS",
    suggestion=WSL_SUGGESTION,
)

NO_LEAK_SANITIZER = Denial(
    reason="LeakSanitizer has no Windows runtime; leak detection needs Linux",
    suggestion=WSL_SUGGESTION,
)

DENIED = {Analysis.TSAN: NO_THREAD_SANITIZER, Analysis.LSAN: NO_LEAK_SANITIZER}

INSTALL_HINTS = {Analysis.CLANG_TIDY: "winget install LLVM.LLVM"}


def detect() -> Platform:
    """Read this host. The only place that does -- everything else takes a Platform."""
    return Platform(
        name=NAME,
        executable_suffix=".exe",
        extra_tool_dirs=(LLVM_DIR,) if LLVM_DIR.is_dir() else (),
        denied=DENIED,
        install_hints=INSTALL_HINTS,
    )
