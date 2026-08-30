"""How dangerous a finding is: the tiers a report counts by and leads with.

Witnessed beats suspected. CRITICAL belongs to defects a runtime tool watched happen;
static analysis matches patterns in source text and tops out at MAJOR, however sure it
sounds. Both opinions live in the tables below, so a new one is a row, never a branch.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from cpp_analysis_mcp.store.models import Finding

__all__ = ["STATIC_TIERS", "WITNESSED", "Tier", "tier_for"]


class Tier(StrEnum):
    """Danger, most first. Declaration order is the order a report counts them in."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    STYLE = "style"
    UNRATED = "unrated"


# what a runtime tool's word is worth, keyed by the tool field its parser writes. A leak
# is the exception: the program answered correctly and kept the memory, which is major
# rather than a defect that corrupted the run.
WITNESSED: Mapping[str, Tier] = {
    "asan": Tier.CRITICAL,
    "tsan": Tier.CRITICAL,
    "ubsan": Tier.CRITICAL,
    "lsan": Tier.MAJOR,
}

# first match wins, so the specific rows sit above the families they belong to. A pattern
# is an exact category, `prefix*`, or `*suffix` -- no regex, because a table of opinions
# should be readable by anyone who wants to add one.
STATIC_TIERS: tuple[tuple[str, Tier], ...] = (
    # the two ways a check produces nothing: it died, or it was never watching. Either
    # one must surface, or a file nobody managed to analyze reads as a clean file.
    ("*-failed", Tier.MAJOR),
    ("tool-unavailable", Tier.MAJOR),
    ("bugprone-use-after-move", Tier.MAJOR),
    ("bugprone-dangling-handle", Tier.MAJOR),
    ("bugprone-exception-escape", Tier.MAJOR),
    ("clang-analyzer-cplusplus.NewDelete*", Tier.MAJOR),
    # unchecked operator[] and friends: the read is out of bounds or it is not
    ("cppcoreguidelines-pro-bounds-*", Tier.MAJOR),
    ("cppcoreguidelines-pro-type-member-init", Tier.MAJOR),
    # clang's -Wthread-safety, which reads the lock annotations rather than guessing
    ("thread-safety*", Tier.MAJOR),
    ("cppcoreguidelines-init-variables", Tier.MINOR),
    ("performance-*", Tier.MINOR),
    ("clang-analyzer-*", Tier.MINOR),
    ("clang-diagnostic-*", Tier.MINOR),
    ("cppcoreguidelines-avoid-magic-numbers", Tier.STYLE),
    ("modernize-*", Tier.STYLE),
    ("readability-*", Tier.STYLE),
    ("cppcoreguidelines-*", Tier.STYLE),
)


def tier_for(finding: Finding) -> Tier:
    """The tier this finding lands in: what observed it first, then what it is about.

    A category no row claims comes back UNRATED. Guessing a tier for an unknown check
    would put a number on an opinion nobody formed.
    """
    observed = WITNESSED.get(finding.tool)
    if observed is not None:
        return observed
    for pattern, tier in STATIC_TIERS:
        if _matches(pattern, finding.category):
            return tier
    return Tier.UNRATED


def _matches(pattern: str, category: str) -> bool:
    if pattern.startswith("*"):
        return category.endswith(pattern[1:])
    if pattern.endswith("*"):
        return category.startswith(pattern[:-1])
    return pattern == category
