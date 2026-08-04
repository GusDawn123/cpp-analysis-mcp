"""Pin the shape of the shared vocabulary in models.py.

The dataclasses are frozen and slotted on purpose: a Finding passed between layers
must not be edited in place, and a typo like `finding.messge = ...` must fail loudly
rather than attach a new attribute nobody reads.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cpp_analysis_mcp.models import (
    AccessOp,
    BuiltBinary,
    CapabilityStatus,
    Finding,
    Frame,
    Hotspot,
    Location,
    SanitizerKind,
    Severity,
    ThreadAccess,
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


def test_built_binary_takes_a_plain_dict_for_runtime_env() -> None:
    binary = a_built_binary()

    assert binary.runtime_env["TSAN_OPTIONS"] == "history_size=7"
    assert binary.compile_commands is None
    assert binary.sanitizer == "thread"
    assert binary.warnings == ()


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
