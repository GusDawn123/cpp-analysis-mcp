"""Canonical path spellings for identity, one per file however a tool spelled it.

Fingerprints hash project-relative POSIX paths (ADR-0002); tools print absolute ones
in whatever style the OS taught them. relativizer() bridges the two, so the same
finding carries the same identity on every machine and in every checkout.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def relativizer(root: Path) -> Callable[[str], str]:
    """Canonicalize spellings against the root, resolving each distinct one once.

    Absolute paths under the root come back relative POSIX ("src/a.cpp"); outside it
    they stay whole, since truncating a caller-named file to a basename would collide
    two same-named files in different projects. Relative spellings pass through
    untouched: they are relative to some tool's working directory, and resolving them
    against this process's own cwd would invent a location nobody used.
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
