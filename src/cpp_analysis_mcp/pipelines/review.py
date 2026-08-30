"""The review gate: audit remembers a ref, review reports only what a change added.

Everything composed here exists one layer down -- scope, plan, dispatch, store,
baselines. Baselines are recorded on purpose by audit and never built behind the
caller's back; a missing one reports everything and says so.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from cpp_analysis_mcp import compile_db, process
from cpp_analysis_mcp.analyzers import clang_tidy as tidy_plugin
from cpp_analysis_mcp.analyzers import warnings as warnings_plugin
from cpp_analysis_mcp.analyzers.base import Registry, Scope
from cpp_analysis_mcp.analyzers.clang_tidy import CHECK_TIMEOUT_S, ClangTidyAnalyzer
from cpp_analysis_mcp.analyzers.warnings import WarningsAnalyzer
from cpp_analysis_mcp.capabilities import find_clang_tidy
from cpp_analysis_mcp.planner.dispatch import execute
from cpp_analysis_mcp.planner.plan import Plan, Skip, Step, plan
from cpp_analysis_mcp.planner.scope import (
    analyzer_context,
    changed_since,
    current_ref,
    line_reader,
    relativizer,
    repo_root,
    tracked_files,
)
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store import baselines, runs
from cpp_analysis_mcp.store.baselines import Baseline
from cpp_analysis_mcp.store.fingerprints import SCHEME_VERSION
from cpp_analysis_mcp.store.models import Analysis, CapabilityStatus, Finding, Severity
from cpp_analysis_mcp.store.store import FindingStore
from cpp_analysis_mcp.toolchains.base import Toolchain

# index everything, full detail for the top few: the diversity ranking means five
# variations of one bug cannot crowd out the first report from four other files
N_DETAILED = 5


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One line of the everything-index: enough to decide whether to ask for detail."""

    fingerprint: str
    severity: Severity
    category: str
    file: str | None
    line: int | None
    occurrences: int


@dataclass(frozen=True, slots=True)
class ReviewReport:
    """What one review decided and found, plan trace included."""

    root: str
    against: str
    files: tuple[str, ...]
    steps: tuple[Step, ...]
    skips: tuple[Skip, ...]
    baseline_used: bool
    notes: tuple[str, ...]
    total_new: int
    index: tuple[IndexEntry, ...]
    detailed: tuple[Finding, ...]
    truncated: bool
    run_path: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    """The whole picture at one ref, and where its baseline was recorded."""

    root: str
    recorded_as: str
    files: tuple[str, ...]
    steps: tuple[Step, ...]
    skips: tuple[Skip, ...]
    total: int
    index: tuple[IndexEntry, ...]
    detailed: tuple[Finding, ...]
    truncated: bool
    baseline_path: str
    run_path: str


@dataclass(frozen=True, slots=True)
class NoSuchFinding:
    """A lookup miss that explains itself; get_finding never guesses."""

    reason: str


def review_project(
    project_dir: Path,
    against: str,
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    cache_dir: Path | None,
    runner: Runner = process.run,
) -> ReviewReport | CapabilityStatus:
    """Report only the findings the working tree added since `against`.

    With no trustworthy baseline for the ref, everything found is reported and a
    note says to audit the ref first -- never a silent guess, never a checkout.
    """
    scoped = changed_since(project_dir, against, runner=runner)
    if isinstance(scoped, CapabilityStatus):
        return scoped
    store, decided = _static_tier(
        scoped.root,
        scoped.files,
        toolchain=toolchain,
        platform=platform,
        capabilities=capabilities,
        runner=runner,
    )
    key = _invalidation_key(scoped.root, toolchain=toolchain, platform=platform)
    remembered = (
        None
        if cache_dir is None
        else baselines.load(cache_dir, scoped.root, ref=against, scheme=SCHEME_VERSION, key=key)
    )
    baseline_notes: tuple[str, ...] = ()
    if remembered is None:
        fresh = store.findings()
        baseline_notes = (
            f"no trustworthy baseline for {against!r}: everything found is reported. "
            f"Run audit on {against} to record one",
        )
    else:
        fresh = store.new_against(remembered.fingerprints)

    fresh_identities = {finding.fingerprint for finding in fresh}
    ranked = tuple(finding for finding in store.ranked() if finding.fingerprint in fresh_identities)
    index, detailed, truncated = _shaped(ranked)
    return ReviewReport(
        root=str(scoped.root),
        against=against,
        files=scoped.files,
        steps=decided.steps,
        skips=decided.skips,
        baseline_used=remembered is not None,
        notes=baseline_notes,
        total_new=len(ranked),
        index=index,
        detailed=detailed,
        truncated=truncated,
        run_path=_remember(cache_dir, scoped.root, store),
    )


