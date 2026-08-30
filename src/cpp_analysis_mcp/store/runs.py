"""The remembered last run: findings written whole, read back by fingerprint --
get_finding's memory, written on every review and audit. A run that cannot be read
answers None like a run that never happened; the miss explains itself upstream.
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from cpp_analysis_mcp.store.models import Finding

__all__ = ["find", "save"]

_RUNS = "runs"

# pydantic already serializes these dataclasses on the MCP wire; the same machinery
# round-trips them to disk, nested threads and frames included
_FINDINGS: TypeAdapter[tuple[Finding, ...]] = TypeAdapter(tuple[Finding, ...])


def save(cache_dir: Path, project_root: Path, findings: Sequence[Finding]) -> Path:
    """Write one project's latest run whole, replacing the one before it."""
    path = _path(cache_dir, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_FINDINGS.dump_json(tuple(findings), indent=2))
    return path


def find(cache_dir: Path, project_root: Path, fingerprint: str) -> Finding | None:
    """The remembered finding with this identity, or None when nothing can answer."""
    path = _path(cache_dir, project_root)
    try:
        remembered = _FINDINGS.validate_json(path.read_bytes())
    except (OSError, ValueError):
        return None
    # a linear scan: one interactive lookup over one run's findings
    return next((finding for finding in remembered if finding.fingerprint == fingerprint), None)


def _path(cache_dir: Path, project_root: Path) -> Path:
    project = sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_dir / _RUNS / f"{project}.json"
