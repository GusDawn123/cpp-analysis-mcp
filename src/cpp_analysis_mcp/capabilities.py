"""Probes what this machine can actually do by compiling and running five-line smoke tests,
one per sanitizer and analysis, rather than sniffing version numbers — a compiler can report
a version that supports ThreadSanitizer while the runtime library is missing or a system
setting blocks it. Results become CapabilityStatus values carrying the reason and a concrete
suggestion when something is unavailable, and cache to disk fingerprinted on compiler path,
compiler version, and OS release so only the first run pays the cost.

Every probe compiles a program with a bug planted in it and requires the detector to report
that bug. A build that succeeded and then said nothing is unavailable, not available: a
runtime that stays quiet on a known bug would call clean code and buggy code clean alike.

The Platform and Toolchain always arrive as arguments (rule 3). discover_toolchains is the
one exception, because finding the compilers on a host is host probing, which is this
module's job.
"""

from __future__ import annotations

import hashlib
import json
import platform as host_platform
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from cpp_analysis_mcp import process
from cpp_analysis_mcp.models import SANITIZER_FOR, Analysis, CapabilityStatus
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.toolchains import clang, gcc
from cpp_analysis_mcp.toolchains.base import PINNED_RUNTIME_ENV, Toolchain

# bump when a probe starts meaning something different; it is part of the cache fingerprint,
# so a bump retires every cached result rather than answering new questions with old findings
PROBE_SCHEMA_VERSION: int = 1

COMPILE_TIMEOUT_S = 120
RUN_TIMEOUT_S = 30
VERSION_TIMEOUT_S = 10

TEMP_PREFIX = "cpp-analysis-probe-"
# scratch files are named after the analysis they belong to, so a probe is identifiable
# in a process listing, in a compiler error, and in a test's record of what was spawned
PROBE_STEM = "probe_"

CLANG_TIDY = "clang-tidy"
TIDY_CHECK = "modernize-use-nullptr"

COMPILER_CANDIDATES = ("clang++", "g++")
CONSTRUCTORS = {"clang": clang.toolchain, "gcc": gcc.toolchain}


# ------------------------------------------------------------------------- the buggy programs

# Two threads increment one plain int with nothing between them. Modelled on
# tests/fixtures/cpp/data_race.cpp.
TSAN_SOURCE = """\
#include <thread>

namespace {

int counter = 0;

void bump() {
    for (int i = 0; i < 100000; ++i) {
        ++counter;  // planted: unsynchronized read-modify-write from two threads
    }
}

}  // namespace

int main() {
    std::thread first(bump);
    std::thread second(bump);
    first.join();
    second.join();
    return 0;
}
"""

# Reads one element past a 4-element heap array. Modelled on
# tests/fixtures/cpp/heap_overflow.cpp.
ASAN_SOURCE = """\
int main() {
    // Index and sink are both volatile: at -O1 the read is dead and gets dropped otherwise.
    volatile int index = 4;
    int* values = new int[4]();
    volatile int sink = values[index];  // planted: index 4 is past the end
    (void)sink;
    delete[] values;
    return 0;
}
"""

# Allocates, then drops the only pointer to it. Modelled on tests/fixtures/cpp/leak.cpp,
# which is where this one has to be verified: macOS arm64 denies LSan outright.
LSAN_SOURCE = """\
int main() {
    int* volatile values = new int[4]();  // planted: allocated and never deleted
    values[0] = 1;
    // Drop the last pointer through a volatile store so LSan sees the block as unreachable.
    values = nullptr;
    return 0;
}
"""

# Adds one to INT_MAX. Modelled on tests/fixtures/cpp/signed_overflow.cpp.
UBSAN_SOURCE = """\
#include <climits>

int main() {
    volatile int x = INT_MAX;
    x = x + 1;  // planted: signed overflow past INT_MAX
    return 0;
}
"""

# clang's capability attributes, spelled out rather than behind the usual macros so the
# probe needs no header. The write below is what -Wthread-safety must object to.
THREAD_SAFETY_SOURCE = """\
struct __attribute__((capability("mutex"))) ProbeMutex {
    void lock() __attribute__((acquire_capability())) {}
    void unlock() __attribute__((release_capability())) {}
};

namespace {

ProbeMutex m;
int guarded __attribute__((guarded_by(m))) = 0;

}  // namespace

int main() {
    guarded = 1;  // planted: writes a guarded variable without holding m
    return 0;
}
"""

CLANG_TIDY_SOURCE = """\
int main() {
    int* p = 0;  // planted: a null pointer spelled 0 rather than nullptr
    return p == nullptr ? 0 : 1;
}
"""


