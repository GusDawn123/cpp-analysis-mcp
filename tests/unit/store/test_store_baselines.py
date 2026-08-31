"""A baseline is trusted only while the world that produced it holds still: any drift --
a new compiler, changed flags, edited config, a scheme bump -- reads as "no baseline"
rather than a wrong subtraction that would hide real findings.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from cpp_analysis_mcp.store.baselines import Baseline, load, save
from cpp_analysis_mcp.store.models import Finding, Location, Severity
from cpp_analysis_mcp.store.store import FindingStore

REF = "main"
SCHEME = 1

PRINTS = frozenset({"e56adf7bdc0bf0a3", "e07f28bdd9615329"})


def a_key(**overrides: str) -> dict[str, str]:
    """The architecture-v2 invalidation list, as named facts."""
    key = {
        "compiler": "clang 21.0.0 at /usr/bin/clang++",
        "flags": "sha256:1f2e3d4c5b6a7988",
        "config": "sha256:9a8b7c6d5e4f3211",
        "clang-tidy": "19.1.0 at /usr/bin/clang-tidy",
    }
    key.update(overrides)
    return key


def a_baseline(ref: str = REF, prints: frozenset[str] = PRINTS, **overrides: str) -> Baseline:
    return Baseline(ref=ref, fingerprints=prints, scheme=SCHEME, key=a_key(**overrides))


def test_a_saved_baseline_loads_back_whole(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline())

    loaded = load(tmp_path, root, ref=REF, scheme=SCHEME, key=a_key())

    assert loaded == a_baseline()
    assert loaded is not None
    assert loaded.fingerprints == PRINTS


def test_no_baseline_reads_as_none(tmp_path: Path) -> None:
    assert load(tmp_path, tmp_path / "proj", ref=REF, scheme=SCHEME, key=a_key()) is None


def test_a_changed_compiler_retires_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline())

    stale = load(
        tmp_path, root, ref=REF, scheme=SCHEME, key=a_key(compiler="clang 22.0.0 at /usr/bin")
    )

    assert stale is None


def test_changed_compile_flags_retire_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline())

    assert load(tmp_path, root, ref=REF, scheme=SCHEME, key=a_key(flags="sha256:other")) is None


def test_a_changed_config_retires_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline())

    assert load(tmp_path, root, ref=REF, scheme=SCHEME, key=a_key(config="sha256:edited")) is None


def test_a_changed_tool_retires_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline())

    stale = load(
        tmp_path,
        root,
        ref=REF,
        scheme=SCHEME,
        key=a_key(**{"clang-tidy": "20.0.0 at /usr/bin/clang-tidy"}),
    )

    assert stale is None


def test_a_fact_the_baseline_never_recorded_retires_it(tmp_path: Path) -> None:
    # the key is compared whole: a world that started tracking a new fact cannot
    # vouch for a baseline that never saw it
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline())
    wider = a_key() | {"cppcheck": "2.14"}

    assert load(tmp_path, root, ref=REF, scheme=SCHEME, key=wider) is None


def test_a_scheme_bump_retires_the_baseline(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline())

    assert load(tmp_path, root, ref=REF, scheme=SCHEME + 1, key=a_key()) is None


def test_each_ref_keeps_its_own_baseline(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    on_main = a_baseline(ref="main", prints=frozenset({"aaaaaaaaaaaaaaaa"}))
    on_feature = a_baseline(ref="feat/x", prints=frozenset({"bbbbbbbbbbbbbbbb"}))
    save(tmp_path, root, on_main)
    save(tmp_path, root, on_feature)

    assert load(tmp_path, root, ref="main", scheme=SCHEME, key=a_key()) == on_main
    assert load(tmp_path, root, ref="feat/x", scheme=SCHEME, key=a_key()) == on_feature


def test_two_projects_do_not_collide(tmp_path: Path) -> None:
    ours = a_baseline(prints=frozenset({"aaaaaaaaaaaaaaaa"}))
    theirs = a_baseline(prints=frozenset({"bbbbbbbbbbbbbbbb"}))
    save(tmp_path, tmp_path / "ours", ours)
    save(tmp_path, tmp_path / "theirs", theirs)

    assert load(tmp_path, tmp_path / "ours", ref=REF, scheme=SCHEME, key=a_key()) == ours
    assert load(tmp_path, tmp_path / "theirs", ref=REF, scheme=SCHEME, key=a_key()) == theirs


def test_saving_again_replaces_the_old_baseline(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    save(tmp_path, root, a_baseline(prints=frozenset({"aaaaaaaaaaaaaaaa"})))
    fresher = a_baseline(prints=frozenset({"bbbbbbbbbbbbbbbb"}))
    save(tmp_path, root, fresher)

    assert load(tmp_path, root, ref=REF, scheme=SCHEME, key=a_key()) == fresher


def test_a_corrupt_file_reads_as_none(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    path = save(tmp_path, root, a_baseline())
    path.write_text("{not json", encoding="utf-8")

    assert load(tmp_path, root, ref=REF, scheme=SCHEME, key=a_key()) is None


def test_ten_thousand_identities_round_trip_in_under_a_second(tmp_path: Path) -> None:
    """The latency gate: a baseline load runs on every review call."""
    root = tmp_path / "proj"
    prints = frozenset(f"{index:016x}" for index in range(10_000))

    started = time.perf_counter()
    save(tmp_path, root, a_baseline(prints=prints))
    loaded = load(tmp_path, root, ref=REF, scheme=SCHEME, key=a_key())
    elapsed = time.perf_counter() - started

    assert loaded is not None
    assert len(loaded.fingerprints) == 10_000
    assert elapsed < 1.0, f"a 10k-identity round trip took {elapsed:.3f}s"


# ------------------------------------------------------------- the gate, end to end


LINE = "    process(std::move(order));"
OTHER_LINE = "int y = compute();"


def source_where(mapping: dict[tuple[str, int], str]) -> Callable[[str, int], str]:
    def read_line(file: str, line: int) -> str:
        return mapping.get((file, line), "")

    return read_line


def a_reader() -> Callable[[str, int], str]:
    return source_where({("src/order_book.cpp", 40): LINE, ("src/order_book.cpp", 90): OTHER_LINE})


def a_finding(line: int, rule: str = "bugprone-use-after-move") -> Finding:
    return Finding(
        id=f"tidy-{line:04d}",
        tool="clang-tidy",
        severity=Severity.WARNING,
        category=rule,
        message="'order' used after it was moved",
        location=Location(file="src/order_book.cpp", line=line),
    )


def test_identities_are_the_stores_unsuppressed_fingerprints() -> None:
    store = FindingStore()
    store.ingest([a_finding(40)], a_reader())
    (finding,) = store.findings()
    store.suppress([finding.fingerprint])

    assert store.identities() == frozenset()
    assert store.identities(include_suppressed=True) == frozenset({finding.fingerprint})


def test_new_against_reports_only_what_the_baseline_never_saw() -> None:
    before = FindingStore()
    before.ingest([a_finding(40)], a_reader())
    after = FindingStore()
    after.ingest([a_finding(40), a_finding(90, rule="misc-unused")], a_reader())

    fresh = after.new_against(before.identities())

    assert [finding.category for finding in fresh] == ["misc-unused"]


def test_new_since_and_new_against_tell_one_story() -> None:
    before = FindingStore()
    before.ingest([a_finding(40)], a_reader())
    after = FindingStore()
    after.ingest([a_finding(40), a_finding(90, rule="misc-unused")], a_reader())

    assert after.new_since(before) == after.new_against(before.identities(include_suppressed=True))


def test_the_review_gate_end_to_end(tmp_path: Path) -> None:
    """Run once and remember; change the code and run again; only the new finding
    survives the subtraction."""
    root = tmp_path / "proj"
    first = FindingStore()
    first.ingest([a_finding(40)], a_reader())
    save(
        tmp_path,
        root,
        Baseline(ref=REF, fingerprints=first.identities(), scheme=SCHEME, key=a_key()),
    )

    second = FindingStore()
    second.ingest([a_finding(40), a_finding(90, rule="misc-unused")], a_reader())
    remembered = load(tmp_path, root, ref=REF, scheme=SCHEME, key=a_key())

    assert remembered is not None
    fresh = second.new_against(remembered.fingerprints)
    assert [finding.category for finding in fresh] == ["misc-unused"]
