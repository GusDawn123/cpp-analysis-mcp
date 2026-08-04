"""Smoke test proving the package imports and its dataclasses construct."""

import cpp_analysis_mcp
from cpp_analysis_mcp.models import Finding, Location, Severity


def test_version() -> None:
    assert cpp_analysis_mcp.__version__ == "0.1.0"


def test_finding_constructs() -> None:
    finding = Finding(
        id="tsan-0001",
        tool="thread-sanitizer",
        severity=Severity.ERROR,
        category="data-race",
        message="data race on OrderBook::best_bid_",
        location=Location(file="order_book.cpp", line=42, column=7),
    )

    assert finding.severity == "error"
    assert finding.threads == ()
    assert finding.occurrences == 1
