"""Finding identity that survives edits: content-derived fingerprints (ADR-0002 holds the
normative encoding). Line numbers never enter the hash -- content moves, identity follows.
SHA-256 truncated to 16 hex chars keeps collision odds ~3e-8 at a million findings.
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
    # all whitespace, not just the edges: str.split() splits on every whitespace run.
    # Accepted collision: "a + ++b" and "a++ + b" share a fingerprint -- collapsing runs
    # to one space would avoid that but change identity on every reformat
    return "".join(text.split())


def _normalize_path(path: str) -> str:
    # a fingerprint computed on Windows must equal one from the container, so separators
    # canonicalize to forward slashes; case is preserved because Linux filesystems care.
    # Callers hand in project-relative paths -- absolute would tie identity to the checkout
    return path.replace("\\", "/").removeprefix("./")


def compute_fingerprint(rule: str, path: str, line_text: str, occurrence_index: int) -> str:
    """The pure primitive: canonicalize the four identity fields and hash them. Every
    field is length-prefixed as bytes, so ("ab", "c") and ("a", "bc") cannot meet at the
    same digest -- encoding ambiguity is not an accepted collision source.
    """
    parts = (rule, _normalize_path(path), _strip_ws(line_text), str(occurrence_index))
    blob = bytearray()
    for part in parts:
        encoded = part.encode("utf-8")
        blob += str(len(encoded)).encode("ascii")
        blob += b":"
        blob += encoded
    return sha256(bytes(blob)).hexdigest()[:_DIGEST_CHARS]


def fingerprint(
    finding: Finding,
    line_text: str,
    occurrence_index: int,
    *,
    canonical: Callable[[str], str] | None = None,
) -> Finding:
    """Return the finding carrying its identity; the original is left untouched. A finding
    with no location hashes its rule with empty path and text -- the spec's rule, so the
    text argument is ignored for those. `canonical` rewrites only the path entering the hash.
    """
    if finding.location is None:
        path, line_text = "", ""
    elif canonical is None:
        path = finding.location.file
    else:
        path = canonical(finding.location.file)
    digest = compute_fingerprint(finding.category, path, line_text, occurrence_index)
    return replace(finding, fingerprint=digest, fingerprint_scheme=SCHEME_VERSION)


def fingerprint_batch(
    findings: Sequence[Finding],
    read_line: Callable[[str, int], str],
    *,
    canonical: Callable[[str], str] | None = None,
) -> tuple[Finding, ...]:
    """Fingerprint a whole run, resolving occurrence indices across it. `read_line` is
    injected so this layer stays free of I/O. Indices are a dense rank over the distinct
    line numbers sharing (rule, file, stripped text), so identity survives blocks moving.
    """
    texts: list[str] = []
    keys: list[tuple[str, str, str]] = []
    lines: list[int] = []
    for finding in findings:
        if finding.location is None:
            text, path, line = "", "", -1
        else:
            text = _strip_ws(read_line(finding.location.file, finding.location.line))
            file = finding.location.file if canonical is None else canonical(finding.location.file)
            path = _normalize_path(file)
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
        fingerprint(finding, text, rank[key][line], canonical=canonical)
        for finding, text, key, line in zip(findings, texts, keys, lines, strict=True)
    )