@dataclass(frozen=True, slots=True)
class Probe:
    """A deliberately buggy program, and what counts as proof its detector still works."""

    source: str
    marker: str  # what the detector prints when it catches the planted bug
    detector: str
    planted: str  # the bug the source carries
    action: str  # what the probe did with it, so a report can say so


PROBES: Mapping[Analysis, Probe] = {
    Analysis.TSAN: Probe(
        source=TSAN_SOURCE,
        marker="WARNING: ThreadSanitizer",
        detector="ThreadSanitizer",
        planted="data race",
        action="compiled and ran",
    ),
    Analysis.ASAN: Probe(
        source=ASAN_SOURCE,
        marker="ERROR: AddressSanitizer",
        detector="AddressSanitizer",
        planted="heap buffer overflow",
        action="compiled and ran",
    ),
    Analysis.LSAN: Probe(
        source=LSAN_SOURCE,
        marker="detected memory leaks",
        detector="LeakSanitizer",
        planted="memory leak",
        action="compiled and ran",
    ),
    Analysis.UBSAN: Probe(
        source=UBSAN_SOURCE,
        marker="runtime error:",
        detector="UndefinedBehaviorSanitizer",
        planted="signed integer overflow",
        action="compiled and ran",
    ),
    Analysis.THREAD_SAFETY: Probe(
        source=THREAD_SAFETY_SOURCE,
        # the diagnostic quotes the flag that produced it, in brackets
        marker="-Wthread-safety",
        detector="-Wthread-safety",
        planted="write to a guarded_by variable with no lock held",
        action="compiled",
    ),
    Analysis.CLANG_TIDY: Probe(
        source=CLANG_TIDY_SOURCE,
        marker=TIDY_CHECK,
        detector=CLANG_TIDY,
        planted="null pointer written as 0",
        action="checked",
    ),
}


@dataclass(frozen=True, slots=True)
class Stage:
    """One step of a probe: what it was for, how long it had, and what it produced."""

    name: str
    timeout_s: int
    # a step that had to exit 0 to mean anything: a compiler that rejected -Wthread-safety
    # quotes the flag in the error, and reading that as a detection would invent a capability
    must_succeed: bool
    result: process.RunResult


# ----------------------------------------------------------------------------- public surface


def discover_toolchains(*, runner: Runner = process.run) -> tuple[Toolchain, ...]:
    """Find the C++ compilers on PATH and read their versions.

    The one place in this package that goes looking for a toolchain rather than being handed
    one, because finding compilers is host probing and host probing lives here.
    """
    found: dict[tuple[str, str], Toolchain] = {}
    for name in COMPILER_CANDIDATES:
        path = shutil.which(name)
        if path is None:
            continue
        answer = runner([path, "--version"], timeout_s=VERSION_TIMEOUT_S)
        # covers a timeout too, which leaves no exit code at all
        if answer.exit_code != 0:
            continue
        version = first_line(answer.output)
        if not version:
            continue
        # macOS ships /usr/bin/g++ as a clang driver, so the version text decides, not the name
        family = "clang" if "clang" in version.lower() else "gcc"
        found.setdefault((family, version), CONSTRUCTORS[family](Path(path), version))
    return tuple(found.values())


