"""Finding identity that survives edits: content-derived fingerprints (ADR-0002).

Line numbers never enter the hash -- content moves, identity follows it; see ADR-0002 for
the full scheme and its normative encoding. SHA-256 truncated to 16 hex chars (64 bits)
keeps collision probability negligible even at a million findings (~3e-8, by the birthday
bound); the 12-character alternative reaches ~2e-3 at the same scale and was rejected.
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
    # all whitespace, not just the edges: str.split() with no argument splits on every
    # whitespace run. Accepted collision: "a + ++b" and "a++ + b" then share a
    # fingerprint -- collapsing runs to one space instead would avoid that but change
    # identity on every reformat, which happens daily.
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
    being one identity is the behavior a baseline wants for them. The text argument is
    ignored for those on purpose: the spec says locationless findings contribute empty
    text, and which entry point computed a fingerprint must never change it.
    """
    if finding.location is None:
        path, line_text = "", ""
    else:
        path = finding.location.file
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
