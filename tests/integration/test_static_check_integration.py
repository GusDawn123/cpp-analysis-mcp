"""Run the whole static_check chain against the real compiler and the real clang-tidy.

The unit suite replays captured output through a fake process; this proves the real loop
end to end -- a wrong command, dropped checks, or the wrong parser would all look like the
same empty report. The clean fixture proves that all-clear is earned, not assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import bug_line, cpp_source

from cpp_analysis_mcp import platforms
from cpp_analysis_mcp.capabilities import discover_toolchains, probe_all
from cpp_analysis_mcp.pipelines.static_check import check_file, check_snippet
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.store.models import (
    Analysis,
    AnalysisReport,
    BuildFailure,
    CapabilityStatus,
    Severity,
)
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration

Capabilities = dict[Analysis, CapabilityStatus]

UNGUARDED_WRITE = "unguarded_write"
CLEAN = "clean"
NULLPTR_ZERO = "nullptr_zero"

THREAD_SAFETY_CATEGORY = "thread-safety-analysis"
TIDY_CATEGORY = "modernize-use-nullptr"

# the nullptr fixture is silent under tidy's defaults, so the check has to be asked for
NULLPTR_CHECKS = f"-*,{TIDY_CATEGORY}"

# A guarded variable written with no lock held, with no fixture file behind it, so the snippet
# path has to materialize it. Modelled on capabilities.THREAD_SAFETY_SOURCE: the capability
# attributes are spelled out rather than written through the usual macros, so it needs no
# header, and there is no main() -- which is exactly what -fsyntax-only makes checkable.
GUARDED_SNIPPET = """\
struct __attribute__((capability("mutex"))) SnippetMutex {
    void lock() __attribute__((acquire_capability())) {}
    void unlock() __attribute__((release_capability())) {}
};

namespace {

SnippetMutex m;
int guarded __attribute__((guarded_by(m))) = 0;

void touch() {
    guarded = 1;
}

}  // namespace
"""

# A missing semicolon: the compile fails, but it fails saying file, line and column, so the
# error arrives as a finding rather than as a blob of text.
BROKEN_SNIPPET = """\
int value() {
    int x = 1
    return x;
}
"""


@pytest.fixture(scope="module")
def host() -> Platform:
    return platforms.detect()


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    """clang, because -Wthread-safety is clang's and no other compiler has an equivalent.

    A suite that took either compiler would have to stop asserting the thing it is here to
    check: gcc compiles the guarded-write fixture in silence. clang is also the one compiler
    present on all three target platforms.
    """
    found = [chain for chain in discover_toolchains() if chain.family == "clang"]
    if not found:
        pytest.skip("no clang on this machine")
    return found[0]


@pytest.fixture(scope="module")
def capabilities(toolchain: Toolchain, host: Platform) -> Capabilities:
    """Probe once for the module, with the cache off so these are today's answers, not a file
    some earlier run wrote."""
    return probe_all(toolchain, host, cache_dir=None)


def require(capabilities: Capabilities, analysis: Analysis) -> None:
    """Skip rather than fail when this machine cannot do the analysis under test."""
    status = capabilities[analysis]
    if not status.available:
        pytest.skip(f"{analysis} is unavailable here: {status.reason}")


def reported(result: AnalysisReport | BuildFailure | CapabilityStatus) -> AnalysisReport:
    assert isinstance(result, AnalysisReport), f"expected an AnalysisReport, got {result}"
    return result


def categories(report: AnalysisReport) -> list[str]:
    return [finding.category for finding in report.findings]


def test_thread_safety_finds_the_unguarded_write_on_the_line_the_fixture_marks(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities
) -> None:
    """The whole chain: gate, compile with -fsyntax-only, parse, blame a line."""
    require(capabilities, Analysis.THREAD_SAFETY)

    report = reported(
        check_file(
            cpp_source(UNGUARDED_WRITE),
            Analysis.THREAD_SAFETY,
            toolchain=toolchain,
            platform=host,
            capabilities=capabilities,
        )
    )

    lock_bugs = [
        finding for finding in report.findings if finding.category == THREAD_SAFETY_CATEGORY
    ]
    assert lock_bugs, f"clang reported no lock bug: exit {report.exit_code}, {categories(report)}"
    assert lock_bugs[0].location is not None
    assert lock_bugs[0].location.line == bug_line(UNGUARDED_WRITE)
    assert report.verified_by, "a report with no proof behind it"


def test_a_correctly_locked_program_produces_a_trustworthy_all_clear(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities
) -> None:
    """Only means something because the test above showed this chain reports when it should."""
    require(capabilities, Analysis.THREAD_SAFETY)

    report = reported(
        check_file(
            cpp_source(CLEAN),
            Analysis.THREAD_SAFETY,
            toolchain=toolchain,
            platform=host,
            capabilities=capabilities,
        )
    )

    assert report.findings == ()
    assert report.exit_code == 0
    assert report.timed_out is False


def test_a_snippet_is_written_down_and_checked_like_a_file(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """Text in, findings out: nothing about the source existed before the call."""
    require(capabilities, Analysis.THREAD_SAFETY)
    build_dir = tmp_path / "snippet"

    report = reported(
        check_snippet(
            GUARDED_SNIPPET,
            Analysis.THREAD_SAFETY,
            toolchain=toolchain,
            platform=host,
            capabilities=capabilities,
            build_dir=build_dir,
        )
    )

    assert THREAD_SAFETY_CATEGORY in categories(report)
    # the file stays behind, because the finding's location names it
    assert (build_dir / "snippet.cpp").is_file()


def test_a_snippet_that_does_not_compile_comes_back_as_error_findings(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities, tmp_path: Path
) -> None:
    """The compile failed, but it failed with a file and a line, so it stays structured."""
    require(capabilities, Analysis.THREAD_SAFETY)

    report = reported(
        check_snippet(
            BROKEN_SNIPPET,
            Analysis.THREAD_SAFETY,
            toolchain=toolchain,
            platform=host,
            capabilities=capabilities,
            build_dir=tmp_path / "broken",
        )
    )

    errors = [finding for finding in report.findings if finding.severity is Severity.ERROR]
    assert errors, f"a failed compile reported nothing: exit {report.exit_code}"
    assert errors[0].location is not None
    assert report.exit_code not in (0, None), "the compile is supposed to have failed"


def test_clang_tidy_either_finds_the_null_pointer_or_says_why_it_cannot(
    toolchain: Toolchain, host: Platform, capabilities: Capabilities
) -> None:
    """brew keeps llvm off PATH and plenty of machines have no clang-tidy at all.

    Both branches are the same guarantee from opposite sides: a caller never gets an empty
    finding list from a check that was not running.
    """
    status = capabilities[Analysis.CLANG_TIDY]

    result = check_file(
        cpp_source(NULLPTR_ZERO),
        Analysis.CLANG_TIDY,
        toolchain=toolchain,
        platform=host,
        capabilities=capabilities,
        checks=NULLPTR_CHECKS,
    )

    if not status.available:
        assert isinstance(result, CapabilityStatus), f"a denied analysis still ran: {result}"
        assert result.reason == status.reason
        assert result.reason, "unavailable and saying nothing"
        return

    report = reported(result)
    modernizations = [finding for finding in report.findings if finding.category == TIDY_CATEGORY]
    assert modernizations, f"clang-tidy reported no null pointer: {categories(report)}"
    assert modernizations[0].location is not None
    assert modernizations[0].location.line == bug_line(NULLPTR_ZERO)