def fingerprint(toolchain: Toolchain, platform: Platform) -> str:
    """Hash everything that decides a probe's outcome, and nothing else."""
    payload = {
        "schema": PROBE_SCHEMA_VERSION,
        "family": toolchain.family,
        "compiler": str(toolchain.compiler),
        "version": toolchain.version,
        "platform": platform.name,
        "env_facts": dict(platform.env_facts),
        "os_release": host_platform.release(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def probe_all(
    toolchain: Toolchain,
    platform: Platform,
    *,
    cache_dir: Path | None = None,
    runner: Runner = process.run,
) -> dict[Analysis, CapabilityStatus]:
    """Return one status per analysis, from the cache when this machine has not changed."""
    if cache_dir is None:
        return _probe_every(toolchain, platform, runner)

    cache_file = cache_dir / f"{fingerprint(toolchain, platform)}.json"
    cached = read_cache(cache_file)
    if cached is not None:
        return cached

    statuses = _probe_every(toolchain, platform, runner)
    write_cache(cache_file, statuses)
    return statuses


def classify(
    analysis: Analysis,
    platform: Platform,
    probe: Probe,
    stages: Sequence[Stage],
) -> CapabilityStatus:
    """Turn what the probe's steps produced into a status. Pure: reads no host, spawns nothing."""
    for stage in stages:
        if stage.result.timed_out:
            return CapabilityStatus(
                available=False,
                reason=f"the {stage.name} step timed out after {stage.timeout_s}s",
            )
        if stage.must_succeed and stage.result.exit_code != 0:
            return _failed(platform, stage)

    detection = stages[-1]
    if probe.marker in detection.result.output:
        return CapabilityStatus(
            available=True,
            verified_by=f"{probe.action} a planted {probe.planted}; {probe.detector} reported it",
            limitations=tuple(platform.limitations.get(analysis, ())),
        )
    # the false all-clear: it built, it ran, it exited clean, and it saw nothing
    if detection.result.exit_code == 0:
        return CapabilityStatus(
            available=False,
            reason=(
                f"{probe.action} a planted {probe.planted} and {probe.detector} reported "
                "nothing. A detector that stays silent on a known bug reports buggy code "
                "exactly as it reports clean code, so nothing it says can be trusted."
            ),
        )
    return _failed(platform, detection)


def excerpt(text: str, lines: int = 4) -> str:
    """Flatten the first few lines of tool output onto one line."""
    kept = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " | ".join(kept[:lines]) or "(no output)"


def first_line(text: str) -> str:
    """Return the first non-empty line of output, stripped."""
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""


# ------------------------------------------------------------------------------------ probing


def _probe_every(
    toolchain: Toolchain, platform: Platform, runner: Runner
) -> dict[Analysis, CapabilityStatus]:
    """Answer the structural nos for free, then probe whatever is left, all at once."""
    statuses: dict[Analysis, CapabilityStatus] = {}
    pending: list[Analysis] = []
    for analysis in Analysis:
        refused = _refused(toolchain, platform, analysis)
        if refused is not None:
            statuses[analysis] = refused
        else:
            pending.append(analysis)

    if pending:
        # subprocess-bound, so threads are the right kind of concurrency here
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            probing = {
                analysis: pool.submit(_probe, toolchain, platform, analysis, runner)
                for analysis in pending
            }
        statuses.update({analysis: future.result() for analysis, future in probing.items()})
    # declaration order, so a fresh probe and a cache read hand back the same shape
    return {analysis: statuses[analysis] for analysis in Analysis}


def _refused(
    toolchain: Toolchain, platform: Platform, analysis: Analysis
) -> CapabilityStatus | None:
    """Answer the analyses this compiler or this OS can never do, without spawning anything."""
    unsupported = toolchain.unsupported_reason(analysis)
    if unsupported is not None:
        return CapabilityStatus(available=False, reason=unsupported)

    denial = platform.denied.get(analysis)
    if denial is not None:
        return CapabilityStatus(available=False, reason=denial.reason, suggestion=denial.suggestion)
    return None


def _probe(
    toolchain: Toolchain, platform: Platform, analysis: Analysis, runner: Runner
) -> CapabilityStatus:
    """Write this analysis's planted bug into a scratch directory and try to catch it."""
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as directory:
        source = Path(directory) / f"{PROBE_STEM}{analysis.value}.cpp"
        source.write_text(PROBES[analysis].source, encoding="utf-8")

        if analysis is Analysis.CLANG_TIDY:
            return _probe_clang_tidy(platform, source, runner)
        if analysis is Analysis.THREAD_SAFETY:
            return _probe_thread_safety(toolchain, platform, source, runner)
        return _probe_sanitizer(toolchain, platform, analysis, source, runner)


def _probe_sanitizer(
    toolchain: Toolchain,
    platform: Platform,
    analysis: Analysis,
    source: Path,
    runner: Runner,
) -> CapabilityStatus:
    """Build the buggy program under its sanitizer, run it, and look for the report."""
    kind = SANITIZER_FOR[analysis]
    binary = source.with_suffix("")
    compiled = Stage(
        name="compile",
        timeout_s=COMPILE_TIMEOUT_S,
        must_succeed=True,
        result=runner(
            [
                str(toolchain.compiler),
                *toolchain.sanitize_flags(kind),
                *platform.compile_extras,
                str(source),
                "-o",
                str(binary),
            ],
            timeout_s=COMPILE_TIMEOUT_S,
        ),
    )
    if compiled.result.exit_code != 0:
        return classify(analysis, platform, PROBES[analysis], (compiled,))

    # the sanitizer's options are half the decision to compile with it: a run without them
    # reports less than the build promised
    ran = Stage(
        name="run",
        timeout_s=RUN_TIMEOUT_S,
        must_succeed=False,  # a sanitizer that reports usually exits nonzero
        result=runner(
            [str(binary)],
            timeout_s=RUN_TIMEOUT_S,
            env=process.hygienic_env(PINNED_RUNTIME_ENV[kind]),
        ),
    )
    return classify(analysis, platform, PROBES[analysis], (compiled, ran))


def _probe_thread_safety(
    toolchain: Toolchain, platform: Platform, source: Path, runner: Runner
) -> CapabilityStatus:
    """Compile only: the warning is the detection, so there is nothing to run."""
    compiled = Stage(
        name="compile",
        timeout_s=COMPILE_TIMEOUT_S,
        must_succeed=True,
        result=runner(
            [str(toolchain.compiler), "-fsyntax-only", *toolchain.warning_flags, str(source)],
            timeout_s=COMPILE_TIMEOUT_S,
        ),
    )
    return classify(Analysis.THREAD_SAFETY, platform, PROBES[Analysis.THREAD_SAFETY], (compiled,))


def _probe_clang_tidy(platform: Platform, source: Path, runner: Runner) -> CapabilityStatus:
    """Run the one check the snippet was written to trip."""
    tidy = find_clang_tidy(platform)
    if tidy is None:
        return CapabilityStatus(
            available=False,
            reason=f"{CLANG_TIDY} is not on PATH and not in this platform's tool directories",
            suggestion=platform.install_hints.get(Analysis.CLANG_TIDY),
        )

    checked = Stage(
        name=CLANG_TIDY,
        timeout_s=RUN_TIMEOUT_S,
        must_succeed=False,
        result=runner(
            [str(tidy), f"--checks=-*,{TIDY_CHECK}", str(source), "--", "-std=c++20"],
            timeout_s=RUN_TIMEOUT_S,
        ),
    )
    return classify(Analysis.CLANG_TIDY, platform, PROBES[Analysis.CLANG_TIDY], (checked,))


def find_clang_tidy(platform: Platform) -> Path | None:
    """PATH first, then wherever this OS keeps llvm: brew does not link it into PATH."""
    on_path = shutil.which(CLANG_TIDY)
    if on_path is not None:
        return Path(on_path)
    for directory in platform.extra_tool_dirs:
        candidate = directory / CLANG_TIDY
        if candidate.exists():
            return candidate
    return None


def _failed(platform: Platform, stage: Stage) -> CapabilityStatus:
    """Explain a failed step: the platform's diagnosis when it has one, its own words when not."""
    signature = platform.diagnose(stage.result.output)
    if signature is not None:
        return CapabilityStatus(
            available=False, reason=signature.reason, suggestion=signature.suggestion
        )
    return CapabilityStatus(
        available=False,
        reason=(
            f"the {stage.name} step failed with exit {stage.result.exit_code}: "
            f"{excerpt(stage.result.output)}"
        ),
    )


# -------------------------------------------------------------------------------------- cache

STATUS_FIELDS = ("available", "reason", "suggestion", "verified_by", "limitations")


def read_cache(path: Path) -> dict[Analysis, CapabilityStatus] | None:
    """Return the cached statuses, or None when the file is missing or not what we wrote.

    Anything unexpected condemns the whole file rather than part of it: a cache that answers
    for some analyses and not others would leave the caller with a hole it cannot see.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or set(raw) != {analysis.value for analysis in Analysis}:
        return None

    statuses: dict[Analysis, CapabilityStatus] = {}
    for name, entry in raw.items():
        status = _status_from_json(entry)
        if status is None:
            return None
        statuses[Analysis(name)] = status
    # declaration order, matching a fresh probe: the file is sorted alphabetically instead
    return {analysis: statuses[analysis] for analysis in Analysis}


def write_cache(path: Path, statuses: Mapping[Analysis, CapabilityStatus]) -> None:
    """Write the statuses under this machine's fingerprint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {analysis.value: _status_to_json(status) for analysis, status in statuses.items()}
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def _status_to_json(status: CapabilityStatus) -> dict[str, object]:
    return {
        "available": status.available,
        "reason": status.reason,
        "suggestion": status.suggestion,
        "verified_by": status.verified_by,
        "limitations": list(status.limitations),
    }


def _status_from_json(entry: object) -> CapabilityStatus | None:
    """Rebuild one status, or None when the shape is not what this version writes."""
    if not isinstance(entry, dict) or set(entry) != set(STATUS_FIELDS):
        return None

    available = entry["available"]
    limitations = entry["limitations"]
    words = (entry["reason"], entry["suggestion"], entry["verified_by"])
    if not isinstance(available, bool) or not isinstance(limitations, list):
        return None
    if not all(isinstance(note, str) for note in limitations):
        return None
    if not all(word is None or isinstance(word, str) for word in words):
        return None

    reason, suggestion, verified_by = words
    return CapabilityStatus(
        available=available,
        reason=reason,
        suggestion=suggestion,
        verified_by=verified_by,
        limitations=tuple(limitations),
    )