def audit_project(
    project_dir: Path,
    *,
    record_as: str | None = None,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    cache_dir: Path | None,
    runner: Runner = process.run,
) -> AuditReport | CapabilityStatus:
    """Scan everything git tracks, report the whole picture, and record the baseline.

    Recording is the point: review(against=X) can only subtract what an audit at X
    wrote down. The label defaults to the current branch, or the commit when detached.
    """
    scoped = tracked_files(project_dir, runner=runner)
    if isinstance(scoped, CapabilityStatus):
        return scoped
    label = record_as if record_as is not None else current_ref(project_dir, runner=runner)
    if isinstance(label, CapabilityStatus):
        return label
    store, decided = _static_tier(
        scoped.root,
        scoped.files,
        toolchain=toolchain,
        platform=platform,
        capabilities=capabilities,
        runner=runner,
    )
    baseline_path = ""
    if cache_dir is not None:
        key = _invalidation_key(scoped.root, toolchain=toolchain, platform=platform)
        recorded = Baseline(
            ref=label, fingerprints=store.identities(), scheme=SCHEME_VERSION, key=key
        )
        baseline_path = str(baselines.save(cache_dir, scoped.root, recorded))

    ranked = store.ranked()
    index, detailed, truncated = _shaped(ranked)
    return AuditReport(
        root=str(scoped.root),
        recorded_as=label,
        files=scoped.files,
        steps=decided.steps,
        skips=decided.skips,
        total=len(ranked),
        index=index,
        detailed=detailed,
        truncated=truncated,
        baseline_path=baseline_path,
        run_path=_remember(cache_dir, scoped.root, store),
    )


def remembered_finding(
    project_dir: Path,
    fingerprint: str,
    *,
    cache_dir: Path,
    runner: Runner = process.run,
) -> Finding | NoSuchFinding:
    """Full detail for one identity out of the project's remembered last run."""
    root = repo_root(project_dir, runner=runner)
    if isinstance(root, CapabilityStatus):
        return NoSuchFinding(reason=root.reason or "git could not resolve the project")
    found = runs.find(cache_dir, root, fingerprint)
    if found is None:
        return NoSuchFinding(
            reason=(
                f"no remembered run holds {fingerprint!r}; run review or audit first, "
                "then ask again with a fingerprint from its index"
            )
        )
    return found


def _static_tier(
    root: Path,
    files: tuple[str, ...],
    *,
    toolchain: Toolchain,
    platform: Platform,
    capabilities: Mapping[Analysis, CapabilityStatus],
    runner: Runner,
) -> tuple[FindingStore, Plan]:
    """Plan and run both compile-time plugins over the scope, findings into one store."""
    named = {
        ClangTidyAnalyzer.name: capabilities[Analysis.CLANG_TIDY],
        WarningsAnalyzer.name: capabilities[Analysis.THREAD_SAFETY],
    }
    context = analyzer_context(root, named)
    scope = Scope(project_root=root, files=files)
    registry = Registry()
    registry.register(
        ClangTidyAnalyzer(
            check=tidy_plugin.file_check(
                toolchain=toolchain,
                platform=platform,
                status=capabilities[Analysis.CLANG_TIDY],
                checks=None,
                timeout_s=CHECK_TIMEOUT_S,
                runner=runner,
            )
        )
    )
    registry.register(
        WarningsAnalyzer(
            check=warnings_plugin.file_check(
                toolchain=toolchain,
                platform=platform,
                status=capabilities[Analysis.THREAD_SAFETY],
                checks=None,
                timeout_s=CHECK_TIMEOUT_S,
                runner=runner,
            )
        )
    )
    decided = plan(scope, context, registry)
    ran = execute(decided, scope, context, registry)

    store = FindingStore()
    reader = line_reader()
    canonical = relativizer(root)
    for finished in ran:
        # each tool's report is one run: occurrence ranks resolve per analyzer
        store.ingest(finished.findings, reader, canonical=canonical)
    return store, decided


def _invalidation_key(root: Path, *, toolchain: Toolchain, platform: Platform) -> dict[str, str]:
    """The architecture-v2 invalidation list, read as named facts of this moment."""
    return {
        "compiler": f"{toolchain.version} at {toolchain.compiler}",
        "flags": _content_identity(compile_db.find_under(root)),
        "config": _config_identity(root),
        "clang-tidy": _tool_identity(find_clang_tidy(platform)),
        "checks": tidy_plugin.DEFAULT_CHECKS,
    }


def _content_identity(path: Path | None) -> str:
    if path is None:
        return "none"
    try:
        return sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unreadable"


def _config_identity(root: Path) -> str:
    # per-file discovery walks parents; a whole-project run answers to the root's file
    committed = [root / name for name in tidy_plugin.TIDY_CONFIG_NAMES if (root / name).is_file()]
    return _content_identity(committed[0]) if committed else "none"


def _tool_identity(tool: Path | None) -> str:
    if tool is None:
        return "none"
    try:
        return f"{tool}:{tool.stat().st_mtime_ns}"
    except OSError:
        return str(tool)


def _remember(cache_dir: Path | None, root: Path, store: FindingStore) -> str:
    if cache_dir is None:
        return ""
    return str(runs.save(cache_dir, root, store.findings()))


def _shaped(
    ranked: Sequence[Finding],
) -> tuple[tuple[IndexEntry, ...], tuple[Finding, ...], bool]:
    index = tuple(
        IndexEntry(
            fingerprint=finding.fingerprint,
            severity=finding.severity,
            category=finding.category,
            file=finding.location.file if finding.location is not None else None,
            line=finding.location.line if finding.location is not None else None,
            occurrences=finding.occurrences,
        )
        for finding in ranked
    )
    return index, tuple(ranked[:N_DETAILED]), len(ranked) > N_DETAILED
