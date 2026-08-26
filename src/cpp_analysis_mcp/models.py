"""Shared vocabulary every layer speaks: findings, capabilities, built binaries, hotspots."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType


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
    PROFILE = "profile"


# which analyses need a binary built with a sanitizer; the three missing here are the two
# compile-time checks and the profiler, which builds optimized and instruments nothing
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

    def __post_init__(self) -> None:
        # frozen= stops rebinding but not editing inside the mapping; the proxy copy also
        # unshares the pinned table a builder hands in
        object.__setattr__(self, "runtime_env", MappingProxyType(dict(self.runtime_env)))


@dataclass(frozen=True, slots=True)
class BuildFailure:
    """A build that produced no binary, reported as facts.

    User code that does not compile is an expected outcome of asking to build it, not an
    internal error, so it comes back as a return value rather than an exception: the caller
    reads which step died and what the tool said instead of catching something.
    """

    stage: str  # "configure", "compile" or "build" -- which step died
    # the tool's full output, stderr merged in, kept whole so nothing a reader needs is cut
    output: str
    # what the platform's failure signature said, when one matched; None when the output
    # explains itself and inventing a reason would be worse than quoting the tool
    reason: str | None = None
    suggestion: str | None = None
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """What one analysis observed, and what makes its silence worth trusting.

    An empty `findings` means a detector that was proved to work here saw nothing. The proof
    is the capability probe, so `verified_by` and `limitations` travel with the result rather
    than being looked up again later: read without them, an empty report cannot be told from
    one produced by a detector that was never watching.
    """

    analysis: Analysis
    findings: tuple[Finding, ...]
    # compile-time findings from the build step, distinct from what the run observed
    build_warnings: tuple[Finding, ...] = ()
    # the program's own exit status: a crash with no sanitizer report is still a fact
    exit_code: int | None = None
    timed_out: bool = False
    limitations: tuple[str, ...] = ()
    verified_by: str | None = None


@dataclass(frozen=True, slots=True)
class Hotspot:
    """Where a profiler saw the program spend its time."""

    function: str
    self_pct: float
    total_pct: float
    location: Location | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One recognized pattern in a profile: the fact in plain words, plus the known
    rewrite families for it. Never a diff and never a verdict."""

    category: str
    share_pct: float
    statement: str
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileReport:
    """Where the time went, and what makes the ranking worth believing.

    Separate from AnalysisReport because a profiler answers a different question. A sanitizer
    reports discrete facts that are true or absent; a profiler reports a distribution, and a
    distribution read without knowing how it was sampled is a ranking of noise. So the two
    numbers that decide whether these percentages mean anything travel with them:

    `samples` is how many the profiler actually took. A hundred samples spread over a hundred
    functions ranks nothing, and the difference between 40% and 30% at that count is chance.

    `event` is what was counted. `cpu/cycles/P` is the hardware counter and is what a
    profile normally means; a virtualized host with no PMU falls back to `cpu-clock`, a
    timer interrupt, which still finds hot code but cannot see stalls -- and neither can be
    told from the other by looking at the percentages.
    """

    analysis: Analysis
    # ordered by self time, hottest first -- the profiler's own order is by cumulative time,
    # which puts main() and _start at the top of every profile ever taken
    hotspots: tuple[Hotspot, ...]
    samples: int
    event: str
    # the ranking read back in plain words: which library machinery ate the time
    fingerprints: tuple[Fingerprint, ...] = ()
    # what the sample count says about trusting the percentages
    confidence: str | None = None
    next_step: str | None = None
    # the program's own exit status: a workload that crashed profiled only what it reached
    exit_code: int | None = None
    timed_out: bool = False
    limitations: tuple[str, ...] = ()
    verified_by: str | None = None


@dataclass(frozen=True, slots=True)
class Variant:
    """One competitor in a benchmark race: a name and a complete program."""

    name: str
    code: str


@dataclass(frozen=True, slots=True)
class VariantResult:
    """How one variant fared in a race: its times, and whether its answer held up.

    A rejected variant keeps its name and the reason it is out, but its numbers stay None.
    Reporting a time for a program that crashed or answered differently would invite the
    exact comparison the rejection exists to prevent.
    """

    name: str
    runs: int
    mean_ms: float | None = None
    min_ms: float | None = None
    stddev_ms: float | None = None
    matches_baseline: bool = False
    rejected: str | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Who won the race, and what makes the numbers worth believing.

    Variants are ranked fastest first among those that survived; rejected ones sit at the
    end with their reasons. The baseline is named because every claim in the report is
    relative to it: a variant only counts as faster if it produced the same output from
    the same input. `repeats` is how many timed runs each mean rests on, and a mean with
    a large stddev next to it is a shrug, not a ranking.
    """

    baseline: str
    variants: tuple[VariantResult, ...]
    repeats: int
    limitations: tuple[str, ...] = ()
    next_step: str | None = None
