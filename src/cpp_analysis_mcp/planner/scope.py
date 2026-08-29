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

    Under the root: relative POSIX ("src/a.cpp"). Outside it: kept whole, so two
    same-named files cannot collide. Relative spellings pass through untouched --
    only the tool that printed one knows what it was relative to.
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
