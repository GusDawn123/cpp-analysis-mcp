"""Shared vocabulary every layer speaks: findings, capabilities, built binaries, hotspots."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SanitizerKind(StrEnum):
    """A runtime sanitizer a binary can be compiled with."""

    THREAD = "thread"
    ADDRESS = "address"
    UNDEFINED = "undefined"
    LEAK = "leak"


class Analysis(StrEnum):
    """One analysis the server can offer."""

    TSAN = "tsan"
    ASAN = "asan"
    LSAN = "lsan"
    UBSAN = "ubsan"
    THREAD_SAFETY = "thread-safety"
    CLANG_TIDY = "clang-tidy"


# which analyses need a binary built with a sanitizer; the two missing here run at compile time
SANITIZER_FOR: Mapping[Analysis, SanitizerKind] = {
    Analysis.TSAN: SanitizerKind.THREAD,
    Analysis.ASAN: SanitizerKind.ADDRESS,
    Analysis.LSAN: SanitizerKind.LEAK,
    Analysis.UBSAN: SanitizerKind.UNDEFINED,
}


class Severity(StrEnum):
    """How serious a finding is."""

    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


class AccessOp(StrEnum):
    """Whether a memory access read or wrote."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class Location:
    """A point in source code."""

    file: str
    line: int
    column: int | None = None


@dataclass(frozen=True, slots=True)
class Frame:
    """One level of a stack trace."""

    function: str
    location: Location | None = None


@dataclass(frozen=True, slots=True)
class ThreadAccess:
    """What one thread did to the memory a race was reported on."""

    thread_id: str
    op: AccessOp
    size: int | None
    # the diagnosis for a data race: one thread held the lock, the other did not
    locks_held: tuple[str, ...]
    frames: tuple[Frame, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a tool observed, reported as a fact rather than advice."""

    id: str
    tool: str
    severity: Severity
    category: str
    message: str
    location: Location | None = None
    symbol: str | None = None
    threads: tuple[ThreadAccess, ...] = ()
    allocated_at: Location | None = None
    occurrences: int = 1


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """Whether one analysis works on this machine, and why not when it doesn't."""

    available: bool
    reason: str | None = None
    suggestion: str | None = None
    verified_by: str | None = None
    # available but caveated, e.g. TSan runs on macOS yet cannot detect deadlocks there
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuiltBinary:
    """A binary bound to the environment it must run under."""

    path: Path
    build_dir: Path
    sanitizer: SanitizerKind | None
    # apply when running path — a sanitized binary launched without these reports nothing
    runtime_env: Mapping[str, str]
    compile_commands: Path | None
    # -Wthread-safety fires at compile time, so building already produces findings
    warnings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class Hotspot:
    """Where a profiler saw the program spend its time."""

    function: str
    self_pct: float
    total_pct: float
    location: Location | None = None
    note: str | None = None
