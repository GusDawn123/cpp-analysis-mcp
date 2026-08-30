"""Read clang-tidy's own fix-it export, or read nothing and say nothing. A suggestion is
a bonus on top of a finding that already stands, so every way the export can disappoint --
absent, truncated, malformed, pointing past a stale end -- comes back empty, not loudly.
"""

from __future__ import annotations

from collections.abc import Callable

from cpp_analysis_mcp.parsers.tidy_fixes import parse

FILE = "/repo/src/order.hpp"
MEMBER_INIT = "cppcoreguidelines-pro-type-member-init"

# three lines whose newline offsets are 3, 7 and 13 -- the known answers below count them
LINES = b"one\ntwo\nthree\n"

SOURCE = b"struct Order {\n    int id;\n    Order() {}\n};\n"


def an_export(
    *,
    check: str = MEMBER_INIT,
    file: str = FILE,
    fix_file: str | None = None,
    offset: int,
    length: int,
    text: str,
) -> str:
    """One diagnostic carrying one replacement, spelled the way clang-tidy spells it."""
    return f"""\
---
MainSourceFile:  '{file}'
Diagnostics:
  - DiagnosticName:  '{check}'
    DiagnosticMessage:
      Message:         'constructor does not initialize these fields: id'
      FilePath:        '{file}'
      FileOffset:      {offset}
      Replacements:
        - FilePath:        '{fix_file if fix_file is not None else file}'
          Offset:          {offset}
          Length:          {length}
          ReplacementText: '{text}'
    Level:           Warning
...
"""


def reading(content: bytes, *, named: str = FILE) -> Callable[[str], bytes | None]:
    """A reader that knows one file and answers None for every other, as an OSError would."""
    return lambda file: content if file == named else None


def test_a_replacement_becomes_a_suggestion_naming_what_goes_and_what_arrives() -> None:
    at = SOURCE.index(b"{}") + 1
    export = an_export(offset=at, length=0, text=" : id() ")

    (fix,) = parse(export, reading(SOURCE))

    assert fix.check == MEMBER_INIT
    assert fix.file == FILE
    assert fix.line == 3
    assert fix.replaced == ""
    assert fix.replacement == " : id() "


def test_the_replaced_text_is_the_bytes_the_edit_would_overwrite() -> None:
    at = SOURCE.index(b"int id;")
    export = an_export(offset=at, length=len(b"int id;"), text="int id{0};")

    (fix,) = parse(export, reading(SOURCE))

    assert fix.replaced == "int id;"
    assert fix.replacement == "int id{0};"
    assert fix.line == 2


def test_the_line_is_counted_from_the_files_own_bytes() -> None:
    """Known answers: offsets either side of each newline in a file of three lines."""
    expected = {0: 1, 3: 1, 4: 2, 7: 2, 8: 3, 13: 3}

    lines = {
        offset: fix.line
        for offset in expected
        for fix in parse(an_export(offset=offset, length=0, text="x"), reading(LINES))
    }

    assert lines == expected


def test_an_insertion_at_the_very_end_of_a_file_is_still_in_range() -> None:
    (fix,) = parse(an_export(offset=len(LINES), length=0, text="four\n"), reading(LINES))

    assert fix.line == 4


def test_an_offset_past_the_end_of_the_file_produces_nothing() -> None:
    """The file was edited since clang-tidy read it; a fix sliced from thin air is worse
    than no fix, and the finding it belongs to stands either way."""
    assert parse(an_export(offset=len(LINES) + 1, length=0, text="x"), reading(LINES)) == ()
    assert parse(an_export(offset=len(LINES) - 1, length=9, text="x"), reading(LINES)) == ()


def test_a_negative_offset_produces_nothing() -> None:
    assert parse(an_export(offset=-1, length=0, text="x"), reading(LINES)) == ()


def test_a_file_that_cannot_be_read_produces_nothing() -> None:
    assert parse(an_export(offset=0, length=0, text="x"), lambda _file: None) == ()


