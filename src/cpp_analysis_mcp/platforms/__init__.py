"""OS concerns only: symbolizer, profiler backend, tool locations. Passed in, never looked up."""

from __future__ import annotations

import platform

from cpp_analysis_mcp.platforms import darwin, linux
from cpp_analysis_mcp.platforms.base import Platform

__all__ = ["Platform", "detect"]

DETECTORS = {linux.NAME: linux.detect, darwin.NAME: darwin.detect}


def detect() -> Platform:
    """Return the running OS's Platform. The one sanctioned platform lookup (rule 3)."""
    name = platform.system().lower()
    detector = DETECTORS.get(name)
    if detector is None:
        raise NotImplementedError(f"unsupported operating system: {name}")
    return detector()
