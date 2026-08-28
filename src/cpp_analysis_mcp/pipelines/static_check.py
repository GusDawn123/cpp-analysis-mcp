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

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from cpp_analysis_mcp import compile_db, process
from cpp_analysis_mcp.analyzers.base import AnalyzerContext, Registry, Resolution, Scope
from cpp_analysis_mcp.analyzers.clang_tidy import ClangTidyAnalyzer
from cpp_analysis_mcp.analyzers.warnings import WarningsAnalyzer
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
from cpp_analysis_mcp.store.fingerprints import fingerprint_batch
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

# the files clang-tidy itself looks for above a source file, in its own order
TIDY_CONFIG_NAMES = (".clang-tidy", "_clang-tidy")

# clang-tidy enables nothing on its own. Given neither --checks nor a .clang-tidy above the
# file, clang-tidy 22 exits 1 printing "Error: no checks enabled." and its whole usage text,
# which parses to no findings and reads as a broken tool. Measured. So a project that has
# committed no configuration gets one, and is told that it did.
#
# Correctness and cost, not style: these are the families whose findings are worth acting on
# without knowing anything about a project's conventions. readability-* and modernize-* are
# deliberately absent -- they are opinions about how code should look, and handing someone a
# hundred of them unasked buries the four that matter.
DEFAULT_CHECKS = "bugprone-*,clang-analyzer-*,performance-*,portability-*"

DEFAULT_CHECKS_NOTE = (
    f"this project committed no .clang-tidy, so a default check set was used: "
    f"{DEFAULT_CHECKS}. Pass `checks` to choose your own, or commit a .clang-tidy file"
)

NO_DATABASE_NOTE = (
    "no compile_commands.json was found near this file, so the check ran with no project "
    "include directories; a file that includes a project header will fail to parse"
)

# what clang says when an include could not be resolved, and the only case where the missing
# database is the explanation rather than a guess about someone else's compile error
MISSING_INCLUDE = "file not found"

NO_DATABASE_SUGGESTION = (
    "generate a compilation database and this check will find it by itself: configure with "
    "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON, or run bear -- <your build> for a non-CMake build"
)


@dataclass(frozen=True, slots=True)
class _Checked:
    """What one check step produced: which step it was, what it printed, what that parsed to."""

    stage: str
    result: process.RunResult
    findings: tuple[Finding, ...]
    # what the caller has to know to read this result, beyond what the platform already said:
    # the check set nobody chose, the include paths that were never found
    notes: tuple[str, ...] = ()
    # whether a compilation database was behind the flags, which decides whether a parse
    # failure is explained by its absence or by the code
    database: Path | None = None


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
    outcome, _ = _routed_check(
        source,
        analysis,
        toolchain=toolchain,
        platform=platform,
        capabilities=capabilities,
        checks=checks,
        timeout_s=timeout_s,
        runner=runner,
    )
    return outcome


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


def _routed_check(
    source: Path,
    analysis: Analysis,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    checks: str | None,
    timeout_s: int,
    runner: Runner,
) -> tuple[AnalysisReport | BuildFailure | CapabilityStatus, tuple[Resolution, ...]]:
    """The registry decides, the check runs, and every finding leaves carrying identity.

    The gate that used to be an inline capability lookup is now the registry's chain over
    both compile-time plugins, so a refusal here and a skip in a future plan trace are
    the same verdict from the same code. A caller-named scope passes the selection gates
    by design; the capability gate binds regardless, and a refusal returns the probe's
    own status object, exactly as the inline gate did.

    The plugins' run loop stays out of this path on purpose: it flattens failures into
    findings for the store, and this surface still owes callers the failure itself. The
    verdict is the plugins' contribution here; execution stays with the check steps.
    """
    runner_for = _RUNNERS[analysis]
    resolutions = _registry().resolve(
        Scope(project_root=source.parent, files=(source.name,), caller_named=True),
        AnalyzerContext(
            capabilities={
                ClangTidyAnalyzer.name: capabilities[Analysis.CLANG_TIDY],
                WarningsAnalyzer.name: capabilities[Analysis.THREAD_SAFETY],
            }
        ),
    )
    verdict = next(
        row.verdict for row in resolutions if row.analyzer.name == _PLUGIN_NAMES[analysis]
    )
    if not verdict.eligible:
        return capabilities[analysis], resolutions

    checked = runner_for(
        source,
        toolchain=toolchain,
        platform=platform,
        checks=checks,
        timeout_s=timeout_s,
        runner=runner,
    )
    if isinstance(checked, CapabilityStatus):
        return checked, resolutions
    outcome = _outcome(checked, analysis, capabilities[analysis])
    if isinstance(outcome, AnalysisReport):
        outcome = replace(outcome, findings=fingerprint_batch(outcome.findings, _line_reader()))
    return outcome, resolutions


def _registry() -> Registry:
    """Both compile-time plugins, registered for their gates alone.

    The sentinel check documents that resolution never executes: if it ever fires, a
    plugin's run loop entered a path that promised verdicts only.
    """
    registry = Registry()
    registry.register(ClangTidyAnalyzer(check=_no_execution))
    registry.register(WarningsAnalyzer(check=_no_execution))
    return registry


