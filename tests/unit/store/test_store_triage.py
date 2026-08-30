"""Pin the danger tiers and the verify-with hints: both are tables, read the same way.

The table is the opinion, so these read it the way a report does -- a runtime tool's
word outranks a linter's, the first matching row wins, and a category nobody has an
opinion about says so instead of being guessed at.
"""

from __future__ import annotations

from cpp_analysis_mcp.store.models import Finding, Severity
from cpp_analysis_mcp.store.triage import (
    STATIC_TIERS,
    WITNESSED,
    WOULD_WITNESS,
    Tier,
    tier_for,
    verify_with,
)


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


# ------------------------------------------------- which runtime check would witness it


def test_the_defects_a_memory_sanitizer_would_watch_happen_name_asan() -> None:
    for category in (
        "bugprone-dangling-handle",
        "clang-analyzer-cplusplus.NewDelete",
    ):
        assert verify_with(a_finding(category=category)) == "asan"


def test_use_after_move_names_no_tool_because_nothing_runtime_traps_it() -> None:
    # a moved-from object is alive and reading it is legal C++; sending anyone to ASan
    # would spend minutes on a run that reports nothing
    assert verify_with(a_finding(category="bugprone-use-after-move")) is None


def test_memory_still_held_at_exit_names_the_leak_detector_not_the_memory_one() -> None:
    """The specific row sits above the family it belongs to, or every leak reads as asan."""
    assert verify_with(a_finding(category="clang-analyzer-cplusplus.NewDeleteLeaks")) == "lsan"


def test_the_lock_and_concurrency_opinions_name_tsan() -> None:
    for category in ("thread-safety-analysis", "concurrency-mt-unsafe"):
        assert verify_with(a_finding(category=category)) == "tsan"


def test_a_cost_opinion_names_the_profiler_because_only_measurement_ranks_it() -> None:
    assert verify_with(a_finding(category="performance-unnecessary-value-param")) == "profile"


def test_a_category_no_runtime_check_could_witness_gets_no_hint() -> None:
    for category in ("modernize-use-nullptr", "some-check-invented-tomorrow", "analysis-note"):
        assert verify_with(a_finding(category=category)) is None


def test_the_hints_read_through_the_same_matcher_the_tiers_do() -> None:
    """One glob dialect, one table format: a row here behaves exactly like a tier row."""
    reached = {sample_of(pattern): analysis for pattern, analysis in WOULD_WITNESS}

    unreachable = [
        (category, expected)
        for category, expected in reached.items()
        if verify_with(a_finding(category=category)) != expected
    ]
    assert not unreachable, f"rows an earlier pattern already claims: {unreachable}"


def test_a_check_that_died_names_no_runtime_tool_to_go_ask_instead() -> None:
    """A dead check is not a defect: the file did not compile, and it would not compile
    under a sanitizer either. Without the guard row, thread-safety-failed reads as tsan."""
    for category in ("thread-safety-failed", "clang-tidy-failed", "tool-unavailable"):
        assert verify_with(a_finding(category=category)) is None


def test_every_named_verifier_is_something_this_server_can_actually_run() -> None:
    # a hint naming a tool with no tool behind it costs the reader a wasted call
    named = {analysis for _, analysis in WOULD_WITNESS if analysis is not None}
    assert named <= {"asan", "tsan", "lsan", "ubsan", "profile"}
