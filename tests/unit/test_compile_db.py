"""Find and read a compilation database, with nothing but files on disk.

Every shape here came off a real cmake build tree, not imagined -- especially the response
file case: on Windows, cmake moves include flags into one once a project has a few, so a
command with no -I at all is the norm there, the exact case this module exists to handle.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cpp_analysis_mcp import compile_db

# a cmake/Ninja entry as written on Windows, forward slashes in the flag and backslashes in
# the paths, exactly as the generator emits it
INLINE_COMMAND = (
    r"C:\PROGRA~1\LLVM\bin\CLANG_~1.EXE -I{include} -O3 -DNDEBUG -std=gnu++20 "
    r"-D_DLL -Xclang --dependent-lib=msvcrt -o obj\OrderBook.cpp.obj -c {source}"
)

# the same build once the include list is long enough for cmake to move it out of the way
RESPONSE_COMMAND = r"C:\msys64\ucrt64\bin\c++.exe  @{rsp} -std=gnu++20 -o obj\x.obj -c {source}"


def write_db(directory: Path, entries: list[dict[str, object]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / compile_db.DATABASE
    database.write_text(json.dumps(entries), encoding="utf-8")
    return database


def a_project(root: Path) -> tuple[Path, Path]:
    """A source file and the include directory it needs, on disk."""
    source = root / "engine" / "src" / "OrderBook.cpp"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('#include "orderbook/OrderBook.hpp"\n', encoding="utf-8")
    include = root / "engine" / "include"
    include.mkdir(parents=True, exist_ok=True)
    return source, include


# ------------------------------------------------------------------------------ finding one


def test_a_database_beside_the_sources_is_found(tmp_path: Path) -> None:
    source, _ = a_project(tmp_path)
    database = write_db(
        tmp_path, [{"directory": str(tmp_path), "file": str(source), "command": "clang++"}]
    )

    assert compile_db.find(source) == database


def test_a_database_in_a_build_directory_is_found(tmp_path: Path) -> None:
    """Where it actually lives: nobody commits one next to their sources."""
    source, _ = a_project(tmp_path)
    database = write_db(
        tmp_path / "build",
        [{"directory": str(tmp_path), "file": str(source), "command": "clang++"}],
    )

    assert compile_db.find(source) == database


def test_the_database_that_names_the_file_wins_over_a_nearer_one(tmp_path: Path) -> None:
    """A checkout commonly holds several build trees and they do not describe the same files.

    The one that compiled this file knows its flags; the others are guesses about it. Named
    so the wrong one sorts first, or the preference would pass by accident.
    """
    source, _ = a_project(tmp_path)
    write_db(
        tmp_path / "build-aaa",
        [{"directory": str(tmp_path), "file": str(tmp_path / "other.cpp"), "command": "clang++"}],
    )
    right = write_db(
        tmp_path / "build-zzz",
        [{"directory": str(tmp_path), "file": str(source), "command": "clang++"}],
    )

    assert compile_db.find(source) == right


def test_a_file_with_no_database_anywhere_finds_none(tmp_path: Path) -> None:
    """Most single files. The check still runs; it just runs without project includes."""
    source, _ = a_project(tmp_path)

    assert compile_db.find(source) is None


# -------------------------------------------------------------------------- reading the flags


def test_the_flags_that_decide_whether_a_file_parses_are_taken(tmp_path: Path) -> None:
    """Include paths, defines and the language standard. Those decide whether the file
    parses; everything else describes a build that is not happening here."""
    source, include = a_project(tmp_path)
    database = write_db(
        tmp_path / "build",
        [
            {
                "directory": str(tmp_path / "build"),
                "file": str(source),
                "command": INLINE_COMMAND.format(include=include, source=source),
            }
        ],
    )

    flags = compile_db.flags_for(database, source)

    assert f"-I{include}" in flags
    assert "-DNDEBUG" in flags
    assert "-D_DLL" in flags
    assert "-std=gnu++20" in flags


def test_everything_belonging_to_a_build_is_left_behind(tmp_path: Path) -> None:
    """A check compiles nothing and links nothing, so an -o it copied across would write an
    object file, and an -O3 would describe a different compilation than the one it is doing."""
    source, include = a_project(tmp_path)
    database = write_db(
        tmp_path / "build",
        [
            {
                "directory": str(tmp_path / "build"),
                "file": str(source),
                "command": INLINE_COMMAND.format(include=include, source=source),
            }
        ],
    )

    flags = compile_db.flags_for(database, source)

    assert "-O3" not in flags
    assert "-o" not in flags
    assert "-c" not in flags
    assert str(source) not in flags
    assert not any("dependent-lib" in flag for flag in flags)


def test_an_include_list_moved_into_a_response_file_is_still_read(tmp_path: Path) -> None:
    """The Windows default once a project has a few include directories. Read without
    expanding it, the entry looks like a compilation with no include paths at all."""
    source, include = a_project(tmp_path)
    build = tmp_path / "build"
    build.mkdir(parents=True, exist_ok=True)
    rsp = build / "includes_CXX.rsp"
    rsp.write_text(f"-I{include} -DFROM_RESPONSE_FILE", encoding="utf-8")
    database = write_db(
        build,
        [
            {
                "directory": str(build),
                "file": str(source),
                # named relative to the entry's directory, as cmake writes it
                "command": RESPONSE_COMMAND.format(rsp="includes_CXX.rsp", source=source),
            }
        ],
    )

    flags = compile_db.flags_for(database, source)

    assert f"-I{include}" in flags
    assert "-DFROM_RESPONSE_FILE" in flags


def test_a_relative_include_is_resolved_against_the_entrys_own_directory(tmp_path: Path) -> None:
    """The check runs from somewhere else entirely, so a path relative to the build tree
    means nothing by the time it is used."""
    source, include = a_project(tmp_path)
    build = tmp_path / "build"
    database = write_db(
        build,
        [
            {
                "directory": str(build),
                "file": str(source),
                "command": "clang++ -I../engine/include -c " + str(source),
            }
        ],
    )

    flags = compile_db.flags_for(database, source)

    (resolved,) = [flag for flag in flags if flag.startswith("-I")]
    assert Path(resolved[2:]).is_absolute()
    assert Path(resolved[2:]).resolve() == include.resolve()


def test_a_separate_form_include_keeps_its_argument(tmp_path: Path) -> None:
    """-I and its directory as two words, which is how hand-written builds spell it."""
    source, include = a_project(tmp_path)
    database = write_db(
        tmp_path / "build",
        [
            {
                "directory": str(tmp_path / "build"),
                "file": str(source),
                "command": f"clang++ -I {include} -isystem {include} -c {source}",
            }
        ],
    )

    flags = compile_db.flags_for(database, source)

    assert flags.count("-I") == 1
    assert str(include) in flags
    assert "-isystem" in flags


def test_the_arguments_form_is_read_as_well_as_the_command_form(tmp_path: Path) -> None:
    """Both are valid; which one a database uses depends on its generator."""
    source, include = a_project(tmp_path)
    database = write_db(
        tmp_path / "build",
        [
            {
                "directory": str(tmp_path / "build"),
                "file": str(source),
                "arguments": ["clang++", f"-I{include}", "-std=c++23", "-c", str(source)],
            }
        ],
    )

    flags = compile_db.flags_for(database, source)

    assert f"-I{include}" in flags
    assert "-std=c++23" in flags


def test_a_file_the_database_never_compiled_gets_every_include_it_knows(tmp_path: Path) -> None:
    """Headers compile in no TU of their own, so none appears in a database, yet a header is
    an ordinary check target. Wider is the safe default: a spare -I changes nothing, but a
    missing one stops the file parsing -- so a header gets every -I the database knows.
    """
    source, include = a_project(tmp_path)
    header = include / "orderbook" / "OrderBook.hpp"
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_text("#pragma once\n", encoding="utf-8")
    other = tmp_path / "vendor"
    database = write_db(
        tmp_path / "build",
        [
            {
                "directory": str(tmp_path / "build"),
                "file": str(source),
                "command": f"clang++ -I{include} -c {source}",
            },
            {
                "directory": str(tmp_path / "build"),
                "file": str(tmp_path / "other.cpp"),
                "command": f"clang++ -I{other} -c {tmp_path / 'other.cpp'}",
            },
        ],
    )

    flags = compile_db.flags_for(database, header)

    assert f"-I{include}" in flags
    assert f"-I{other}" in flags


def test_a_relative_file_entry_resolves_against_its_own_directory(tmp_path: Path) -> None:
    """CMake writes absolute paths, but bear and hand-written databases write relative
    ones -- resolved against the process's cwd they match nothing, and the entry's
    flags are silently lost."""
    source, include = a_project(tmp_path)
    database = write_db(
        tmp_path / "build",
        [
            {
                "directory": str(source.parent),
                "file": source.name,
                "command": f"clang++ -I{include} -DMATCHED -c {source.name}",
            }
        ],
    )

    assert "-DMATCHED" in compile_db.flags_for(database, source)


def test_paths_spelled_differently_still_match_the_same_file(tmp_path: Path) -> None:
    """A database writes whatever spelling the build used -- forward slashes on Windows, a
    different case -- so the strings are not comparable and the resolved paths are."""
    source, include = a_project(tmp_path)
    database = write_db(
        tmp_path / "build",
        [
            {
                "directory": str(tmp_path / "build"),
                "file": str(source).replace(os.sep, "/"),
                "command": f"clang++ -I{include} -c {source}",
            }
        ],
    )

    assert compile_db.find(source) == database
    assert f"-I{include}" in compile_db.flags_for(database, source)


# ------------------------------------------------------------------------- when it is broken


def test_a_database_that_cannot_be_parsed_answers_with_nothing(tmp_path: Path) -> None:
    """A half-written build directory must not become a crash about the wrong thing: the
    check can still run without a database, exactly as it did before this module existed."""
    source, _ = a_project(tmp_path)
    broken = tmp_path / "build" / compile_db.DATABASE
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{not json", encoding="utf-8")

    assert compile_db.flags_for(broken, source) == ()


def test_a_response_file_the_build_tree_no_longer_has_is_skipped(tmp_path: Path) -> None:
    """Build trees get cleaned while a database survives; that is a missing include path,
    not a reason to fail the check."""
    source, _ = a_project(tmp_path)
    build = tmp_path / "build"
    database = write_db(
        build,
        [
            {
                "directory": str(build),
                "file": str(source),
                "command": RESPONSE_COMMAND.format(rsp="gone.rsp", source=source),
            }
        ],
    )

    assert compile_db.flags_for(database, source) == ("-std=gnu++20",)
