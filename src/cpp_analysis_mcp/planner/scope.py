"""Scope resolution: one canonical language for paths, and the review's two questions.
relativizer() turns tool spellings into the project-relative POSIX that fingerprints hash
(ADR-0002); changed_since() asks git what changed; analyzer_context() asks the build.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cpp_analysis_mcp import compile_db, process
from cpp_analysis_mcp.analyzers.base import AnalyzerContext
from cpp_analysis_mcp.process import Runner, RunResult
from cpp_analysis_mcp.store.models import CapabilityStatus

# one plumbing question per spawn; even huge repos answer in seconds, and a hung git
# must not stall a review
GIT_TIMEOUT_S = 30


def relativizer(root: Path) -> Callable[[str], str]:
    """Canonicalize spellings against the root, resolving each distinct one once. Under
    the root: relative POSIX. Outside: kept whole, so same-named files cannot collide.
    Relative spellings pass through -- only the tool knows what they were relative to.
    """
    settled = root.resolve()
    cache: dict[str, str] = {}

    def canonical(path: str) -> str:
        known = cache.get(path)
        if known is None:
            known = _canonical(path, settled)
            cache[path] = known
        return known

    return canonical


def line_reader() -> Callable[[str, int], str]:
    """Read flagged lines for fingerprinting, each file once, misses as empty text.
    Reads use the paths exactly as the tool printed them; how a path enters the hash
    is the caller's `canonical` to decide, never this reader's.
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


def _canonical(path: str, root: Path) -> str:
    spelled = Path(path)
    if not spelled.is_absolute():
        return path
    # resolve() settles ../ segments and, on case-insensitive filesystems, folds an
    # existing file's spelling to the one on disk -- both are identity, not location
    resolved = spelled.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


@dataclass(frozen=True, slots=True)
class ChangedScope:
    """What a diff against a ref resolved to: the repo root and the files to look at."""

    root: Path
    # repo-relative POSIX, exactly as git prints them -- already the canonical form
    files: tuple[str, ...]


_NO_GIT = CapabilityStatus(
    available=False,
    reason="git is not on PATH, so nothing can say what changed; name files explicitly",
)


def repo_root(directory: Path, *, runner: Runner = process.run) -> Path | CapabilityStatus:
    """The repository root the directory belongs to, or git's own refusal."""
    if shutil.which("git") is None:
        return _NO_GIT
    top = runner(_git(directory, "rev-parse", "--show-toplevel"), timeout_s=GIT_TIMEOUT_S)
    if top.exit_code != 0:
        return _refused(top)
    # line endings only: a root that genuinely ends in a space must survive the trim
    return Path(top.output.strip("\r\n"))


def changed_since(
    directory: Path, ref: str, *, runner: Runner = process.run
) -> ChangedScope | CapabilityStatus:
    """Ask git what changed between the working tree and ref, untracked files included.
    Deletes are dropped and a rename counts as its new name. Refusals carry git's own
    words, and a missing git refuses too: scope never silently widens to a full scan.
    """
    root = repo_root(directory, runner=runner)
    if isinstance(root, CapabilityStatus):
        return root
    # both questions run at the root: ls-files answers cwd-relative and cwd-limited, so
    # asked from a subdirectory it would silently drop untracked files everywhere else
    diffed = runner(_git(root, "diff", "--name-status", "-z", ref), timeout_s=GIT_TIMEOUT_S)
    if diffed.exit_code != 0:
        return _refused(diffed)
    untracked = runner(
        _git(root, "ls-files", "--others", "--exclude-standard", "-z"),
        timeout_s=GIT_TIMEOUT_S,
    )
    if untracked.exit_code != 0:
        return _refused(untracked)

    fresh = (name for name in untracked.output.split("\0") if name)
    files = dict.fromkeys((*_diffed_files(diffed.output), *fresh))
    return ChangedScope(root=root, files=tuple(files))


def tracked_files(
    directory: Path, *, runner: Runner = process.run
) -> ChangedScope | CapabilityStatus:
    """Every file git tracks, from the root -- the audit's whole-project scope. Selection
    stays with the analyzer gates: non-C++ files ride along and are refused there, so
    scope resolution never grows its own idea of relevance.
    """
    root = repo_root(directory, runner=runner)
    if isinstance(root, CapabilityStatus):
        return root
    listed = runner(_git(root, "ls-files", "-z"), timeout_s=GIT_TIMEOUT_S)
    if listed.exit_code != 0:
        return _refused(listed)
    return ChangedScope(root=root, files=tuple(name for name in listed.output.split("\0") if name))


def current_ref(directory: Path, *, runner: Runner = process.run) -> str | CapabilityStatus:
    """The label a baseline recorded now naturally carries: the branch name, or the
    commit itself when the head is detached and no branch name exists."""
    if shutil.which("git") is None:
        return _NO_GIT
    named = runner(_git(directory, "rev-parse", "--abbrev-ref", "HEAD"), timeout_s=GIT_TIMEOUT_S)
    if named.exit_code != 0:
        return _refused(named)
    ref = named.output.strip()
    if ref != "HEAD":
        return ref
    pinned = runner(_git(directory, "rev-parse", "HEAD"), timeout_s=GIT_TIMEOUT_S)
    if pinned.exit_code != 0:
        return _refused(pinned)
    return pinned.output.strip()


def analyzer_context(root: Path, capabilities: Mapping[str, CapabilityStatus]) -> AnalyzerContext:
    """One place answers "which files are in the build?" for every gate. The database's
    files come back canonical (root-relative POSIX) so membership and fingerprints speak
    one language; no database, or a broken one, means the empty set.
    """
    database = compile_db.find_under(root)
    if database is None:
        return AnalyzerContext(capabilities=capabilities)
    canonical = relativizer(root)
    units = frozenset(canonical(str(source)) for source in compile_db.sources(database))
    return AnalyzerContext(translation_units=units, capabilities=capabilities)


def _git(directory: Path, *args: str) -> list[str]:
    return ["git", "-C", str(directory), *args]


def _refused(result: RunResult) -> CapabilityStatus:
    lines = result.output.strip().splitlines()
    return CapabilityStatus(
        available=False, reason=lines[0] if lines else "git answered with nothing"
    )


def _diffed_files(output: str) -> list[str]:
    # -z framing: NUL after every field, so names with spaces or exotic bytes survive
    tokens = [token for token in output.split("\0") if token]
    files: list[str] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        if status.startswith(("R", "C")):
            # a rename or copy carries old then new; the new name is the one on disk
            files.append(tokens[index + 2])
            index += 3
        elif status.startswith("D"):
            index += 2
        else:
            files.append(tokens[index + 1])
            index += 2
    return files
