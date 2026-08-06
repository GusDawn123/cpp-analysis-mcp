"""Gate on the capability, run one compile-time check, parse -- the cheapest rung of the ladder.

Two analyses arrive here and they look nothing alike from outside: -Wthread-safety is a flag on
the compiler already in hand, clang-tidy is a separate program that has to be found first.
Underneath they are the same three steps against the same gate, which is why they share a file
rather than each growing their own idea of what an unavailable analysis returns.

The gate is a hard stop, not a note on the report. Both refusals come through it and neither is
special-cased here: gcc has no -Wthread-safety to offer, and a machine with no clang-tidy
installed has nothing to run -- the probe wrote both down as an unavailable status already.
Checking anyway would produce an empty finding list that reads exactly like clean code, which is
the false all-clear this project exists to avoid.

Nothing is linked and nothing is executed. -fsyntax-only is the whole point: the warnings are
the product, and a snippet with no main() still has to be checkable, which a link step refuses.

Composes primitives only, never another pipeline (rule 1); the Platform and Toolchain arrive as
arguments (rule 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cpp_analysis_mcp import process
from cpp_analysis_mcp.capabilities import CLANG_TIDY, find_clang_tidy
from cpp_analysis_mcp.models import (
    Analysis,
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
)
from cpp_analysis_mcp.parsers import clang_tidy, diagnostics
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.toolchains.base import Toolchain

# nothing runs the checked program, so this only has to cover parsing one translation unit;
# it is generous next to the seconds either check usually takes
CHECK_TIMEOUT_S = 60

SNIPPET_STEM = "snippet"

STANDARD = "-std=c++20"

# which step died, for BuildFailure.stage: the analysis the caller asked for, not the binary
# that ran, since "clang" would not tell a reader which of the two checks failed
THREAD_SAFETY_STAGE = "thread-safety"
CLANG_TIDY_STAGE = "clang-tidy"


@dataclass(frozen=True, slots=True)
class _Checked:
    """What one check step produced: which step it was, what it printed, what that parsed to."""

    stage: str
    result: process.RunResult
    findings: tuple[Finding, ...]


class _Check(Protocol):
    """One analysis's step: spawn its tool once and read what came back.

    `checks` means something to clang-tidy alone. It rides on the shared shape rather than
    forking the signature, because the alternative is the caller branching on the analysis
    before dispatching on it.
    """

    def __call__(
        self,
        source: Path,
        *,
        toolchain: Toolchain,
        platform: Platform,
        checks: str | None,
        timeout_s: int,
        runner: Runner,
    ) -> _Checked | CapabilityStatus: ...


def check_file(
    source: Path,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    checks: str | None = None,
    timeout_s: int = CHECK_TIMEOUT_S,
    runner: Runner = process.run,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Check one source file at compile time and report what the tool said about it.

    Three outcomes, all of them ordinary: a report, the failure that stopped one being
    produced, or the capability status saying this machine cannot run this check at all.
    """
    runner_for = _RUNNERS[analysis]
    status = capabilities[analysis]
    if not status.available:
        return status

    checked = runner_for(
        source,
        toolchain=toolchain,
        platform=platform,
        checks=checks,
        timeout_s=timeout_s,
        runner=runner,
    )
    if isinstance(checked, CapabilityStatus):
        return checked
    return _outcome(checked, analysis, status)


def check_snippet(
    text: str,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    build_dir: Path,
    checks: str | None = None,
    timeout_s: int = CHECK_TIMEOUT_S,
    runner: Runner = process.run,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Write a piece of C++ to disk and check it as a file.

    The file stays behind on purpose: every finding names it, and a location pointing at
    something that was deleted is unreadable.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    source = build_dir / f"{SNIPPET_STEM}.cpp"
    source.write_text(text, encoding="utf-8")

    return check_file(
        source,
        analysis,
        toolchain=toolchain,
        platform=platform,
        capabilities=capabilities,
        checks=checks,
        timeout_s=timeout_s,
        runner=runner,
    )


def _check_thread_safety(
    source: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    checks: str | None,
    timeout_s: int,
    runner: Runner,
) -> _Checked | CapabilityStatus:
    """Compile the file and read the compiler's own output; `checks` is clang-tidy's alone."""
    result = runner(
        [
            str(toolchain.compiler),
            STANDARD,
            # no output file: the warnings are the product, and a snippet with no main()
            # must still be checkable, which a link step would refuse
            "-fsyntax-only",
            *toolchain.warning_flags,
            *platform.compile_extras,
            str(source),
        ],
        timeout_s=timeout_s,
        env=process.hygienic_env({}),
    )
    return _Checked(
        stage=THREAD_SAFETY_STAGE,
        result=result,
        findings=diagnostics.parse(result.output),
    )


def _check_clang_tidy(
    source: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    checks: str | None,
    timeout_s: int,
    runner: Runner,
) -> _Checked | CapabilityStatus:
    """Run clang-tidy over the file; the build compiler is not involved in what it checks."""
    tidy = find_clang_tidy(platform)
    if tidy is None:
        # the gate already passed, so the probe found clang-tidy -- but that answer can be
        # a cached one, and the binary may have been uninstalled since it was written
        return CapabilityStatus(
            available=False,
            reason=f"{CLANG_TIDY} is not on PATH and not in this platform's tool directories",
            suggestion=platform.install_hints.get(Analysis.CLANG_TIDY),
        )

    result = runner(
        [
            str(tidy),
            # no --checks at all means clang-tidy's own defaults, which is what lets a
            # project's committed .clang-tidy file decide instead of this argument
            *((f"--checks={checks}",) if checks is not None else ()),
            str(source),
            # everything past -- is the compilation this file would have had
            "--",
            STANDARD,
            *platform.compile_extras,
        ],
        timeout_s=timeout_s,
        env=process.hygienic_env({}),
    )
    return _Checked(
        stage=CLANG_TIDY_STAGE,
        result=result,
        findings=clang_tidy.parse(result.output),
    )


def _outcome(
    checked: _Checked, analysis: Analysis, status: CapabilityStatus
) -> AnalysisReport | BuildFailure:
    """Decide whether what came back is a report or the failure that replaced one."""
    if checked.result.timed_out:
        return BuildFailure(stage=checked.stage, output=checked.result.output, timed_out=True)
    # a nonzero exit with findings behind it is code that does not compile, and clang-tidy
    # files those under clang-diagnostic-error like any other check -- structured beats a
    # text blob. A nonzero exit with nothing parsed is the tool itself failing, and that
    # output is the only thing that explains it.
    if checked.result.exit_code != 0 and not checked.findings:
        return BuildFailure(stage=checked.stage, output=checked.result.output)

    return AnalysisReport(
        analysis=analysis,
        findings=checked.findings,
        build_warnings=(),
        exit_code=checked.result.exit_code,
        timed_out=False,
        limitations=status.limitations,
        verified_by=status.verified_by,
    )


# only the compile-time analyses: asking this pipeline for a sanitizer raises rather than
# quietly checking syntax and calling the result a clean TSan run
_RUNNERS: Mapping[Analysis, _Check] = {
    Analysis.THREAD_SAFETY: _check_thread_safety,
    Analysis.CLANG_TIDY: _check_clang_tidy,
}
