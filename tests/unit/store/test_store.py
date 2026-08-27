"""Prove the store answers the review gate's questions and destroys nothing.

The operations under test are the product's spine: duplicates become counts,
cross-tool agreement becomes confirmation instead of noise, `new_since` reports only
what a change introduced, suppression hides without deleting, and ranking spends a
reader's limited budget on variety before repetition. Determinism gets its own
assertions because a gate that orders findings differently on identical inputs
cannot be golden-tested -- or trusted.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from cpp_analysis_mcp.store.models import Finding, Location, Severity
from cpp_analysis_mcp.store.store import FindingStore

LINE_TEXT = "    process(std::move(order));"


def a_finding(
    file: str = "src/order_book.cpp",
    line: int = 40,
    rule: str = "bugprone-use-after-move",
    tool: str = "clang-tidy",
    severity: Severity = Severity.WARNING,
) -> Finding:
    return Finding(
        id=f"{tool}-{line:04d}",
        tool=tool,
        severity=severity,
        category=rule,
        message="'order' used after it was moved",
        location=Location(file=file, line=line),
    )


def same_text_everywhere(_file: str, _line: int) -> str:
    return LINE_TEXT


def a_store(*findings: Finding) -> FindingStore:
    store = FindingStore()
    store.ingest(findings, same_text_everywhere)
    return store


# ---------------------------------------------------------------- ingest


def test_ingest_stamps_identity_on_everything_it_keeps() -> None:
    store = a_store(a_finding())

    (kept,) = store.findings()
    assert kept.fingerprint != ""
    assert kept.fingerprint_scheme > 0


def test_the_same_tool_repeating_itself_becomes_a_count() -> None:
    store = a_store(a_finding(line=40), a_finding(line=40))

    (kept,) = store.findings()
    assert kept.occurrences == 2


def test_a_second_tool_agreeing_becomes_a_confirmation_not_a_second_finding() -> None:
    store = a_store(a_finding(tool="clang-tidy"), a_finding(tool="cppcheck"))

    (kept,) = store.findings()
    assert kept.tool == "clang-tidy"  # first report is the finding of record
    assert [seen.tool for seen in kept.confirmations] == ["cppcheck"]
    assert kept.confirmations[0].finding_id == "cppcheck-0040"


def test_whichever_tool_arrives_first_holds_the_finding() -> None:
    tidy_first = a_store(a_finding(tool="clang-tidy"), a_finding(tool="cppcheck"))
    cppcheck_first = a_store(a_finding(tool="cppcheck"), a_finding(tool="clang-tidy"))

    (from_tidy,) = tidy_first.findings()
    (from_cppcheck,) = cppcheck_first.findings()
    # identity does not depend on arrival order; only the holder does
    assert from_tidy.fingerprint == from_cppcheck.fingerprint
    assert from_tidy.tool == "clang-tidy"
    assert from_cppcheck.tool == "cppcheck"


def test_a_tool_confirms_any_finding_at_most_once() -> None:
    store = a_store(
        a_finding(tool="clang-tidy"),
        a_finding(tool="cppcheck"),
        a_finding(tool="cppcheck"),
    )

    (kept,) = store.findings()
    assert len(kept.confirmations) == 1


# ---------------------------------------------------------------- new_since


def test_new_since_reports_exactly_what_the_baseline_lacks() -> None:
    baseline = a_store(a_finding(line=40), a_finding(file="src/feed.cpp", line=10))
    head = a_store(
        a_finding(line=45),  # moved five lines: same identity, not news
        a_finding(file="src/feed.cpp", line=10),
        a_finding(file="src/cache.cpp", line=7, rule="bugprone-dangling-handle"),
    )

    new = head.new_since(baseline)

    assert [finding.category for finding in new] == ["bugprone-dangling-handle"]


def test_new_since_between_identical_stores_is_empty() -> None:
    baseline = a_store(a_finding(line=40))
    head = a_store(a_finding(line=40))

    assert head.new_since(baseline) == ()


def test_suppressed_findings_are_not_news() -> None:
    baseline = a_store(a_finding(line=40))
    head = a_store(a_finding(line=40), a_finding(file="src/cache.cpp", line=7))

    (newcomer,) = head.new_since(baseline)
    head.suppress([newcomer.fingerprint])

    assert head.new_since(baseline) == ()


# ---------------------------------------------------------------- suppression


def test_suppression_hides_without_deleting() -> None:
    store = a_store(a_finding(line=40))
    (kept,) = store.findings()

    store.suppress([kept.fingerprint])

    assert store.findings() == ()
    assert store.findings(include_suppressed=True) == (kept,)


def test_a_suppressed_finding_keeps_absorbing_reports() -> None:
    store = a_store(a_finding(line=40))
    (kept,) = store.findings()
    store.suppress([kept.fingerprint])

    store.ingest((a_finding(line=40),), same_text_everywhere)

    assert store.findings() == ()
    (hidden,) = store.findings(include_suppressed=True)
    assert hidden.occurrences == 2  # the record stayed alive behind the veil


# ---------------------------------------------------------------- ranking


def test_ranking_puts_errors_before_warnings_before_notes() -> None:
    store = a_store(
        a_finding(line=10, severity=Severity.NOTE, rule="readability-else-after-return"),
        a_finding(line=20, severity=Severity.ERROR, rule="clang-diagnostic-error"),
        a_finding(line=30, severity=Severity.WARNING),
    )

    severities = [finding.severity for finding in store.ranked()]

    assert severities == [Severity.ERROR, Severity.WARNING, Severity.NOTE]


def test_ranking_hears_every_file_before_any_file_repeats() -> None:
    store = a_store(
        a_finding(file="src/a.cpp", line=1, rule="r1"),
        a_finding(file="src/a.cpp", line=2, rule="r2"),
        a_finding(file="src/a.cpp", line=3, rule="r3"),
        a_finding(file="src/b.cpp", line=1, rule="r4"),
        a_finding(file="src/c.cpp", line=1, rule="r5"),
    )

    files = [finding.location.file for finding in store.ranked() if finding.location]

    # one from each place first; a.cpp's pile waits its turn
    assert files == ["src/a.cpp", "src/b.cpp", "src/c.cpp", "src/a.cpp", "src/a.cpp"]


def test_ranking_is_deterministic_for_the_same_ingest_history() -> None:
    def build() -> FindingStore:
        return a_store(
            a_finding(file="src/b.cpp", line=5, severity=Severity.ERROR, rule="r1"),
            a_finding(file="src/a.cpp", line=9, rule="r2"),
            a_finding(file="src/b.cpp", line=7, rule="r3"),
        )

    first, second = build(), build()

    assert first.ranked() == second.ranked()
    assert first.ranked() == first.ranked()


# ---------------------------------------------------------------- latency


def test_new_since_across_two_ten_thousand_finding_stores_is_fast() -> None:
    """The gate's hot path: a key-difference walk, not a comparison of contents.

    Latency assertions ride with the unit suite on purpose -- hot paths are benchmarked
    every increment, not in a job nobody runs. The bound carries ~20x headroom over the
    expected cost so CI noise cannot flake it; if this ever trips, the walk stopped
    being linear, which is exactly the news it exists to deliver.
    """

    def bulk(store: FindingStore, start: int) -> None:
        distinct_text: Callable[[str, int], str] = lambda _file, line: f"stmt_{line};"  # noqa: E731
        run = tuple(
            a_finding(file=f"src/file_{index % 100}.cpp", line=index)
            for index in range(start, start + 10_000)
        )
        store.ingest(run, distinct_text)

    baseline, head = FindingStore(), FindingStore()
    bulk(baseline, 0)
    bulk(head, 1_000)  # nine thousand shared, one thousand new

    started = time.perf_counter()
    new = head.new_since(baseline)
    elapsed = time.perf_counter() - started

    assert len(new) == 1_000
    assert elapsed < 0.05, f"new_since took {elapsed:.3f}s across 10k-finding stores"


def test_the_package_facade_serves_the_store() -> None:
    import cpp_analysis_mcp.store as facade

    assert facade.FindingStore is FindingStore


def test_replace_keeps_identity_fields_intact() -> None:
    # ranked/ingest lean on dataclasses.replace never dropping fields silently
    store = a_store(a_finding())
    (kept,) = store.findings()

    assert replace(kept).fingerprint == kept.fingerprint
