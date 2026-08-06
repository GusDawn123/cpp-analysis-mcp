"""Keep scripts/fixtures.py, the C++ fixtures, and the captured goldens in agreement.

The support matrix in scripts/fixtures.py is the ground truth for which fixture proves
which tool still works. These tests fail when the three drift apart -- a case naming a
source that no longer exists, a source nothing exercises, a golden that no case explains.
No compiler is involved; everything here is reading files.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from helpers import CLEAN_FIXTURE, CPP_DIR, FIXTURES_SCRIPT, GOLDEN_DIR, bug_line, cpp_source


def load_fixtures_script() -> ModuleType:
    """Import scripts/fixtures.py by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("fixtures_script", FIXTURES_SCRIPT)
    assert spec is not None and spec.loader is not None, f"cannot load {FIXTURES_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    # register before executing: @dataclass resolves annotations through sys.modules
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FIXTURES = load_fixtures_script()
SUPPORT: list[Any] = list(FIXTURES.SUPPORT)
CASES_BY_KEY: dict[tuple[str, str], Any] = {(case.tool, case.fixture): case for case in SUPPORT}

# tool_stem.os-family.txt -- the stem carries underscores, so peel the suffix off the end
GOLDEN_NAME = re.compile(r"^(?P<pair>.+)\.(?P<os>darwin|linux)-(?P<family>clang|gcc)\.txt$")

# The capture script builds and runs programs, so it owns the runtime sanitizers only.
# -Wthread-safety reports while compiling: there is nothing to run and no sanitizer to
# name, so these are captured by hand and the matrix does not speak for them. One mapping
# registers each hand-captured golden to its fixture -- two independent lists could drift,
# and an orphaned or mistyped name on either side would pass both.
COMPILE_TIME_CAPTURES = {
    "unguarded_write": ("thread_safety_unguarded_write.darwin-clang.txt",),
    "nullptr_zero": ("clang_tidy_nullptr_zero.linux-clang.txt",),
}
COMPILE_TIME_FIXTURES = frozenset(COMPILE_TIME_CAPTURES)
COMPILE_TIME_GOLDENS = frozenset(name for names in COMPILE_TIME_CAPTURES.values() for name in names)


def fixture_stems() -> list[str]:
    return sorted(path.stem for path in CPP_DIR.glob("*.cpp"))


def golden_files() -> list[Path]:
    # ignore dotfiles so a stray .DS_Store cannot fail the naming rule
    return sorted(
        path for path in GOLDEN_DIR.iterdir() if path.is_file() and not path.name.startswith(".")
    )


def read_golden(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_support_matrix_is_not_empty() -> None:
    assert SUPPORT, f"no cases found in {FIXTURES_SCRIPT}"
    assert fixture_stems(), f"no C++ fixtures found in {CPP_DIR}"
    assert len(CASES_BY_KEY) == len(SUPPORT), "two cases share the same (tool, fixture) pair"


def test_every_case_names_an_existing_source() -> None:
    missing = [
        f"{case.tool}/{case.fixture} -> {cpp_source(case.fixture)}"
        for case in SUPPORT
        if not cpp_source(case.fixture).is_file()
    ]

    assert not missing, "cases point at sources that do not exist:\n" + "\n".join(missing)


def test_every_source_is_exercised_by_a_case() -> None:
    covered = {case.fixture for case in SUPPORT} | COMPILE_TIME_FIXTURES
    orphans = [stem for stem in fixture_stems() if stem not in covered]

    assert not orphans, f"C++ fixtures no case exercises: {orphans}"


def test_cases_state_what_they_prove() -> None:
    """Every buggy case asserts on output; the clean control asserts on silence."""
    problems: list[str] = []
    for case in SUPPORT:
        label = f"{case.tool}/{case.fixture}"
        if case.fixture == CLEAN_FIXTURE:
            if not case.forbid:
                problems.append(f"{label}: clean case has an empty forbid list")
        elif not case.expect:
            problems.append(f"{label}: buggy case has an empty expect list")

    assert not problems, "\n".join(problems)


def test_every_buggy_fixture_has_exactly_one_marker() -> None:
    for stem in fixture_stems():
        if stem == CLEAN_FIXTURE:
            continue
        # raises unless the file carries exactly one // BUG: line
        assert bug_line(stem) > 0


def test_clean_fixture_has_no_marker() -> None:
    with pytest.raises(ValueError, match="BUG"):
        bug_line(CLEAN_FIXTURE)


def test_golden_filenames_follow_the_convention() -> None:
    known_pairs = {f"{tool}_{stem}" for tool, stem in CASES_BY_KEY}
    problems: list[str] = []
    for path in golden_files():
        match = GOLDEN_NAME.match(path.name)
        if match is None:
            problems.append(f"{path.name}: expected {{tool}}_{{stem}}.{{os}}-{{family}}.txt")
        elif path.name in COMPILE_TIME_GOLDENS:
            # hand-captured: named by the same convention, but no matrix case owns it
            continue
        elif match.group("pair") not in known_pairs:
            problems.append(f"{path.name}: no case in the support matrix produces it")

    assert not problems, "unexpected files in tests/fixtures/golden/:\n" + "\n".join(problems)


def test_the_hand_captured_fixtures_are_all_there() -> None:
    """The exemptions above must not become a way to lose a fixture or a golden quietly."""
    missing: list[str] = []
    empty: list[str] = []
    # walked as pairs, so a golden is only ever checked under the fixture that owns it
    for stem, goldens in sorted(COMPILE_TIME_CAPTURES.items()):
        source = cpp_source(stem)
        if not source.is_file():
            missing.append(str(source))
        elif not source.read_text(encoding="utf-8").strip():
            empty.append(source.name)
        for name in goldens:
            path = GOLDEN_DIR / name
            if not path.is_file():
                missing.append(str(path))
            elif not read_golden(path).strip():
                empty.append(name)

    assert not missing, f"hand-captured fixtures that do not exist: {missing}"
    assert not empty, f"hand-captured goldens with nothing in them: {empty}"


def test_goldens_contain_what_their_case_expects() -> None:
    problems: list[str] = []
    for path in golden_files():
        match = GOLDEN_NAME.match(path.name)
        if match is None:
            continue  # the naming test owns this failure
        tool, _, stem = match.group("pair").partition("_")
        case = CASES_BY_KEY.get((tool, stem))
        if case is None:
            continue

        content = read_golden(path)
        if case.fixture == CLEAN_FIXTURE:
            leaked = [marker for marker in case.forbid if marker in content]
            if leaked:
                problems.append(f"{path.name}: clean run reported {leaked}")
            if content:
                problems.append(f"{path.name}: clean run must be silent, got {content[:120]!r}")
            continue

        missing = [marker for marker in case.expect if marker not in content]
        if missing:
            problems.append(f"{path.name}: missing expected {missing}")

    assert not problems, "goldens disagree with the support matrix:\n" + "\n".join(problems)


def test_golden_points_at_the_marked_bug_line() -> None:
    """End-to-end check of the marker convention: TSan reported the line // BUG: sits on."""
    golden = GOLDEN_DIR / "tsan_data_race.darwin-clang.txt"
    assert golden.is_file(), f"missing captured output: {golden}"

    reference = f"data_race.cpp:{bug_line('data_race')}"
    assert reference in read_golden(golden), f"{golden.name} does not reference {reference}"
