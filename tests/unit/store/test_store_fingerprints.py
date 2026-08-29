"""Prove fingerprints hold identity still while everything around a finding moves.

An inserted line, a reformat, or a moved block must not change who a finding is, or the
diff against main reports false "new" findings from a one-line edit. The other direction
matters too: identical lines are still two findings, and adjacent fields can't collide.
"""

from __future__ import annotations

import hashlib
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


def test_a_locationless_finding_ignores_whatever_text_is_passed() -> None:
    # the spec: no location means empty path AND empty text, through every entry point.
    # a caller handing text to a locationless finding must not mint a second identity
    homeless = Finding(
        id="build-0001",
        tool="cmake",
        severity=Severity.ERROR,
        category="configure-failed",
        message="generator not found",
    )

    with_text = fingerprint(homeless, "stray text from a confused caller", 0)
    without = fingerprint(homeless, "", 0)

    assert with_text.fingerprint == without.fingerprint


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


def test_ten_thousand_findings_fingerprint_in_under_a_second() -> None:
    """The latency gate: identity must stay noise next to the analyzers it serves.

    The bound is generous on purpose (a Windows dev box measured 0.14s where the Mac it
    was calibrated on sat well under 0.1): its one job is catching a quadratic
    regression, which at this size overshoots a full second by an order of magnitude.
    """
    findings = tuple(
        a_finding(file=f"src/file_{index % 200}.cpp", line=index) for index in range(10_000)
    )

    started = time.perf_counter()
    stamped = fingerprint_batch(findings, lambda _file, line: f"call_{line}(std::move(x));")
    elapsed = time.perf_counter() - started

    assert len(stamped) == 10_000
    assert elapsed < 1.0, f"fingerprint_batch took {elapsed:.3f}s for 10k findings"


def test_batch_hashes_the_canonical_path_when_one_is_given() -> None:
    """The identity seam: the hash sees the canonical spelling, the finding keeps the
    tool's own. Without this, a snippet's random scratch directory is part of who
    its findings are, and the same snippet is a different finding every call."""
    printed = "C:\\scratch\\tmp1a2b\\snippet.cpp"

    (stamped,) = fingerprint_batch(
        (a_finding(file=printed),),
        source_where({(printed, 40): LINE_TEXT}),
        canonical=lambda path: "snippet.cpp",
    )

    assert stamped.fingerprint == compute_fingerprint(RULE, "snippet.cpp", LINE_TEXT, 0)
    assert stamped.location is not None
    assert stamped.location.file == printed


def test_two_spellings_of_one_file_share_identity_under_a_canonical() -> None:
    # the occurrence rank must group on the canonical path too, or two checkouts'
    # spellings of the same line would rank -- and so fingerprint -- independently
    here = "C:\\checkout-one\\src\\a.cpp"
    there = "/home/ci/checkout-two/src/a.cpp"

    stamped = fingerprint_batch(
        (a_finding(file=here), a_finding(file=there)),
        source_where({(here, 40): LINE_TEXT, (there, 40): LINE_TEXT}),
        canonical=lambda path: "src/a.cpp",
    )

    assert stamped[0].fingerprint == stamped[1].fingerprint


# ---------------------------------------------------------------- the normative spec


def spec_literal_fingerprint(rule: str, path: str, text: str, index: int) -> str:
    """ADR-0002's normative encoding, reimplemented from the document alone.

    Written against the prose, not the production code: each canonicalized field as
    UTF-8, prefixed with the ASCII decimal byte length and a colon, concatenated,
    SHA-256, lowercase hex, first sixteen characters. If this and compute_fingerprint
    ever disagree, one of them changed scheme without saying so.
    """
    normalized_path = path.replace("\\", "/").removeprefix("./")
    stripped = "".join(text.split())
    blob = b""
    for part in (rule, normalized_path, stripped, str(index)):
        encoded = part.encode("utf-8")
        blob += str(len(encoded)).encode("ascii") + b":" + encoded
    return hashlib.sha256(blob).hexdigest()[:16]


SPEC_CASES = (
    ("bugprone-use-after-move", "src/order_book.cpp", "    process(std::move(order));", 0),
    ("bugprone-use-after-move", "src\\order_book.cpp", "process( std::move( order ) );", 0),
    ("data-race", "src/feed.cpp", "balance += amt;", 1),
    ("configure-failed", "", "", 0),
    # non-ASCII in rule-adjacent text and path, with a no-break space (U+00A0) in the
    # source line: the length prefixes count UTF-8 bytes and the stripper speaks Unicode,
    # and an implementation counting characters or ASCII whitespace fails exactly here
    (
        "bugprone-suspicious-include",
        "src/misc/h\u00e9ader_users.cpp",
        'auto\u00a0name = "\u03a9";',
        0,
    ),
)


def test_production_matches_the_spec_literal_reimplementation() -> None:
    for case in SPEC_CASES:
        assert compute_fingerprint(*case) == spec_literal_fingerprint(*case), case


def test_known_answer_digests_are_pinned_forever() -> None:
    """The ADR's worked example and companions, as constants.

    These hex strings appear in ADR-0002 and here, nowhere derived. A synchronized
    change to the spec and the implementation still breaks this test -- which is the
    point: moving these digests is a scheme bump, never a refactor.
    """
    expected = (
        "e56adf7bdc0bf0a3",
        "e56adf7bdc0bf0a3",
        "e07f28bdd9615329",
        "46532caa118af9be",
        "5ff61dcafa372f42",
    )

    assert tuple(compute_fingerprint(*case) for case in SPEC_CASES) == expected
