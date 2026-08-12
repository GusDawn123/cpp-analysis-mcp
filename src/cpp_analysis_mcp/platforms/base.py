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

from cpp_analysis_mcp.models import Analysis


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

    def diagnose(self, output: str) -> FailureSignature | None:
        """Return the first signature whose marker appears in tool output."""
        for signature in self.failure_signatures:
            if signature.marker in output:
                return signature
        return None
