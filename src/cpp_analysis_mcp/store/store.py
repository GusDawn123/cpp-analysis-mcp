"""Where every tool's reports become one set of facts (ADR-0002, architecture v2 layer 2).

In-memory and pure: no git, no filesystem, no persistence -- those belong to the scope
resolver and caches of later phases. Suppression hides; it never deletes, so the
complete record stays readable, in the same spirit as raw tool logs surviving on disk.
"""

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import replace

from cpp_analysis_mcp.store.fingerprints import fingerprint_batch
from cpp_analysis_mcp.store.models import Confirmation, Finding, Severity

__all__ = ["FindingStore"]

# ranking order: what breaks the build outranks what warns, which outranks what remarks
_SEVERITY_RANK: Mapping[Severity, int] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.NOTE: 2,
}


class FindingStore:
    """One run's findings, indexed by identity.

    The primary index is a dict keyed by fingerprint: ingest and lookup are O(1) per
    finding, and `new_since` is a key-difference walk, O(n + m) across two stores.
    Insertion order is preserved everywhere order is not otherwise specified, so the
    same reports ingested in the same order always read back the same way -- the
    determinism claim, applied to a data structure.
    """

    def __init__(self) -> None:
        self._by_fingerprint: dict[str, Finding] = {}
        self._suppressed: set[str] = set()

    def ingest(
        self,
        findings: Sequence[Finding],
        read_line: Callable[[str, int], str],
    ) -> None:
        """Fingerprint one run's findings and fold them in.

        One run enters whole: occurrence indices resolve within a single call to
        `fingerprint_batch`, so splitting a run across several ingests would let
        identical duplicate lines land on the same index and wrongly merge.

        A repeat from the tool that already reported a fingerprint grows its
        occurrence count. A report from a *different* tool attaches a Confirmation
        and nothing else -- the first tool's evidence stays the finding of record,
        and a tool confirms any one finding at most once.
        """
        for stamped in fingerprint_batch(findings, read_line):
            existing = self._by_fingerprint.get(stamped.fingerprint)
            if existing is None:
                self._by_fingerprint[stamped.fingerprint] = stamped
            elif stamped.tool == existing.tool:
                self._by_fingerprint[stamped.fingerprint] = replace(
                    existing, occurrences=existing.occurrences + stamped.occurrences
                )
            elif all(seen.tool != stamped.tool for seen in existing.confirmations):
                confirmed = (*existing.confirmations, Confirmation(stamped.tool, stamped.id))
                self._by_fingerprint[stamped.fingerprint] = replace(
                    existing, confirmations=confirmed
                )

    def findings(self, *, include_suppressed: bool = False) -> tuple[Finding, ...]:
        """Everything on record, in the order it arrived; suppressed entries opt in.

        The flag exists because suppression must be inspectable to be trustworthy:
        hidden findings keep merging new reports, and only a reader who can still see
        them can verify nothing was destroyed.
        """
        return tuple(
            finding
            for fingerprint, finding in self._by_fingerprint.items()
            if include_suppressed or fingerprint not in self._suppressed
        )

    def identities(self, *, include_suppressed: bool = False) -> frozenset[str]:
        """The fingerprint set on record -- what a persisted baseline is made of."""
        return frozenset(
            fingerprint
            for fingerprint in self._by_fingerprint
            if include_suppressed or fingerprint not in self._suppressed
        )

    def new_against(self, baseline: AbstractSet[str]) -> tuple[Finding, ...]:
        """The findings whose identity the given baseline set never saw.

        The set-shaped twin of new_since, for baselines loaded from disk: only
        identities persist across runs, never the findings that carried them.
        Set-shaped on purpose -- membership must stay O(1) per finding.
        """
        return tuple(
            finding
            for fingerprint, finding in self._by_fingerprint.items()
            if fingerprint not in baseline and fingerprint not in self._suppressed
        )

    def new_since(self, baseline: "FindingStore") -> tuple[Finding, ...]:
        """The findings this store has and the baseline does not -- the review gate.

        Identity is the fingerprint, so a baseline finding that moved, reformatted,
        or reordered still matches, and only genuinely new findings survive the
        subtraction. Suppressed findings are not news either way.
        """
        return self.new_against(baseline._by_fingerprint.keys())

    def suppress(self, fingerprints: Iterable[str]) -> None:
        """Hide these identities from queries without touching the record."""
        self._suppressed.update(fingerprints)

    def ranked(self) -> tuple[Finding, ...]:
        """Severity first, then variety: every place is heard from before any repeats.

        Within one severity band the findings round-robin across files -- five
        variations of one bug in one file must not crowd out the first report from
        four other files, because a reader with a token budget sees the top of this
        list and maybe nothing else. Ordering is stable for a given ingest history.
        """
        bands: dict[int, dict[str, list[Finding]]] = {}
        for finding in self.findings():
            band = bands.setdefault(_SEVERITY_RANK[finding.severity], {})
            place = finding.location.file if finding.location is not None else ""
            band.setdefault(place, []).append(finding)

        # a queue of (bucket, next index) makes the round-robin linear: each finding is
        # emitted exactly once and each bucket requeued only while it has more to say --
        # a depth-based rescan of every bucket per pass would go quadratic when one file
        # holds most of the findings
        out: list[Finding] = []
        for rank in sorted(bands):
            queue: deque[tuple[list[Finding], int]] = deque(
                (bucket, 0) for bucket in bands[rank].values()
            )
            while queue:
                bucket, index = queue.popleft()
                out.append(bucket[index])
                if index + 1 < len(bucket):
                    queue.append((bucket, index + 1))
        return tuple(out)
