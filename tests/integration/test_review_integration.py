"""The acceptance run: plant a bug, review, get exactly the planted finding back as new.
Real git, real compiler, real clang-tidy -- the whole gate with nothing faked, on a
throwaway repository whose first commit already carries one known finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cpp_analysis_mcp import platforms, process
from cpp_analysis_mcp.capabilities import discover_toolchains, probe_all
from cpp_analysis_mcp.pipelines.review import (
    AuditReport,
    ReviewReport,
    audit_project,
    remembered_finding,
    review_project,
)
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.store.models import Analysis, CapabilityStatus, Finding
from cpp_analysis_mcp.toolchains.base import Toolchain

pytestmark = pytest.mark.integration

USE_AFTER_MOVE = "bugprone-use-after-move"

FIRST_BUG = """\
#include <string>
#include <utility>

int first_length() {
    std::string a = "one";
    std::string b = std::move(a);
    return static_cast<int>(a.size()) + static_cast<int>(b.size());
}
"""

SECOND_BUG = """\

int second_length() {
    std::string c = "two";
    std::string d = std::move(c);
    return static_cast<int>(c.size()) + static_cast<int>(d.size());
}
"""


@pytest.fixture(scope="module")
def toolchain() -> Toolchain:
    found = [chain for chain in discover_toolchains() if chain.family == "clang"]
    if not found:
        pytest.skip("no clang on this machine")
    return found[0]


@pytest.fixture(scope="module")
def host() -> Platform:
    return platforms.detect()


@pytest.fixture(scope="module")
def capabilities(toolchain: Toolchain, host: Platform) -> dict[Analysis, CapabilityStatus]:
    probed = probe_all(toolchain, host, cache_dir=None)
    if not probed[Analysis.CLANG_TIDY].available:
        pytest.skip(f"clang-tidy is unavailable here: {probed[Analysis.CLANG_TIDY].reason}")
    return probed


def git(directory: Path, *args: str) -> None:
    result = process.run(["git", "-C", str(directory), *args], timeout_s=30)
    assert result.exit_code == 0, result.output


def test_review_reports_exactly_the_planted_finding_as_new(
    tmp_path: Path,
    toolchain: Toolchain,
    host: Platform,
    capabilities: dict[Analysis, CapabilityStatus],
) -> None:
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    source = repo / "src" / "a.cpp"
    source.write_text(FIRST_BUG, encoding="utf-8")
    # name the branch outright: the machine's init.defaultBranch must not decide the test
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=ci@example.com", "-c", "user.name=CI", "commit", "-q", "-m", "s")
    cache = tmp_path / "cache"

    known = audit_project(
        repo,
        record_as="main",
        toolchain=toolchain,
        platform=host,
        capabilities=capabilities,
        cache_dir=cache,
        runner=process.run,
    )
    assert isinstance(known, AuditReport), known
    audited_categories = [entry.category for entry in known.index]
    assert USE_AFTER_MOVE in audited_categories  # the pre-existing bug is visible

    source.write_text(FIRST_BUG + SECOND_BUG, encoding="utf-8")

    report = review_project(
        repo,
        "main",
        toolchain=toolchain,
        platform=host,
        capabilities=capabilities,
        cache_dir=cache,
        runner=process.run,
    )
    assert isinstance(report, ReviewReport), report
    assert report.baseline_used is True

    fresh_categories = [entry.category for entry in report.index]
    # the planted second bug is new; the first one is baseline and stays out of the way
    assert fresh_categories.count(USE_AFTER_MOVE) == 1
    assert report.total_new < known.total + audited_categories.count(USE_AFTER_MOVE)

    # the new finding's identity round-trips through the remembered run
    (fresh,) = [e.fingerprint for e in report.index if e.category == USE_AFTER_MOVE]
    detail = remembered_finding(repo, fresh, cache_dir=cache, runner=process.run)
    assert isinstance(detail, Finding), detail
    assert detail.category == USE_AFTER_MOVE
