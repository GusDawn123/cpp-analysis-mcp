"""Pin the shape of the shared vocabulary in models.py.

The dataclasses are frozen and slotted on purpose: a Finding passed between layers
must not be edited in place, and a typo like `finding.messge = ...` must fail loudly
rather than attach a new attribute nobody reads.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cpp_analysis_mcp.store.models import (
    SANITIZER_FOR,
    AccessOp,
    Analysis,
    BenchmarkReport,
    BuildFailure,
    BuiltBinary,
    CapabilityStatus,
    Finding,
    Frame,
    Hotspot,
    Location,
    SanitizerKind,
    Severity,
    ThreadAccess,
    VariantResult,
)


def a_location() -> Location:
    return Location(file="data_race.cpp", line=12, column=9)


def a_finding() -> Finding:
    return Finding(
        id="tsan-0001",
        tool="thread-sanitizer",
        severity=Severity.ERROR,
        category="data-race",
        message="data race on counter",
    )


def a_built_binary() -> BuiltBinary:
    return BuiltBinary(
        path=Path("build/data_race_tsan"),
        build_dir=Path("build"),
        sanitizer=SanitizerKind.THREAD,
        runtime_env={"TSAN_OPTIONS": "history_size=7"},
        compile_commands=None,
        warnings=(),
    )


def a_build_failure() -> BuildFailure:
    return BuildFailure(stage="compile", output="a.cpp:1:1: error: expected ';'\n")


def test_enum_values_are_the_strings_tools_use() -> None:
    # SanitizerKind values go straight into -fsanitize=, so they are not free to rename
    assert SanitizerKind.THREAD.value == "thread"
    assert SanitizerKind.ADDRESS.value == "address"
    assert SanitizerKind.UNDEFINED.value == "undefined"
    assert SanitizerKind.LEAK.value == "leak"
    assert Severity.ERROR.value == "error"
    assert Severity.WARNING.value == "warning"
    assert Severity.NOTE.value == "note"
    assert AccessOp.READ.value == "read"
    assert AccessOp.WRITE.value == "write"


def test_enum_members_are_exactly_these() -> None:
    assert [kind.value for kind in SanitizerKind] == ["thread", "address", "undefined", "leak"]
    assert [level.value for level in Severity] == ["error", "warning", "note"]
    assert [op.value for op in AccessOp] == ["read", "write"]


def test_enum_members_are_strings() -> None:
    assert isinstance(SanitizerKind.THREAD, str)
    # str() must give the bare value so it can go straight into -fsanitize= and JSON
    assert str(SanitizerKind.THREAD) == "thread"
    assert f"{Severity.ERROR}" == "error"
    assert SanitizerKind("thread") is SanitizerKind.THREAD


def test_finding_defaults() -> None:
    finding = a_finding()

    assert finding.location is None
    assert finding.symbol is None
    assert finding.allocated_at is None
    assert finding.threads == ()
    assert finding.occurrences == 1


def test_frozen_instances_reject_assignment() -> None:
    location = a_location()
    finding = a_finding()

    with pytest.raises(FrozenInstanceError):
        location.line = 13  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        finding.occurrences = 2  # type: ignore[misc]

    assert location.line == 12
    assert finding.occurrences == 1


def test_instances_reject_unknown_attributes() -> None:
    finding = a_finding()

    # slots refuse the attribute; frozen+slots on 3.11 raises TypeError rather than
    # AttributeError from its generated __setattr__
    with pytest.raises((AttributeError, TypeError)):
        finding.confidence = 0.5  # type: ignore[attr-defined]


def test_instances_use_slots() -> None:
    instances = (
        a_location(),
        Frame(function="bump()"),
        ThreadAccess(thread_id="T1", op=AccessOp.READ, size=4, locks_held=(), frames=()),
        a_finding(),
        CapabilityStatus(available=True),
        a_built_binary(),
        a_build_failure(),
        Hotspot(function="bump()", self_pct=1.0, total_pct=1.0),
    )

    for instance in instances:
        assert not hasattr(instance, "__dict__"), f"{type(instance).__name__} lost its slots"


def test_thread_access_records_the_diagnosis() -> None:
    access = ThreadAccess(
        thread_id="T2",
        op=AccessOp.WRITE,
        size=4,
        locks_held=(),
        frames=(Frame(function="bump()", location=a_location()),),
    )

    assert access.op == "write"
    assert access.locks_held == ()
    assert access.frames[0].location == a_location()


def test_capability_status_defaults() -> None:
    status = CapabilityStatus(available=True)

    assert status.reason is None
    assert status.suggestion is None
    assert status.verified_by is None
    assert status.limitations == ()


def test_analysis_values_are_the_names_the_server_offers() -> None:
    assert [analysis.value for analysis in Analysis] == [
        "tsan",
        "asan",
        "lsan",
        "ubsan",
        "thread-safety",
        "clang-tidy",
        "profile",
    ]
    assert Analysis("thread-safety") is Analysis.THREAD_SAFETY
    assert f"{Analysis.TSAN}" == "tsan"


def test_every_sanitizer_analysis_names_its_sanitizer() -> None:
    assert SANITIZER_FOR == {
        Analysis.TSAN: SanitizerKind.THREAD,
        Analysis.ASAN: SanitizerKind.ADDRESS,
        Analysis.LSAN: SanitizerKind.LEAK,
        Analysis.UBSAN: SanitizerKind.UNDEFINED,
    }
    # every sanitizer is reachable, so no kind is left with no analysis offering it
    assert set(SANITIZER_FOR.values()) == set(SanitizerKind)

    for analysis, kind in SANITIZER_FOR.items():
        assert SANITIZER_FOR[Analysis(analysis.value)] is kind


def test_the_analyses_that_instrument_nothing_have_no_sanitizer() -> None:
    """Two run at compile time and one builds optimized; none takes a -fsanitize= flag.

    The profiler is the one worth stating outright: instrumenting a build would change the
    very thing it is measuring, so it is not a sanitizer that happens to lack a flag.
    """
    assert Analysis.THREAD_SAFETY not in SANITIZER_FOR
    assert Analysis.CLANG_TIDY not in SANITIZER_FOR
    assert Analysis.PROFILE not in SANITIZER_FOR


def test_built_binary_takes_a_plain_dict_for_runtime_env() -> None:
    binary = a_built_binary()

    assert binary.runtime_env["TSAN_OPTIONS"] == "history_size=7"
    assert binary.compile_commands is None
    assert binary.sanitizer == "thread"
    assert binary.warnings == ()


def test_built_binary_runtime_env_rejects_item_assignment() -> None:
    """frozen= stops rebinding the field; the proxy stops editing what is inside it.

    An edit here would be a binary whose environment stopped matching how it was compiled.
    """
    binary = a_built_binary()

    with pytest.raises(TypeError):
        binary.runtime_env["TSAN_OPTIONS"] = "history_size=0"  # type: ignore[index]

    assert binary.runtime_env["TSAN_OPTIONS"] == "history_size=7"


def test_built_binary_unshares_the_mapping_it_was_handed() -> None:
    """Builders pass module-level pinned tables in; holding the caller's dict would let a
    later edit rewrite an already-built binary's environment."""
    options = {"TSAN_OPTIONS": "history_size=7"}
    binary = BuiltBinary(
        path=Path("build/data_race_tsan"),
        build_dir=Path("build"),
        sanitizer=SanitizerKind.THREAD,
        runtime_env=options,
        compile_commands=None,
        warnings=(),
    )

    options["TSAN_OPTIONS"] = "history_size=0"

    assert binary.runtime_env["TSAN_OPTIONS"] == "history_size=7"


