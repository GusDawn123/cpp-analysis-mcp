"""Prove fingerprints hold identity still while everything around a finding moves.

Each invariance here is a way a baseline would otherwise lie: a line inserted above,
a reformat, a block moved wholesale -- none of them may change who a finding is, or the
diff against main reports dozens of "new" findings from a one-line edit. The other
direction matters as much: two identical flagged lines are two findings, different
rules are different findings, and adjacent fields must never trade characters into a
manufactured collision.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace

from cpp_analysis_mcp.store.fingerprints import (
    SCHEME_VERSION,
    compute_fingerprint,
    fingerprint,
    fingerprint_batch,
)
from cpp_analysis_mcp.store.models import Finding, Location, Severity

RULE = "bugprone-use-after-move"
LINE_TEXT = "    process(std::move(order));"


def a_finding(file: str = "src/order_book.cpp", line: int = 40, rule: str = RULE) -> Finding:
    return Finding(
        id=f"tidy-{line:04d}",
        tool="clang-tidy",
        severity=Severity.WARNING,
        category=rule,
        message="'order' used after it was moved",
        location=Location(file=file, line=line),
    )


def source_where(mapping: dict[tuple[str, int], str]) -> Callable[[str, int], str]:
    """A read_line that serves the given (file, line) -> text table, blank elsewhere."""

    def read_line(file: str, line: int) -> str:
        return mapping.get((file, line), "")

    return read_line


# ---------------------------------------------------------------- the primitive


def test_digest_is_sixteen_hex_characters() -> None:
    digest = compute_fingerprint(RULE, "src/a.cpp", LINE_TEXT, 0)

    assert len(digest) == 16
    assert set(digest) <= set("0123456789abcdef")


def test_line_numbers_never_enter_the_hash() -> None:
    # the same flagged text is the same finding at line 40 and at line 41 -- an insertion
    # above it must not mint a new identity
    at_40 = fingerprint(a_finding(line=40), LINE_TEXT, 0)
    at_41 = fingerprint(a_finding(line=41), LINE_TEXT, 0)

    assert at_40.fingerprint == at_41.fingerprint


def test_reformatting_leaves_identity_alone() -> None:
    variants = (
        "process(std::move(order));",
        "    process(std::move(order));",
        "\tprocess( std::move( order ) );",
        "process (std::move(order)) ;",
    )

    digests = {compute_fingerprint(RULE, "src/a.cpp", text, 0) for text in variants}

    assert len(digests) == 1


def test_different_rules_on_one_line_are_different_findings() -> None:
    use_after_move = compute_fingerprint("bugprone-use-after-move", "src/a.cpp", LINE_TEXT, 0)
    dangling = compute_fingerprint("bugprone-dangling-handle", "src/a.cpp", LINE_TEXT, 0)

    assert use_after_move != dangling


def test_windows_and_container_paths_agree() -> None:
    from_windows = compute_fingerprint(RULE, "src\\core\\a.cpp", LINE_TEXT, 0)
    from_container = compute_fingerprint(RULE, "src/core/a.cpp", LINE_TEXT, 0)
    dot_relative = compute_fingerprint(RULE, "./src/core/a.cpp", LINE_TEXT, 0)

    assert from_windows == from_container == dot_relative


def test_adjacent_fields_cannot_trade_characters() -> None:
    # without length prefixes, rule "ab" + path "c" and rule "a" + path "bc" would hash
    # the same bytes -- a collision by construction rather than by SHA-256
    assert compute_fingerprint("ab", "c", "x", 0) != compute_fingerprint("a", "bc", "x", 0)
    assert compute_fingerprint("r", "p1", "x", 0) != compute_fingerprint("r", "p", "1x", 0)


# ---------------------------------------------------------------- single findings


def test_fingerprint_stamps_the_scheme_and_keeps_the_original() -> None:
    original = a_finding()
    stamped = fingerprint(original, LINE_TEXT, 0)

    assert stamped.fingerprint != ""
    assert stamped.fingerprint_scheme == SCHEME_VERSION
    assert original.fingerprint == ""  # frozen input untouched; replace() returned a copy
    assert replace(stamped, fingerprint="", fingerprint_scheme=0) == original


def test_a_finding_with_no_location_still_gets_an_identity() -> None:
    homeless = Finding(
        id="build-0001",
        tool="cmake",
        severity=Severity.ERROR,
        category="configure-failed",
        message="generator not found",
    )

    stamped = fingerprint(homeless, "", 0)
    again = fingerprint(homeless, "", 0)

    assert stamped.fingerprint != ""
    assert stamped.fingerprint == again.fingerprint


# ---------------------------------------------------------------- whole runs


def test_batch_survives_an_insertion_above_every_finding() -> None:
    before = fingerprint_batch(
        (a_finding(line=40),),
        source_where({("src/order_book.cpp", 40): LINE_TEXT}),
    )
    after = fingerprint_batch(
        (a_finding(line=45),),
        source_where({("src/order_book.cpp", 45): LINE_TEXT}),
    )

    assert before[0].fingerprint == after[0].fingerprint


def test_identical_flagged_lines_are_two_findings_wherever_the_block_sits() -> None:
    twice = fingerprint_batch(
        (a_finding(line=10), a_finding(line=90)),
        source_where(
            {("src/order_book.cpp", 10): LINE_TEXT, ("src/order_book.cpp", 90): LINE_TEXT}
        ),
    )
    moved = fingerprint_batch(
        (a_finding(line=30), a_finding(line=110)),
        source_where(
            {("src/order_book.cpp", 30): LINE_TEXT, ("src/order_book.cpp", 110): LINE_TEXT}
        ),
    )

    assert twice[0].fingerprint != twice[1].fingerprint
    # the whole block moved down twenty lines; first is still first, second still second
    assert twice[0].fingerprint == moved[0].fingerprint
    assert twice[1].fingerprint == moved[1].fingerprint


def test_two_reports_of_one_line_share_one_identity() -> None:
    # the dense rank gives duplicates of the same line the same index, so the store's
    # dedup can recognize them as one finding reported twice
    duplicated = fingerprint_batch(
        (a_finding(line=40), a_finding(line=40)),
        source_where({("src/order_book.cpp", 40): LINE_TEXT}),
    )

    assert duplicated[0].fingerprint == duplicated[1].fingerprint


def test_batch_preserves_order_and_count() -> None:
    findings = tuple(a_finding(line=line) for line in (90, 10, 50))

    stamped = fingerprint_batch(
        findings, source_where({("src/order_book.cpp", line): LINE_TEXT for line in (90, 10, 50)})
    )

    assert len(stamped) == 3
    assert [f.id for f in stamped] == ["tidy-0090", "tidy-0010", "tidy-0050"]


def test_spacing_only_token_differences_share_identity_by_design() -> None:
    """The accepted boundary of scheme 1, pinned so a change to it is a decision.

    Stripping every whitespace run merges `a + ++b` with `a++ + b`. The alternative --
    collapsing runs to one space -- would keep those apart but change every finding's
    identity on each reformat, and reformats vastly outnumber adjacent-operator edits.
    If this assertion ever needs to flip, that is a new scheme: bump SCHEME_VERSION
    rather than editing the stripping (see the module docstring and ADR-0002).
    """
    spaced = compute_fingerprint(RULE, "src/a.cpp", "result = a + ++b;", 0)
    munched = compute_fingerprint(RULE, "src/a.cpp", "result = a++ + b;", 0)

    assert spaced == munched


def test_inserting_a_duplicate_above_grows_the_identity_set_by_exactly_one() -> None:
    """Attribution among identical duplicates may rotate; the set difference may not.

    With duplicates at lines 50 and 90, inserting a third identical line at 10 reranks
    the survivors -- but baseline subtraction works on fingerprint sets, and the set
    must grow by exactly the inserted finding for the review gate to be honest.
    """
    text = {("src/order_book.cpp", line): LINE_TEXT for line in (10, 50, 90)}
    before = fingerprint_batch(
        (a_finding(line=50), a_finding(line=90)),
        source_where(text),
    )
    after = fingerprint_batch(
        (a_finding(line=10), a_finding(line=50), a_finding(line=90)),
        source_where(text),
    )

    old_identities = {finding.fingerprint for finding in before}
    new_identities = {finding.fingerprint for finding in after}

    assert old_identities < new_identities
    assert len(new_identities - old_identities) == 1


def test_ten_thousand_findings_fingerprint_in_under_a_tenth_of_a_second() -> None:
    """The latency gate: identity must stay noise next to the analyzers it serves."""
    findings = tuple(
        a_finding(file=f"src/file_{index % 200}.cpp", line=index) for index in range(10_000)
    )

    started = time.perf_counter()
    stamped = fingerprint_batch(findings, lambda _file, line: f"call_{line}(std::move(x));")
    elapsed = time.perf_counter() - started

    assert len(stamped) == 10_000
    assert elapsed < 0.1, f"fingerprint_batch took {elapsed:.3f}s for 10k findings"
