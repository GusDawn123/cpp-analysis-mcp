"""Read a hotspot ranking back in plain words.

A profile of real C++ is mostly mangled library machinery: _Rb_tree walks, __shared_count
traffic, raw hex where symbols are missing. An expert recognizes those on sight; this table
is that recognition, written down. Facts only: each fingerprint says where the time went
and names the rewrite families known for that pattern. Choosing one stays with the caller.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cpp_analysis_mcp.models import Fingerprint, Hotspot

# below this share a pattern is background noise, not a lead worth chasing
THRESHOLD_PCT = 5.0

# sample counts that separate a coarse ranking from a firm one
COARSE_SAMPLES = 300
FIRM_SAMPLES = 3000

HEX_NAME = re.compile(r"^0x[0-9a-fA-F]+$")

NEXT_STEP = (
    "pick a candidate, write 2 or 3 whole-program variants, and race them with "
    "benchmark_variants"
)


@dataclass(frozen=True, slots=True)
class Rule:
    """One recognizable pattern: its markers in symbol names, and its rewrite families."""

    category: str
    statement: str
    markers: tuple[str, ...]
    candidates: tuple[str, ...] = ()


# claimed by the first rule that marks it. Containers go first on purpose: a tree or
# list templated over shared_ptr carries "shared_ptr" in its mangled name, and those
# rows belong to the container, not to refcounting.
RULES: tuple[Rule, ...] = (
    Rule(
        category="map-machinery",
        statement="{pct}% of self time inside std::map/std::set tree machinery",
        markers=("_Rb_tree", "stl_tree"),
        candidates=(
            "a flat sorted container with contiguous storage",
            "batch the operations and process them in key order",
        ),
    ),
    Rule(
        category="hash-machinery",
        statement="{pct}% of self time inside hash table machinery",
        markers=("_Hashtable", "_Hash_node", "unordered_map", "unordered_set"),
        candidates=(
            "an open-addressing flat map",
            "reserve the expected size up front",
        ),
    ),
    Rule(
        category="list-chasing",
        statement="{pct}% of self time walking linked list nodes",
        markers=("_List_node", "_List_base", "std::__cxx11::list"),
        candidates=("contiguous storage instead of a linked list",),
    ),
    Rule(
        category="memory-shifting",
        statement="{pct}% of self time shifting or regrowing contiguous storage",
        markers=("_M_realloc", "memmove", "memcpy"),
        candidates=(
            "reserve capacity up front",
            "append then sort once instead of inserting in the middle",
            "a container that does not shift on insert",
        ),
    ),
    Rule(
        category="allocation",
        statement="{pct}% of self time allocating memory, kernel page faults included",
        markers=("operator new", "_int_malloc", "malloc", "mm_fault", "page_fault", "anonymous_page"),
        candidates=(
            "a pool or arena for the hot objects",
            "store objects by value inside their container",
            "reserve containers before the loop",
        ),
    ),
    Rule(
        category="refcounting",
        statement="{pct}% of self time in shared_ptr reference counting",
        markers=("_Sp_counted", "__shared_count"),
        candidates=(
            "unique ownership or plain values where sharing is not real",
            "pass references instead of copying the pointer",
        ),
    ),
    Rule(
        category="rng",
        statement="{pct}% of self time generating random numbers, likely the workload itself",
        markers=("mersenne_twister",),
    ),
)

UNRESOLVED = Rule(
    category="unresolved",
    statement=(
        "{pct}% of self time in symbols with no name, usually library code without debug "
        "info; the named rows still carry the caller's source lines"
    ),
    markers=(),
)


def read(hotspots: Sequence[Hotspot]) -> tuple[Fingerprint, ...]:
    """Sum self time per pattern and report the ones above the noise floor, largest first."""
    shares: dict[str, float] = {}
    for spot in hotspots:
        rule = _claim(spot.function)
        if rule is not None:
            shares[rule.category] = shares.get(rule.category, 0.0) + spot.self_pct

    by_category = {rule.category: rule for rule in (*RULES, UNRESOLVED)}
    found = (
        Fingerprint(
            category=category,
            share_pct=round(share, 1),
            statement=by_category[category].statement.format(pct=round(share, 1)),
            candidates=by_category[category].candidates,
        )
        for category, share in shares.items()
        if share >= THRESHOLD_PCT
    )
    return tuple(sorted(found, key=lambda mark: mark.share_pct, reverse=True))


def confidence(samples: int) -> str:
    if samples < COARSE_SAMPLES:
        return f"{samples} samples is a coarse ranking; a longer workload would firm it up"
    if samples < FIRM_SAMPLES:
        return f"{samples} samples; treat differences of a few points as noise"
    return f"{samples} samples"


def next_step(found: Sequence[Fingerprint]) -> str | None:
    """The breadcrumb: only when some pattern actually has rewrites worth racing."""
    if any(mark.candidates for mark in found):
        return NEXT_STEP
    return None


def _claim(function: str) -> Rule | None:
    if HEX_NAME.match(function):
        return UNRESOLVED
    for rule in RULES:
        if any(marker in function for marker in rule.markers):
            return rule
    return None
