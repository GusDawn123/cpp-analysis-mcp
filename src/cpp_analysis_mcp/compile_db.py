"""Find the compilation database near a source file, and read the flags it needs to parse.

A compile-time check on one file out of a real project fails on a missing include before
it can fail on anything interesting, so this finds compile_commands.json and takes only
what decides whether a file parses -- include directories, macro definitions, the
language standard -- never optimization, warnings, sanitizers, or output paths.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterator
from pathlib import Path

DATABASE = "compile_commands.json"

# where a database is looked for at each level: beside the sources, or in the build directory
# that a conventionally named build tree would have put it in
BUILD_GLOB = "build*"

# how far up from the file to look. Deep enough for any project layout, and bounded so a
# path that is not in a project does not walk the whole drive globbing as it goes
MAX_LEVELS = 12

# what a build system writes instead of a long command line, and what the argument naming
# one begins with. Common on Windows, where the command line is capped near 32k
RESPONSE = "@"

# flags whose value is the next argument along
SEPARATE = frozenset(
    {"-I", "-D", "-U", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-isysroot"}
)

# flags whose value is attached to them. -std= carries the language version, which decides
# whether the file parses at all; the rest name places to look and things to define
ATTACHED = ("-I", "-D", "-U", "-isystem", "-iquote", "-idirafter", "-std=", "--std=", "--sysroot=")

# of those, the ones whose value is a path and therefore relative to the entry's directory
PATH_SEPARATE = frozenset(
    {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-isysroot"}
)
PATH_ATTACHED = ("-I", "-isystem", "-iquote", "-idirafter", "--sysroot=")


def find(source: Path) -> Path | None:
    """Return the compilation database that best covers `source`, or None when there is none.

    Best means: the nearest one that actually names this file. A checkout commonly holds
    several build trees -- a debug one, a release one, one an IDE made -- and they do not
    describe the same files. Falling back to the nearest database that exists at all is still
    worth doing when none names the file, because a header never appears in one and its
    include directories are the same as its neighbours'.
    """
    found = list(_candidates(source))
    for candidate in found:
        if _entry(candidate, source) is not None:
            return candidate
    return found[0] if found else None


def flags_for(database: Path, source: Path) -> tuple[str, ...]:
    """Return the flags this file needs to parse, taken from the database.

    The entry for the file itself when there is one. When there is not -- which is every
    header, since a build compiles no header on its own -- the include and define flags of
    every entry, in first-seen order and without repeats. That is a wider set than any one
    translation unit used, and wider is the safe direction: an unnecessary -I changes nothing
    about how the file parses, and a missing one stops it parsing at all.
    """
    entry = _entry(database, source)
    if entry is not None:
        return _flags(entry)

    seen: dict[str, None] = {}
    for other in _entries(database):
        for flag in _flags(other):
            seen.setdefault(flag, None)
    return tuple(seen)


def _candidates(source: Path) -> Iterator[Path]:
    """Yield the databases near this file, nearest first."""
    for directory in list(source.parents)[:MAX_LEVELS]:
        beside = directory / DATABASE
        if beside.is_file():
            yield beside
        # sorted so a checkout with several build trees is read in one order rather than
        # whichever the filesystem happens to hand back
        yield from sorted(path for path in directory.glob(f"{BUILD_GLOB}/{DATABASE}"))


def _entries(database: Path) -> list[dict[str, object]]:
    """Read the database, or nothing at all when it cannot be read.

    A database we cannot parse is a database we do not have. Raising here would turn a
    half-written build directory into a crash about the wrong thing, and the check can still
    run without one -- it just runs the way it did before this file existed.
    """
    try:
        document: object = json.loads(database.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(document, list):
        return []
    return [entry for entry in document if isinstance(entry, dict)]


def _entry(database: Path, source: Path) -> dict[str, object] | None:
    """Find this file's own entry, comparing paths as the filesystem would.

    A database writes whatever spelling the build used -- forward slashes on Windows, a
    different case, a path through a symlink -- so the strings are not comparable and the
    resolved paths are.
    """
    wanted = _normalized(source)
    for entry in _entries(database):
        named = entry.get("file")
        if not isinstance(named, str):
            continue
        # a relative `file` is relative to the entry's own `directory`, per the spec:
        # CMake writes absolute paths, but bear and hand-written databases write
        # relative ones, and resolved against the cwd they would match nothing
        directory = entry.get("directory")
        base = Path(directory) if isinstance(directory, str) else Path.cwd()
        if _normalized(Path(_absolute(named, base))) == wanted:
            return entry
    return None


def _normalized(path: Path) -> str:
    """One spelling of a path, for comparison only."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return os.path.normcase(str(resolved))