def test_a_diagnostic_with_no_replacements_produces_nothing() -> None:
    export = """\
---
MainSourceFile:  '/repo/src/order.hpp'
Diagnostics:
  - DiagnosticName:  'bugprone-use-after-move'
    DiagnosticMessage:
      Message:         "'a' used after it was moved"
      FilePath:        '/repo/src/order.hpp'
      FileOffset:      4
      Replacements:    []
    Level:           Warning
...
"""

    assert parse(export, reading(SOURCE)) == ()


def test_malformed_yaml_produces_nothing() -> None:
    assert parse("Diagnostics: [ - this: is not: yaml", reading(SOURCE)) == ()


def test_an_empty_export_produces_nothing() -> None:
    """clang-tidy writes the file whether or not any check offered a fix."""
    assert parse("", reading(SOURCE)) == ()
    empty = "---\nMainSourceFile: '/repo/src/order.hpp'\nDiagnostics:\n...\n"
    assert parse(empty, reading(SOURCE)) == ()


def test_a_document_of_the_wrong_shape_produces_nothing() -> None:
    for text in ("[1, 2, 3]\n", "Diagnostics: 7\n", "Diagnostics:\n  - 'a string'\n"):
        assert parse(text, reading(SOURCE)) == ()


def test_an_edit_landing_in_another_file_than_the_diagnostic_is_left_alone() -> None:
    """The suggestion is paired to a finding by the file the diagnostic named, so an edit
    somewhere else cannot be presented under it."""
    export = an_export(fix_file="/repo/src/other.cpp", offset=0, length=0, text="x")

    assert parse(export, reading(SOURCE)) == ()


def test_a_replacement_missing_its_numbers_produces_nothing() -> None:
    export = """\
---
Diagnostics:
  - DiagnosticName:  'cppcoreguidelines-pro-type-member-init'
    DiagnosticMessage:
      FilePath:        '/repo/src/order.hpp'
      Replacements:
        - FilePath:        '/repo/src/order.hpp'
          ReplacementText: ' : id()'
...
"""

    assert parse(export, reading(SOURCE)) == ()


def test_every_diagnostic_in_the_export_is_read() -> None:
    export = """\
---
MainSourceFile:  '/repo/src/order.hpp'
Diagnostics:
  - DiagnosticName:  'first-check'
    DiagnosticMessage:
      FilePath:        '/repo/src/order.hpp'
      FileOffset:      0
      Replacements:
        - FilePath:        '/repo/src/order.hpp'
          Offset:          0
          Length:          3
          ReplacementText: 'ONE'
  - DiagnosticName:  'second-check'
    DiagnosticMessage:
      FilePath:        '/repo/src/order.hpp'
      FileOffset:      4
      Replacements:
        - FilePath:        '/repo/src/order.hpp'
          Offset:          4
          Length:          3
          ReplacementText: 'TWO'
...
"""

    first, second = parse(export, reading(LINES))

    assert (first.check, first.line, first.replaced) == ("first-check", 1, "one")
    assert (second.check, second.line, second.replaced) == ("second-check", 2, "two")


def test_the_diagnostics_own_line_travels_as_the_join_key() -> None:
    # the diagnostic speaks at its FileOffset; the edit may land lines away, and both
    # coordinates must survive or the fix cannot find its one finding
    edit_at = SOURCE.rindex(b"{}")
    export = an_export(offset=edit_at, length=0, text=" : id() ").replace(
        f"FileOffset:      {edit_at}", "FileOffset:      0", 1
    )
    (fix,) = parse(export, reading(SOURCE))
    assert fix.at == 1  # offset 0 sits on the struct line
    assert fix.line == 3  # the edit lands where the constructor body is


def test_a_diagnostic_with_no_offset_of_its_own_offers_nothing() -> None:
    # without the diagnostic's coordinate the edit could attach to any same-check
    # sibling in the file, so it attaches to none
    export = an_export(offset=4, length=0, text="x").replace(
        "FileOffset:      4", "FileOffset:      unplaced"
    )
    assert parse(export, reading(SOURCE)) == ()
