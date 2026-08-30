"""Pin the danger tiers: what witnessed a defect decides more than what it is called.

The table is the opinion, so these read it the way a report does -- a runtime tool's
word outranks a linter's, the first matching row wins, and a category nobody has an
opinion about says so instead of being guessed at.
"""

from __future__ import annotations

from cpp_analysis_mcp.store.models import Finding, Severity
from cpp_analysis_mcp.store.triage import STATIC_TIERS, WITNESSED, Tier, tier_for


def a_finding(*, tool: str = "clang-tidy", category: str = "bugprone-use-after-move") -> Finding:
    return Finding(
        id="f-1",
        tool=tool,
        severity=Severity.WARNING,
        category=category,
        message="something worth hearing",
    )


def sample_of(pattern: str) -> str:
    """A category the pattern matches: globs stand in for the part they do not name."""
    if pattern.startswith("*"):
        return f"x{pattern[1:]}"
    if pattern.endswith("*"):
        return f"{pattern[:-1]}x"
    return pattern


def test_the_tiers_read_from_most_dangerous_to_least() -> None:
    # reports count in this order, so the declaration order is the published shape
    assert [tier.value for tier in Tier] == ["critical", "major", "minor", "style", "unrated"]


def test_a_defect_a_sanitizer_watched_happen_is_critical() -> None:
    for tool in ("asan", "tsan", "ubsan"):
        assert tier_for(a_finding(tool=tool, category="heap-use-after-free")) is Tier.CRITICAL


def test_a_witnessed_leak_is_major_rather_than_critical() -> None:
    assert tier_for(a_finding(tool="lsan", category="memory-leak")) is Tier.MAJOR


def test_static_analysis_never_reaches_critical() -> None:
    """Witnessed beats suspected: a linter matches source text, it watches nothing run."""
    assert Tier.CRITICAL not in {tier for _, tier in STATIC_TIERS}
    assert tier_for(a_finding(category="bugprone-use-after-move")) is Tier.MAJOR


def test_what_witnessed_a_finding_outranks_what_the_category_says() -> None:
    # the same category from a linter is style; from a sanitizer it was observed happening
    assert tier_for(a_finding(category="modernize-use-nullptr")) is Tier.STYLE
    assert tier_for(a_finding(tool="asan", category="modernize-use-nullptr")) is Tier.CRITICAL


def test_the_first_matching_row_wins_so_the_specific_beats_the_general() -> None:
    assert tier_for(a_finding(category="clang-analyzer-cplusplus.NewDelete")) is Tier.MAJOR
    assert tier_for(a_finding(category="clang-analyzer-core.NullDereference")) is Tier.MINOR
    assert tier_for(a_finding(category="cppcoreguidelines-pro-bounds-constant-array-index")) is (
        Tier.MAJOR
    )
    assert tier_for(a_finding(category="cppcoreguidelines-owning-memory")) is Tier.STYLE


def test_no_row_is_shadowed_by_the_one_above_it() -> None:
    """Order is the whole mechanism; a row an earlier pattern already claims is dead."""
    reached = {sample_of(pattern): tier for pattern, tier in STATIC_TIERS}

    unreachable = [
        (category, expected)
        for category, expected in reached.items()
        if tier_for(a_finding(category=category)) is not expected
    ]
    assert not unreachable, f"rows an earlier pattern already claims: {unreachable}"


def test_a_check_that_died_is_major_so_nothing_hides_behind_it() -> None:
    for category in ("clang-tidy-failed", "thread-safety-failed", "compile-failed"):
        assert tier_for(a_finding(category=category)) is Tier.MAJOR


def test_a_detector_that_was_not_watching_is_major_too() -> None:
    """The same silence as a dead check: counted, and never crowded out of the detail."""
    assert tier_for(a_finding(category="tool-unavailable")) is Tier.MAJOR


def test_thread_safety_findings_are_major() -> None:
    assert tier_for(a_finding(tool="compiler", category="thread-safety-analysis")) is Tier.MAJOR
    assert tier_for(a_finding(tool="compiler", category="thread-safety")) is Tier.MAJOR


def test_the_opinions_that_are_about_how_code_looks_are_style() -> None:
    for category in (
        "modernize-use-nullptr",
        "readability-identifier-naming",
        "cppcoreguidelines-avoid-magic-numbers",
    ):
        assert tier_for(a_finding(category=category)) is Tier.STYLE


def test_cost_and_correctness_opinions_land_in_minor() -> None:
    for category in (
        "performance-unnecessary-value-param",
        "cppcoreguidelines-init-variables",
        "clang-diagnostic-unused-variable",
    ):
        assert tier_for(a_finding(category=category)) is Tier.MINOR


def test_a_category_nobody_has_rated_says_so_rather_than_being_guessed() -> None:
    assert tier_for(a_finding(category="some-check-invented-tomorrow")) is Tier.UNRATED
    assert tier_for(a_finding(tool="cppcheck", category="analysis-note")) is Tier.UNRATED


def test_every_witnessing_tool_names_a_parser_this_package_ships() -> None:
    # the tool field is written by the parsers, so a typo here silently rates nothing
    assert set(WITNESSED) == {"asan", "tsan", "ubsan", "lsan"}
