"""macOS: brew keeps llvm off PATH, LeakSanitizer has no arm64 build, TSan sees no deadlocks.

Both entries below were measured against the fixtures: the leak case and the deadlock case
are the two scripts/fixtures.py skips here, and this is where those skips become something
the server can report.
"""

from __future__ import annotations

import platform
from pathlib import Path

from cpp_analysis_mcp.models import Analysis
from cpp_analysis_mcp.platforms.base import Denial, Platform

NAME = "darwin"

ARM64 = "arm64"

# brew does not link llvm into a directory on PATH, so clang-tidy is invisible without this
BREW_LLVM_DIRS = (Path("/opt/homebrew/opt/llvm/bin"), Path("/usr/local/opt/llvm/bin"))

NO_LEAK_SANITIZER = Denial(
    reason="LeakSanitizer is Linux-only; it does not run on macOS arm64",
    suggestion="run the leak check on Linux, or on the roadmap's Linux-container mode",
)

# a silent TSan run here means "no races found", never "no deadlocks found"
TSAN_LIMITATIONS = (
    "Apple clang's TSan runtime cannot detect deadlocks or lock-order inversions; "
    "detect_deadlocks=1 is inert, so deadlock findings require Linux",
)

INSTALL_HINTS = {Analysis.CLANG_TIDY: "brew install llvm"}


def detect() -> Platform:
    """Read this host. The only place that does -- everything else takes a Platform."""
    return Platform(
        name=NAME,
        extra_tool_dirs=tuple(path for path in BREW_LLVM_DIRS if path.is_dir()),
        denied=denied(),
        limitations={Analysis.TSAN: TSAN_LIMITATIONS},
        install_hints=INSTALL_HINTS,
    )


def denied() -> dict[Analysis, Denial]:
    """Intel macs do run LeakSanitizer, so the denial is arm64's alone."""
    if platform.machine() != ARM64:
        return {}
    return {Analysis.LSAN: NO_LEAK_SANITIZER}
