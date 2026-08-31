"""Baselines: the identities a past run already knew, remembered with their alibi. Trusted
only while the world that produced them holds still -- the invalidation facts and scheme
travel with the saved set, and any drift reads as "no baseline".
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

__all__ = ["Baseline", "load", "save"]

_BASELINES = "baselines"


@dataclass(frozen=True, slots=True)
class Baseline:
    """One ref's remembered identities, and the facts that make them comparable."""

    ref: str
    fingerprints: frozenset[str]
    scheme: int
    # the invalidation facts as named strings -- compiler, flags, config, tool
    # versions -- compared whole at load, so any drift retires the baseline
    key: Mapping[str, str]

    def __post_init__(self) -> None:
        # shared and frozen like every model: rebinding is blocked by frozen=, and
        # this blocks editing the mapping underneath a saved comparison
        object.__setattr__(self, "key", MappingProxyType(dict(self.key)))


def save(cache_dir: Path, project_root: Path, baseline: Baseline) -> Path:
    """Write one ref's baseline, replacing whatever that ref had before."""
    path = _path(cache_dir, project_root, baseline.ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "ref": baseline.ref,
        "scheme": baseline.scheme,
        "key": dict(baseline.key),
        "fingerprints": sorted(baseline.fingerprints),
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def load(
    cache_dir: Path,
    project_root: Path,
    *,
    ref: str,
    scheme: int,
    key: Mapping[str, str],
) -> Baseline | None:
    """The remembered baseline, or None when there is none worth trusting. Missing,
    unreadable, wrong ref or scheme, and any drifted fact all read the same on purpose:
    no baseline -- report everything rather than subtract against a vanished world.
    """
    path = _path(cache_dir, project_root, ref)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("ref") != ref or document.get("scheme") != scheme:
        return None
    if document.get("key") != dict(key):
        return None
    prints = document.get("fingerprints")
    if not isinstance(prints, list) or not all(isinstance(print_, str) for print_ in prints):
        return None
    return Baseline(ref=ref, fingerprints=frozenset(prints), scheme=scheme, key=key)


def _path(cache_dir: Path, project_root: Path, ref: str) -> Path:
    # hashed names: roots and refs hold path separators and other characters no
    # filename wants, and the readable originals live inside the document
    project = sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    named = sha256(ref.encode("utf-8")).hexdigest()[:16]
    return cache_dir / _BASELINES / f"{project}-{named}.json"
