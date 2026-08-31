"""Where every tool's reports become one set of facts (ADR-0002, architecture v2 layer 2).

In-memory and pure; suppression hides but never deletes, so the complete record stays readable.
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
    """One run's findings in a fingerprint-keyed dict: ingest and lookup are O(1) per
    finding, `new_since` an O(n + m) key-difference walk. Insertion order is preserved
    wherever order is unspecified, so the same ingests always read back the same way.
    """

    def __init__(self) -> None:
        self._by_fingerprint: dict[str, Finding] = {}
        self._suppressed: set[str] = set()

    def ingest(
        self,
        findings: Sequence[Finding],
        read_line: Callable[[str, int], str],
        *,
        canonical: Callable[[str], str] | None = None,
    ) -> None:
        """Fold one run in whole: occurrence indices resolve in a single `fingerprint_batch`
        call, so a split run would wrongly merge identical duplicate lines. Same-tool repeats
        grow the count; another tool attaches at most one Confirmation, evidence unchanged.
        """
        for stamped in fingerprint_batch(findings, read_line, canonical=canonical):
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
        """Everything on record, in arrival order; suppressed entries opt in, because
        suppression must stay inspectable to be trustworthy.
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
        """The findings whose identity the baseline set never saw -- new_since's twin for
        baselines loaded from disk. Set-shaped on purpose: membership stays O(1) per finding.
        """
        return tuple(
            finding
            for fingerprint, finding in self._by_fingerprint.items()
            if fingerprint not in baseline and fingerprint not in self._suppressed
        )

    def new_since(self, baseline: "FindingStore") -> tuple[Finding, ...]:
        """The findings this store has and the baseline does not -- the review gate.

        Fingerprints match a finding that moved or reformatted, so only genuine news survives.
        """
        return self.new_against(baseline._by_fingerprint.keys())

    def suppress(self, fingerprints: Iterable[str]) -> None:
        """Hide these identities from queries without touching the record."""
        self._suppressed.update(fingerprints)

    def ranked(self) -> tuple[Finding, ...]:
        """Severity first, then variety: within a band, findings round-robin across files
        so one noisy file cannot crowd out the rest for a token-budgeted reader who sees
        only the top of this list. Ordering is stable for a given ingest history.
        """
        bands: dict[int, dict[str, list[Finding]]] = {}
        for finding in self.findings():
            band = bands.setdefault(_SEVERITY_RANK[finding.severity], {})
            place = finding.location.file if finding.location is not None else ""
            band.setdefault(place, []).append(finding)

        # a (bucket, next index) queue keeps the round-robin linear; rescanning every
        # bucket per pass would go quadratic when one file holds most of the findings
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