def _no_execution(source: Path) -> AnalysisReport | BuildFailure | CapabilityStatus:
    raise RuntimeError(f"gate resolution never runs a tool, but was asked to check {source}")


def _line_reader() -> Callable[[str, int], str]:
    """Read flagged lines for fingerprinting, each file once, misses as empty text.

    Findings name files the way the tool printed them, so the paths here are absolute --
    which also means these fingerprints are not portable across checkouts yet. That is
    deferred, deliberately: the Phase 2 scope resolver owns relativization, and no
    persisted baseline exists today for a later change to orphan.
    """
    cache: dict[str, tuple[str, ...]] = {}

    def read_line(file: str, line: int) -> str:
        lines = cache.get(file)
        if lines is None:
            try:
                text = Path(file).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            lines = tuple(text.splitlines())
            cache[file] = lines
        return lines[line - 1] if 1 <= line <= len(lines) else ""

    return read_line


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
    database, project = _project_flags(source)
    result = runner(
        [
            str(toolchain.compiler),
            STANDARD,
            # no output file: the warnings are the product, and a snippet with no main()
            # must still be checkable, which a link step would refuse
            "-fsyntax-only",
            *toolchain.warning_flags,
            *platform.compile_extras,
            # after ours, so a project that builds at a different language standard wins:
            # last -std= on a clang command line is the one that takes effect
            *project,
            str(source),
        ],
        timeout_s=timeout_s,
        env=process.hygienic_env({}),
    )
    return _Checked(
        stage=THREAD_SAFETY_STAGE,
        result=result,
        findings=diagnostics.parse(result.output),
        notes=() if database is not None else (NO_DATABASE_NOTE,),
        database=database,
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

    database, project = _project_flags(source)
    effective, chosen_note = _tidy_checks(source, checks)
    result = runner(
        [
            str(tidy),
            # omitted only when the project committed a .clang-tidy: that file is then what
            # decides, which is what a project asking for its own checks means
            *((f"--checks={effective}",) if effective is not None else ()),
            str(source),
            # everything past -- is the compilation this file would have had
            "--",
            STANDARD,
            *platform.compile_extras,
            # and what it really did have, when a build wrote that down
            *project,
        ],
        timeout_s=timeout_s,
        env=process.hygienic_env({}),
    )
    return _Checked(
        stage=CLANG_TIDY_STAGE,
        result=result,
        findings=clang_tidy.parse(result.output),
        notes=(*chosen_note, *(() if database is not None else (NO_DATABASE_NOTE,))),
        database=database,
    )


def _project_flags(source: Path) -> tuple[Path | None, tuple[str, ...]]:
    """Find this file's compilation database and take the flags it needs to parse.

    Both checks want the same thing and neither can do its job without it: a file that
    includes a project header is unparseable until something says where that header lives,
    and the build already wrote it down.
    """
    database = compile_db.find(source)
    if database is None:
        return None, ()
    return database, compile_db.flags_for(database, source)


def _tidy_checks(source: Path, checks: str | None) -> tuple[str | None, tuple[str, ...]]:
    """Decide what to enable, and what the caller has to be told about that decision.

    Three cases and only the last of them chooses anything. An explicit `checks` is the
    caller's. A committed .clang-tidy is the project's, and is left to decide by passing no
    --checks at all. Nothing at all is the case that used to come back as usage text.
    """
    if checks is not None:
        return checks, ()
    if any(
        (directory / name).is_file() for directory in source.parents for name in TIDY_CONFIG_NAMES
    ):
        return None, ()
    return DEFAULT_CHECKS, (DEFAULT_CHECKS_NOTE,)


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
        # an unresolved include with no database behind the check is the one failure this
        # pipeline can explain and fix; every other one belongs to the code and the tool's
        # own words are the answer, so no reason is invented for it
        unresolved = checked.database is None and MISSING_INCLUDE in checked.result.output
        return BuildFailure(
            stage=checked.stage,
            output=checked.result.output,
            reason=NO_DATABASE_NOTE if unresolved else None,
            suggestion=NO_DATABASE_SUGGESTION if unresolved else None,
        )

    return AnalysisReport(
        analysis=analysis,
        findings=checked.findings,
        build_warnings=(),
        exit_code=checked.result.exit_code,
        timed_out=False,
        # the platform's caveats and this run's own, which are about what was decided for
        # the caller rather than about the machine
        limitations=(*status.limitations, *checked.notes),
        verified_by=status.verified_by,
    )


# only the compile-time analyses: asking this pipeline for a sanitizer raises rather than
# quietly checking syntax and calling the result a clean TSan run
_RUNNERS: Mapping[Analysis, _Check] = {
    Analysis.THREAD_SAFETY: _check_thread_safety,
    Analysis.CLANG_TIDY: _check_clang_tidy,
}

# which plugin fronts each analysis in the registry. The warnings plugin answers for the
# thread-safety probe because that is literally today's gate for the warnings path; the
# probe rework of a later phase renames probes after the analyzers that own them
_PLUGIN_NAMES: Mapping[Analysis, str] = {
    Analysis.THREAD_SAFETY: WarningsAnalyzer.name,
    Analysis.CLANG_TIDY: ClangTidyAnalyzer.name,
}