def test_build_failure_defaults() -> None:
    failure = a_build_failure()

    assert failure.stage == "compile"
    assert failure.output.startswith("a.cpp:1:1: error:")
    assert failure.reason is None
    assert failure.suggestion is None
    assert failure.timed_out is False


def test_build_failure_carries_a_diagnosis_when_there_is_one() -> None:
    failure = BuildFailure(
        stage="compile",
        output="/usr/bin/ld: cannot find -ltsan",
        reason="the sanitizer runtime is a separate package here and is not installed",
        suggestion="sudo apt install libtsan0",
        timed_out=False,
    )

    assert failure.reason == "the sanitizer runtime is a separate package here and is not installed"
    assert failure.suggestion == "sudo apt install libtsan0"

    with pytest.raises(FrozenInstanceError):
        failure.stage = "build"  # type: ignore[misc]


def test_hotspot_constructs() -> None:
    hotspot = Hotspot(
        function="OrderBook::match()",
        self_pct=42.5,
        total_pct=61.0,
        location=Location(file="order_book.cpp", line=88),
        note="inlined",
    )

    assert hotspot.self_pct == 42.5
    assert hotspot.location is not None
    assert hotspot.location.column is None
    assert Hotspot(function="idle", self_pct=0.0, total_pct=0.0).note is None


def a_variant_result() -> VariantResult:
    return VariantResult(
        name="flat_map",
        runs=5,
        mean_ms=812.4,
        min_ms=798.0,
        stddev_ms=11.2,
        matches_baseline=True,
    )


def test_variant_result_defaults_to_unproven() -> None:
    """A variant starts with nothing granted: no match claimed, no numbers, no verdict."""
    bare = VariantResult(name="baseline", runs=0)

    assert bare.mean_ms is None
    assert bare.min_ms is None
    assert bare.stddev_ms is None
    assert bare.matches_baseline is False
    assert bare.rejected is None


def test_rejected_variant_keeps_its_reason_and_no_numbers() -> None:
    out = VariantResult(name="unordered", runs=1, rejected="output differs from baseline")

    assert out.rejected == "output differs from baseline"
    assert out.mean_ms is None
    assert out.matches_baseline is False


def test_benchmark_report_constructs_and_defaults() -> None:
    report = BenchmarkReport(
        baseline="baseline",
        variants=(a_variant_result(),),
        repeats=5,
    )

    assert report.baseline == "baseline"
    assert report.variants[0].name == "flat_map"
    assert report.limitations == ()
    assert report.next_step is None


def test_benchmark_models_are_frozen_and_slotted() -> None:
    result = a_variant_result()
    report = BenchmarkReport(baseline="baseline", variants=(result,), repeats=5)

    with pytest.raises(FrozenInstanceError):
        result.mean_ms = 1.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.repeats = 9  # type: ignore[misc]
    for instance in (result, report):
        assert not hasattr(instance, "__dict__"), f"{type(instance).__name__} lost its slots"
