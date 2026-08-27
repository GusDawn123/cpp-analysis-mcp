"""Finding identity that survives edits: content-derived fingerprints (ADR-0002).

The naive key -- rule, file, line number -- breaks the moment anyone adds a line above a
finding: everything below it "disappears" from the baseline and "appears" in the head run.
So line numbers never enter the hash. Identity is what was flagged (the rule), where (the
file), and the flagged line's text with all whitespace removed, so reformatting does not
change who a finding is. Two identical flagged lines in one file are genuinely two
findings; a dense rank over their line numbers (0, 1, ...) tells them apart while leaving
identity untouched when the whole block moves.

The tool's name is deliberately absent: clang-tidy and cppcheck reporting the same rule on
the same line must produce the same fingerprint, because equal fingerprints from different
tools are how the store recognizes independent confirmation.

Digests are SHA-256 truncated to 16 hex characters -- 64 bits. A collision would let a new
finding silently match a baseline entry and vanish, this project's named worst outcome, so
the margin is generous: by the birthday bound p ~= n^2 / 2^65, one million findings in one
store collide with probability ~3e-8. The 12-character alternative (48 bits) reaches
~2e-3 at the same scale, which is one silent suppression per few hundred large stores --
rejected. Fields are length-prefixed before hashing so adjacent fields can never trade
characters and collide by construction.

Two identity boundaries are accepted for scheme 1, both pinned by regression tests:

- Stripping all whitespace merges token spellings that differ only by internal spacing
  (`a + ++b` and `a++ + b` share a fingerprint). Collapsing runs to one space instead
  would fix that rare pair at the cost of changing identity on every reformat -- and
  reformats happen daily. Same trade SonarQube chose.
- Inserting an identical flagged line before existing duplicates rotates their occurrence
  indices, so attribution among indistinguishable duplicates can shift between runs. The
  set of fingerprints still grows by exactly the inserted finding, which is the property
  baseline subtraction needs; attribution among identical lines was never well-defined.

Both would be solved by hashing token context or the enclosing symbol -- the richer
scheme ADR-0002 defers until real-world collisions demand it. That is what bumping
SCHEME_VERSION is for.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from hashlib import sha256

from cpp_analysis_mcp.store.models import Finding

__all__ = ["SCHEME_VERSION", "compute_fingerprint", "fingerprint", "fingerprint_batch"]

# stamped into every Finding this module touches; bump it and the store re-fingerprints
# on load instead of silently orphaning suppressions and baselines (ADR-0002)
SCHEME_VERSION = 1

# how many hex characters of the SHA-256 survive -- 64 bits, per the module docstring
_DIGEST_CHARS = 16


def _strip_ws(text: str) -> str:
    # all whitespace, not just the edges: tabs-to-spaces and realignment both leave
    # identity alone, and str.split() with no argument splits on every whitespace run
    return "".join(text.split())


def _normalize_path(path: str) -> str:
    # a fingerprint computed on Windows must equal one from the container, so separators
    # canonicalize to forward slashes; case is preserved because Linux filesystems care.
    # callers hand in project-relative paths -- absolute paths would make identity depend
    # on where the repo happens to be checked out
    return path.replace("\\", "/").removeprefix("./")


def compute_fingerprint(rule: str, path: str, line_text: str, occurrence_index: int) -> str:
    """The pure primitive: canonicalize the four identity fields and hash them.

    Every field is length-prefixed as bytes before hashing, so ("ab", "c") and
    ("a", "bc") cannot meet at the same digest -- encoding ambiguity is not a
    collision source this module accepts.
    """
    parts = (rule, _normalize_path(path), _strip_ws(line_text), str(occurrence_index))
    blob = bytearray()
    for part in parts:
        encoded = part.encode("utf-8")
        blob += str(len(encoded)).encode("ascii")
        blob += b":"
        blob += encoded
    return sha256(bytes(blob)).hexdigest()[:_DIGEST_CHARS]


def fingerprint(finding: Finding, line_text: str, occurrence_index: int) -> Finding:
    """Return the finding carrying its identity; the original is left untouched.

    A finding with no location fingerprints on rule and empty file and text -- build
    failures and whole-run diagnostics are rare, and "the same rule with no location"
    being one identity is the behavior a baseline wants for them.
    """
    path = finding.location.file if finding.location is not None else ""
    digest = compute_fingerprint(finding.category, path, line_text, occurrence_index)
    return replace(finding, fingerprint=digest, fingerprint_scheme=SCHEME_VERSION)


def fingerprint_batch(
    findings: Sequence[Finding],
    read_line: Callable[[str, int], str],
) -> tuple[Finding, ...]:
    """Fingerprint a whole run, resolving occurrence indices across it.

    `read_line(file, line)` is injected rather than done here: the store reads real
    files, tests hand in sources, and this layer stays free of I/O. Occurrence indices
    are a dense rank over the distinct line numbers sharing (rule, file, stripped text),
    so the second identical flagged line is index 1 wherever the block sits -- and two
    reports of the same line share index, fingerprint, and therefore identity.

    Findings come back in the order they arrived; ranking never reorders the caller.
    """
    texts: list[str] = []
    keys: list[tuple[str, str, str]] = []
    lines: list[int] = []
    for finding in findings:
        if finding.location is None:
            text, path, line = "", "", -1
        else:
            text = _strip_ws(read_line(finding.location.file, finding.location.line))
            path = _normalize_path(finding.location.file)
            line = finding.location.line
        texts.append(text)
        keys.append((finding.category, path, text))
        lines.append(line)

    lines_by_key: dict[tuple[str, str, str], set[int]] = {}
    for key, line in zip(keys, lines, strict=True):
        lines_by_key.setdefault(key, set()).add(line)
    rank = {
        key: {line: index for index, line in enumerate(sorted(group))}
        for key, group in lines_by_key.items()
    }

    return tuple(
        fingerprint(finding, text, rank[key][line])
        for finding, text, key, line in zip(findings, texts, keys, lines, strict=True)
    )