def _flags(entry: dict[str, object]) -> tuple[str, ...]:
    """Take the parse-relevant flags out of one entry, with any relative path made absolute."""
    directory = entry.get("directory")
    base = Path(directory) if isinstance(directory, str) else Path.cwd()

    kept: list[str] = []
    tokens = _tokens(entry, base)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in SEPARATE and index + 1 < len(tokens):
            value = tokens[index + 1]
            kept += [token, _absolute(value, base) if token in PATH_SEPARATE else value]
            index += 2
            continue
        if token.startswith(ATTACHED) and token not in SEPARATE:
            kept.append(_attached(token, base))
        index += 1
    return tuple(kept)


def _tokens(entry: dict[str, object], base: Path) -> list[str]:
    """Read an entry's command line, in either shape a database writes it.

    `arguments` is already a list. `command` is one string, split without POSIX rules
    because on Windows those eat the backslashes that make up paths; surviving quotes
    are stripped by hand.

    Either shape can hand off include directories indirectly as `@some/file.rsp` -- the
    common case on Windows, where a capped ~32k command line pushes cmake into a response
    file as soon as a project has a few includes. Read without expanding it, such an entry
    looks like a compilation with no include paths at all: the same bug this module exists
    to fix, wearing a different hat.
    """
    return _expanded(_raw(entry), base)


def _raw(entry: dict[str, object]) -> list[str]:
    """The entry's own tokens, before any response file is spliced in."""
    arguments = entry.get("arguments")
    if isinstance(arguments, list):
        return [argument for argument in arguments if isinstance(argument, str)]

    command = entry.get("command")
    return _split(command) if isinstance(command, str) else []


def _split(text: str) -> list[str]:
    """Split a command line the way the shell that ran it would have, minus POSIX escaping."""
    lexer = shlex.shlex(text, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [token.strip('"') for token in lexer]


def _expanded(tokens: list[str], base: Path) -> list[str]:
    """Replace each @file argument with the arguments inside it.

    One level deep on purpose: cmake writes flat response files, and following them without
    limit would let a file that names itself hang the check that was supposed to be the
    cheap one.
    """
    expanded: list[str] = []
    for token in tokens:
        if token.startswith(RESPONSE) and len(token) > len(RESPONSE):
            expanded += _response(token[len(RESPONSE) :], base)
        else:
            expanded.append(token)
    return expanded


def _response(name: str, base: Path) -> list[str]:
    """Read one response file, or nothing when the build tree no longer has it."""
    path = Path(name)
    if not path.is_absolute():
        path = base / path
    try:
        return _split(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def _attached(token: str, base: Path) -> str:
    """Make the path inside an attached flag absolute, leaving non-path flags alone."""
    for prefix in PATH_ATTACHED:
        if token.startswith(prefix) and len(token) > len(prefix):
            return prefix + _absolute(token[len(prefix) :], base)
    return token


def _absolute(value: str, base: Path) -> str:
    """Resolve one path against the directory its entry was compiled from."""
    path = Path(value)
    return value if path.is_absolute() else str(base / path)
