"""The contract every platform satisfies: what this OS refuses, what it caveats, how it fails.

Tables and one lookup. Reading the real host happens in each platform's detect(); everything
else receives a Platform as an argument (rule 3), which is what lets the Linux tables be
developed and tested from a macOS laptop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from cpp_analysis_mcp.models import Analysis, SanitizerKind


@dataclass(frozen=True, slots=True)
class Denial:
    """Why an analysis can never work on this OS, and what to do instead."""

    reason: str
    suggestion: str | None = None


@dataclass(frozen=True, slots=True)
class FailureSignature:
    """A cryptic tool crash we have seen, paired with the honest explanation for it."""

    marker: str  # substring to look for in the probe's output
    reason: str
    suggestion: str | None = None


@dataclass(frozen=True, slots=True)
class Platform:
    """One operating system's differences, as data."""

    name: str  # "linux", "darwin" or "windows"
    compile_extras: tuple[str, ...] = ()
    # what a runnable binary's name must end in: ".exe" on Windows, "" elsewhere
    executable_suffix: str = ""
    # extra link inputs a sanitized build needs, per sanitizer. Measured on Windows: the
    # linker resolves the runtime by bare name through the MSVC lib directories first,
    # which hands the link a library built for a different compiler and fails on symbols
    # only MSVC's own objects define; the full path pins LLVM's own runtime instead
    sanitize_link_extras: Mapping[SanitizerKind, tuple[str, ...]] = field(default_factory=dict)
    # DLLs a sanitized binary needs found at load time, per sanitizer. Windows looks
    # beside the executable first, so builds copy these next to what they produce
    runtime_dlls: Mapping[SanitizerKind, tuple[Path, ...]] = field(default_factory=dict)
    # extra cmake configure arguments this OS needs. Measured on Windows: cmake's default
    # there is the Visual Studio generator, which ignores CMAKE_CXX_COMPILER and hands the
    # build to cl.exe, so the configure must name a generator that obeys the choice
    cmake_extras: tuple[str, ...] = ()
    # extra cmake configure arguments one sanitizer needs, on top of cmake_extras. Separate
    # from sanitize_link_extras because these have to reach the configure rather than the
    # link: they change how every object in the project is compiled, and an object is only
    # compiled once. Measured on Windows for UBSan -- see windows.py
    sanitize_cmake_extras: Mapping[SanitizerKind, tuple[str, ...]] = field(default_factory=dict)
    # searched on top of PATH: brew installs llvm outside it on macOS
    extra_tool_dirs: tuple[Path, ...] = ()
    denied: Mapping[Analysis, Denial] = field(default_factory=dict)
    # available, with something the caller has to know -- reported, never a denial
    limitations: Mapping[Analysis, tuple[str, ...]] = field(default_factory=dict)
    failure_signatures: tuple[FailureSignature, ...] = ()
    install_hints: Mapping[Analysis, str] = field(default_factory=dict)
    # cheap volatile host facts, read at detect() time; part of the capability cache fingerprint
    env_facts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen= stops rebinding but not editing inside a mapping; copying into read-only
        # proxies also unshares the module-level tables detect() passes in
        object.__setattr__(self, "denied", MappingProxyType(dict(self.denied)))
        object.__setattr__(self, "limitations", MappingProxyType(dict(self.limitations)))
        object.__setattr__(self, "install_hints", MappingProxyType(dict(self.install_hints)))
        object.__setattr__(self, "env_facts", MappingProxyType(dict(self.env_facts)))
        object.__setattr__(
            self, "sanitize_link_extras", MappingProxyType(dict(self.sanitize_link_extras))
        )
        object.__setattr__(
            self, "sanitize_cmake_extras", MappingProxyType(dict(self.sanitize_cmake_extras))
        )
        object.__setattr__(self, "runtime_dlls", MappingProxyType(dict(self.runtime_dlls)))

    def diagnose(self, output: str) -> FailureSignature | None:
        """Return the first signature whose marker appears in tool output."""
        for signature in self.failure_signatures:
            if signature.marker in output:
                return signature
        return None
