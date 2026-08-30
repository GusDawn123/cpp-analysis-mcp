"""Find the compilation database near a source file, and read the flags it needs to parse.
Takes only what decides whether a file parses -- include directories, macro definitions,
the language standard -- never optimization, warnings, sanitizers, or output paths.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterator
from pathlib import Path

DATABASE = "compile_commands.json"

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
    """The database that best covers `source`: the nearest one that actually names this
    file, else the nearest that exists at all -- a header appears in none, and its include
    directories are the same as its neighbours'.
    """
    found = list(_candidates(source))
    for candidate in found:
        if _entry(candidate, source) is not None:
            return candidate
    return found[0] if found else None


def find_under(root: Path) -> Path | None:
    """The database a project keeps at its root: beside it, or in a build tree there. Never
    walks upward -- a parent checkout's database describes someone else's build.
    """
    return next(iter(databases_under(root)), None)


def databases_under(root: Path) -> tuple[Path, ...]:
    """Every database at the root, best first: a checkout commonly holds several build
    trees, so a report naming the one it read needs to know how many it chose between.
    """
    beside = root / DATABASE
    found = sorted(root.glob(f"{BUILD_GLOB}/{DATABASE}"))
    return (beside, *found) if beside.is_file() else tuple(found)


def sources(database: Path) -> tuple[Path, ...]:
    """The absolute path of every file the database names, in its own order. Relative
    entries resolve against their entry's `directory`; malformed ones contribute nothing.
    """
    named: list[Path] = []
    for entry in _entries(database):
        file = entry.get("file")
        if not isinstance(file, str):
            continue
        directory = entry.get("directory")
        base = Path(directory) if isinstance(directory, str) else Path.cwd()
        named.append(Path(_absolute(file, base)))
    return tuple(named)


def flags_for(database: Path, source: Path) -> tuple[str, ...]:
    """The flags this file needs to parse: its own entry when there is one, else -- every
    header's case -- the include and define flags of every entry, deduped. Wider is the
    safe direction: an extra -I changes nothing, a missing one stops the parse.
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
        # sorted so multiple build trees are read in one order, not whichever the filesystem returns
        yield from sorted(path for path in directory.glob(f"{BUILD_GLOB}/{DATABASE}"))


def _entries(database: Path) -> list[dict[str, object]]:
    """Read the database, or nothing at all when it cannot be read: a database we cannot
    parse is a database we do not have, and the check still runs without one.
    """
    try:
        document: object = json.loads(database.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(document, list):
        return []
    return [entry for entry in document if isinstance(entry, dict)]


def _entry(database: Path, source: Path) -> dict[str, object] | None:
    """Find this file's own entry, comparing paths as the filesystem would: a database
    writes whatever spelling the build used -- forward slashes on Windows, a symlink --
    so the resolved paths are comparable and the strings are not.
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
    """Read an entry's command line, in either shape a database writes it. `command` splits
    without POSIX rules -- those eat the backslashes in a Windows path -- and @file response
    files are expanded, or an entry compiled through one looks like it has no includes.
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
    """Replace each @file argument with the arguments inside it -- one level deep, so a
    response file that names itself cannot hang the check that was supposed to be cheap.
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
