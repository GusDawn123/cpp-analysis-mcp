"""clang-tidy behind the analyzer contract: the plugin, and its real invocation.
The check step arrives as a constructor argument, so import time never triggers toolchain
discovery; file_check() binds a toolchain, platform, and runner it is handed.
"""

import tempfile
from pathlib import Path

from cpp_analysis_mcp.analyzers._adapter import (
    CHECK_TIMEOUT_S,
    NO_DATABASE_NOTE,
    STANDARD,
    Checked,
    CheckFile,
    as_findings,
    checkable_sources,
    membership_gate,
    offered,
    outcome,
    project_flags,
)
from cpp_analysis_mcp.analyzers.base import (
    AnalyzerContext,
    AnalyzerRun,
    Applicability,
    CostTier,
    Scope,
    UnitOfWork,
)
from cpp_analysis_mcp.capabilities import CLANG_TIDY, find_clang_tidy
from cpp_analysis_mcp.parsers.clang_tidy import parse as parse_tidy
from cpp_analysis_mcp.parsers.tidy_fixes import parse as parse_fixes
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner, hygienic_env
from cpp_analysis_mcp.store.models import (
    Analysis,
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Finding,
    SuggestedFix,
)
from cpp_analysis_mcp.toolchains.base import Toolchain

__all__ = [
    "CHECK_TIMEOUT_S",
    "DEFAULT_CHECKS",
    "DEFAULT_CHECKS_NOTE",
    "ClangTidyAnalyzer",
    "file_check",
]

STAGE = "clang-tidy"

EXPORT_PREFIX = "cpp-analysis-tidy-"

# the files clang-tidy itself looks for above a source file, in its own order
TIDY_CONFIG_NAMES = (".clang-tidy", "_clang-tidy")

# clang-tidy enables nothing on its own: given neither --checks nor a .clang-tidy it exits 1
# with "Error: no checks enabled." and usage text -- measured. So an unconfigured project gets
# correctness and cost families only; readability/modernize style opinions would bury them.
DEFAULT_CHECKS = "bugprone-*,clang-analyzer-*,performance-*,portability-*"

DEFAULT_CHECKS_NOTE = (
    f"this project committed no .clang-tidy, so a default check set was used: "
    f"{DEFAULT_CHECKS}. Pass `checks` to choose your own, or commit a .clang-tidy file"
)


class ClangTidyAnalyzer:
    """The static tier's first plugin: TU-grained, seconds-cheap, compilation-dependent."""

    name = "clang-tidy"
    cost_tier = CostTier.STATIC_SECONDS
    unit_of_work = UnitOfWork.TRANSLATION_UNIT

    def __init__(self, check: CheckFile) -> None:
        self._check = check

    def applicable(self, scope: Scope, context: AnalyzerContext) -> Applicability:
        return membership_gate(
            scope,
            context,
            no_sources_reason="no translation units in scope: clang-tidy analyzes what compiles",
        )

    def run(self, scope: Scope, context: AnalyzerContext) -> AnalyzerRun:
        findings: list[Finding] = []
        suggestions: list[SuggestedFix] = []
        for file in checkable_sources(scope, context):
            checked = self._check(scope.project_root / file)
            findings.extend(as_findings(checked, file, self.name))
            suggestions.extend(offered(checked))
        return AnalyzerRun(findings=tuple(findings), suggestions=tuple(suggestions))


def file_check(
    *,
    toolchain: Toolchain,
    platform: Platform,
    status: CapabilityStatus,
    checks: str | None,
    timeout_s: int,
    runner: Runner,
) -> CheckFile:
    """Bind the real clang-tidy invocation into the contract's one-argument shape.
    Everything a spawn needs travels in the closure, and the probe's status rides
    along so a report can say who verified the capability.
    """

    def check(source: Path) -> AnalysisReport | BuildFailure | CapabilityStatus:
        checked = _invoke(
            source,
            toolchain=toolchain,
            platform=platform,
            checks=checks,
            timeout_s=timeout_s,
            runner=runner,
        )
        if isinstance(checked, CapabilityStatus):
            return checked
        return outcome(checked, Analysis.CLANG_TIDY, status, engine=platform.engine)

    return check


def _invoke(
    source: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    checks: str | None,
    timeout_s: int,
    runner: Runner,
) -> Checked | CapabilityStatus:
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

    database, project = project_flags(source)
    effective, chosen_note = _tidy_checks(source, checks)
    # a scratch directory of this check's own: parallel checks would otherwise export over
    # each other, and the file is read back before the block removes it
    with tempfile.TemporaryDirectory(prefix=EXPORT_PREFIX, ignore_cleanup_errors=True) as scratch:
        exported = Path(scratch) / "fixes.yaml"
        result = runner(
            [
                str(tidy),
                # omitted only when the project committed a .clang-tidy: that file is then
                # what decides, which is what a project asking for its own checks means
                *((f"--checks={effective}",) if effective is not None else ()),
                # the same diagnostics again as YAML, with the edits behind them attached
                f"--export-fixes={exported}",
                str(source),
                # everything past -- is the compilation this file would have had
                "--",
                STANDARD,
                *platform.compile_extras,
                # and what it really did have, when a build wrote that down
                *project,
            ],
            timeout_s=timeout_s,
            env=hygienic_env({}),
        )
        suggestions = _exported(exported)
    return Checked(
        stage=STAGE,
        result=result,
        findings=parse_tidy(result.output),
        suggestions=suggestions,
        notes=(*chosen_note, *(() if database is not None else (NO_DATABASE_NOTE,))),
        database=database,
    )


def _exported(path: Path) -> tuple[SuggestedFix, ...]:
    """Read back the fix-its clang-tidy wrote, if it wrote a readable file at all."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return parse_fixes(text, _file_bytes)


def _file_bytes(file: str) -> bytes | None:
    """The bytes an export's offsets index into; None for a file gone since the check."""
    try:
        return Path(file).read_bytes()
    except OSError:
        return None


def _tidy_checks(source: Path, checks: str | None) -> tuple[str | None, tuple[str, ...]]:
    """Decide what to enable, and what the caller has to be told about that decision.
    An explicit `checks` is the caller's; a committed .clang-tidy is left to decide by
    passing no --checks; nothing at all is the case that used to come back as usage text.
    """
    if checks is not None:
        return checks, ()
    if any(
        (directory / name).is_file() for directory in source.parents for name in TIDY_CONFIG_NAMES
    ):
        return None, ()
    return DEFAULT_CHECKS, (DEFAULT_CHECKS_NOTE,)
