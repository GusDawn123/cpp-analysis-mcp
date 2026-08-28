"""What every compiler contributes: flag syntax, pinned runtime options, what it cannot do.

Tables and lookups only. Nothing here runs a compiler -- the path and version string arrive
from whoever discovered them, which keeps a Toolchain constructible in a test.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from cpp_analysis_mcp.store.models import Analysis, SanitizerKind

# must match the flags scripts/fixtures.py compile_case passes; a test cross-checks the two
BASE_FLAGS: tuple[str, ...] = ("-std=c++20", "-g", "-O1", "-fno-omit-frame-pointer")

# what a build meant to be profiled is compiled with. -O2 rather than BASE_FLAGS' -O1 is the
# whole point: optimization decides which calls get inlined, and inlining decides where the
# time appears to go, so a profile of an -O1 build is a ranking of code the release build
# does not contain. -g stays for the source lines and -fno-omit-frame-pointer for the call
# graph -- perf walks frame pointers, and -O2 discards them without it.
PROFILE_FLAGS: tuple[str, ...] = ("-std=c++20", "-g", "-O2", "-fno-omit-frame-pointer")

# what a raced build compiles with: the release shape. No -g and no kept frame pointers,
# because nothing walks these stacks -- a race should time the code a release would ship.
BENCH_FLAGS: tuple[str, ...] = ("-std=c++20", "-O2")

# must match PINNED_ENV in scripts/fixtures.py so a run reproduces the goldens; a test
# cross-checks. ASan and LSan are empty because the goldens were captured on their defaults.
PINNED_RUNTIME_ENV: Mapping[SanitizerKind, Mapping[str, str]] = {
    SanitizerKind.THREAD: {
        "TSAN_OPTIONS": "history_size=7 second_deadlock_stack=1 exitcode=66 detect_deadlocks=1"
    },
    SanitizerKind.ADDRESS: {},
    SanitizerKind.UNDEFINED: {"UBSAN_OPTIONS": "print_stacktrace=1"},
    SanitizerKind.LEAK: {},
}


def sanitize_flag(kind: SanitizerKind) -> str:
    """Return the -fsanitize= flag for a sanitizer; the enum values are the flag words."""
    return f"-fsanitize={kind}"


@dataclass(frozen=True, slots=True)
class Toolchain:
    """One discovered compiler, as data."""

    family: str  # "clang" or "gcc"
    compiler: Path
    version: str  # first line of --version, read by the caller who found the compiler
    warning_flags: tuple[str, ...] = ()
    # structural impossibilities only: analysis -> why this compiler can never do it
    unsupported: Mapping[Analysis, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen= stops rebinding but not editing inside; the proxy copy also unshares
        # the module-level UNSUPPORTED table the constructors pass in
        object.__setattr__(self, "unsupported", MappingProxyType(dict(self.unsupported)))

    def sanitize_flags(self, kind: SanitizerKind) -> tuple[str, ...]:
        """Return the compile flags for building under one sanitizer."""
        return (*BASE_FLAGS, sanitize_flag(kind))

    def unsupported_reason(self, analysis: Analysis) -> str | None:
        """Say why this compiler cannot do an analysis, or None when it can."""
        return self.unsupported.get(analysis)
