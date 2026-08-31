"""Pin the identity fields Finding gained for the store. The four default to "no claim":
an unfingerprinted finding says so through scheme 0, not a plausible-looking hash, and a
finding that names no second tool claims no confirmation.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

import cpp_analysis_mcp.store as store_package
import cpp_analysis_mcp.store.models as store_models
from cpp_analysis_mcp.store.models import Confirmation, Finding, Severity


def a_finding() -> Finding:
    return Finding(
        id="tidy-0001",
        tool="clang-tidy",
        severity=Severity.WARNING,
        category="bugprone-use-after-move",
        message="'order' used after it was moved",
    )


def test_identity_fields_default_to_no_claim() -> None:
    finding = a_finding()

    assert finding.fingerprint == ""
    assert finding.fingerprint_scheme == 0
    assert finding.engine == "local"
    assert finding.confirmations == ()


def test_a_fingerprinted_finding_carries_its_scheme() -> None:
    finding = replace(a_finding(), fingerprint="9f2c04d1", fingerprint_scheme=1)

    assert finding.fingerprint == "9f2c04d1"
    assert finding.fingerprint_scheme == 1


def test_engine_records_where_the_observation_happened() -> None:
    finding = replace(a_finding(), engine="linux-container")

    assert finding.engine == "linux-container"


def test_confirmations_point_at_the_agreeing_finding() -> None:
    confirmation = Confirmation(tool="cppcheck", finding_id="cppcheck-0042")
    finding = replace(a_finding(), confirmations=(confirmation,))

    assert finding.confirmations[0].tool == "cppcheck"
    assert finding.confirmations[0].finding_id == "cppcheck-0042"


def test_confirmation_is_frozen_and_slotted() -> None:
    confirmation = Confirmation(tool="cppcheck", finding_id="cppcheck-0042")

    with pytest.raises(FrozenInstanceError):
        confirmation.tool = "clang-tidy"  # type: ignore[misc]
    assert not hasattr(confirmation, "__dict__"), "Confirmation lost its slots"


def test_the_package_facade_forwards_the_same_objects_not_copies() -> None:
    # a facade that re-declared anything would give isinstance checks two distinct
    # classes with the same name -- every name must be the one store.models defines
    assert set(store_models.__all__) < set(store_package.__all__)
    for name in store_models.__all__:
        assert getattr(store_package, name) is getattr(store_models, name), name
