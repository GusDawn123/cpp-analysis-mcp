"""Pin what the fingerprint table recognizes and what it refuses to guess about.

The symbol names here are real: they come from profiling a deliberately naive order book
(std::map book sides, shared_ptr per order, a hand-sorted vector) through the WSL bridge.
If the table stops recognizing them, the report goes back to raw mangled names.
"""

from __future__ import annotations

from cpp_analysis_mcp import fingerprints
from cpp_analysis_mcp.store.models import Hotspot

RB_TREE_EMPLACE = (
    "std::_Rb_tree_iterator<std::pair<int const, std::__cxx11::list<std::shared_ptr<Order>"
    " > > > std::_Rb_tree<int, std::pair<int const, ...> >::_M_emplace_hint_unique(...)"
)
SHARED_COUNT_DTOR = "std::__shared_count<(__gnu_cxx::_Lock_policy)2>::~__shared_count()"
VECTOR_INSERT = (
    "std::vector<unsigned long, std::allocator<unsigned long> >"
    "::_M_realloc_insert(__gnu_cxx::__normal_iterator<...>, unsigned long const&)"
)
TWISTER = "std::mersenne_twister_engine<unsigned long, 64ul, 312ul, ...>::operator()()"


def spot(function: str, self_pct: float) -> Hotspot:
    return Hotspot(function=function, self_pct=self_pct, total_pct=self_pct)


def categories(found: tuple[object, ...]) -> list[str]:
    return [mark.category for mark in found]  # type: ignore[attr-defined]


def test_the_naive_order_book_profile_is_read_into_named_patterns() -> None:
    found = fingerprints.read(
        [
            spot("0x0000000000197e47", 41.9),
            spot(RB_TREE_EMPLACE, 6.2),
            spot(SHARED_COUNT_DTOR, 9.0),
            spot(VECTOR_INSERT, 7.5),
            spot("operator new(unsigned long)", 5.5),
            spot("Book::add(unsigned long, int, unsigned int, bool)", 12.0),
        ]
    )

    assert categories(found) == [
        "unresolved",
        "refcounting",
        "memory-shifting",
        "map-machinery",
        "allocation",
    ]


def test_shares_sum_across_every_row_of_the_same_pattern() -> None:
    found = fingerprints.read(
        [
            spot(RB_TREE_EMPLACE, 4.0),
            spot("std::_Rb_tree<int, ...>::_M_erase(...)", 3.5),
            spot("stl_tree.h machinery", 2.5),
        ]
    )

    assert categories(found) == ["map-machinery"]
    assert found[0].share_pct == 10.0
    assert "10.0%" in found[0].statement


def test_patterns_below_the_noise_floor_stay_out() -> None:
    found = fingerprints.read([spot(SHARED_COUNT_DTOR, 4.9), spot(RB_TREE_EMPLACE, 5.0)])

    assert categories(found) == ["map-machinery"]


def test_plain_user_code_produces_no_fingerprints() -> None:
    found = fingerprints.read([spot("Book::match()", 60.0), spot("Book::add(unsigned long)", 30.0)])

    assert found == ()
    assert fingerprints.next_step(found) is None


def test_kernel_page_faults_count_as_allocation_pressure() -> None:
    found = fingerprints.read(
        [
            spot("handle_mm_fault", 3.0),
            spot("asm_exc_page_fault", 2.0),
            spot("operator new(unsigned long)", 1.5),
        ]
    )

    assert categories(found) == ["allocation"]
    assert found[0].share_pct == 6.5


def test_actionable_patterns_carry_candidates_and_the_breadcrumb() -> None:
    found = fingerprints.read([spot(VECTOR_INSERT, 40.0)])

    assert found[0].candidates
    assert "race them with" in (fingerprints.next_step(found) or "")


def test_rng_is_named_but_never_actionable() -> None:
    """The workload generator is worth naming so nobody optimizes the harness, and it
    offers no rewrites because there is nothing to fix."""
    found = fingerprints.read([spot(TWISTER, 8.0)])

    assert categories(found) == ["rng"]
    assert found[0].candidates == ()
    assert fingerprints.next_step(found) is None


def test_unresolved_hex_is_grouped_and_explained() -> None:
    found = fingerprints.read([spot("0x0000000000197e47", 41.9), spot("0x00007c4991f97d6a", 7.8)])

    assert categories(found) == ["unresolved"]
    assert found[0].share_pct == 49.7
    assert "no name" in found[0].statement
    assert found[0].candidates == ()


def test_confidence_speaks_in_tiers() -> None:
    coarse = fingerprints.confidence(287)
    middling = fingerprints.confidence(1200)
    firm = fingerprints.confidence(50000)

    assert "coarse" in coarse
    assert "noise" in middling
    assert firm == "50000 samples"


def test_bare_memory_copies_get_no_container_advice() -> None:
    found = fingerprints.read([spot("__memmove_avx_unaligned_erms", 30.0)])

    assert categories(found) == ["bulk-copying"]
    assert found[0].candidates == ()
    assert fingerprints.next_step(found) is None
