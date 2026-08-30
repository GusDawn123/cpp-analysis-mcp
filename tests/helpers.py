"""Paths and lookups shared by the unit tests. Every buggy fixture marks its planted bug
with a `// BUG:` comment on one line; tests find it through `bug_line`, so editing a
fixture moves the assertions with it rather than silently checking the wrong line.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
CPP_DIR = FIXTURES_DIR / "cpp"
GOLDEN_DIR = FIXTURES_DIR / "golden"
SRC_DIR = REPO_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "cpp_analysis_mcp"
FIXTURES_SCRIPT = REPO_ROOT / "scripts" / "fixtures.py"

BUG_MARKER = "// BUG:"

# the one fixture that is supposed to be silent, so it carries no marker
CLEAN_FIXTURE = "clean"


def cpp_source(fixture_stem: str) -> Path:
    return CPP_DIR / f"{fixture_stem}.cpp"


def bug_line(fixture_stem: str) -> int:
    """Return the 1-based line number of the `// BUG:` marker in <stem>.cpp.

    Raises ValueError unless the file carries exactly one marker.
    """
    source = cpp_source(fixture_stem)
    lines = source.read_text(encoding="utf-8").splitlines()
    marked = [number for number, line in enumerate(lines, start=1) if BUG_MARKER in line]
    if len(marked) != 1:
        raise ValueError(
            f"{source}: expected exactly one {BUG_MARKER!r} line, found {len(marked)}"
            + (f" on lines {marked}" if marked else "")
        )
    return marked[0]
