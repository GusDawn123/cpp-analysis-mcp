# ADR-0002: Finding identity is a versioned line-hash fingerprint

**Status:** Accepted · 2026-08-27

## Context

Baseline subtraction ("show only findings this change introduced"), suppression
persistence, escalation cooldown, and cross-tool correlation all require
answering: *is this finding the same finding as that one?* — across runs, and
across edits that move code around.

The naive key `(rule, file, line)` fails immediately: add one line at the top
of a file and every finding below it "disappears" from the baseline and
"appears" in the head run. A diff against `main` would report dozens of false
positives from a one-line edit.

Two production systems were studied:

- **SonarQube** matches issues across analyses by `(rule id, hash of the
  whitespace-stripped flagged line)`, tolerant of the flagged block moving
  within the file. Battle-tested for over a decade of PR analysis.
- **CodeChecker** offers multiple selectable report-hash strategies and
  recommends `context-free-v2` because it survives re-indentation *and checker
  renames* — the scheme itself is versioned.

## Decision

Adopt SonarQube's algorithm as the base and CodeChecker's versioning
discipline on top:

```
fingerprint = hash(rule_id, relative_path, strip_ws(flagged_line_text), occurrence_index)
```

- `occurrence_index` disambiguates identical flagged lines within one file
  (the same `strip_ws` text appearing twice is two findings, counted in file
  order).
- Line *numbers* never enter the hash. Content moves; identity follows it.
- Every stored fingerprint carries a `scheme_version`. A future algorithm
  change re-fingerprints the store on load rather than silently orphaning
  suppressions and baselines.
- The same fingerprint computed from two different tools' findings is the
  correlation key: one finding, multiple confirmations. Cross-tool rule
  equivalence (clang-tidy and cppcheck naming the same defect differently) is
  a store-level mapping table, versioned like the scheme.

A richer scheme (enclosing symbol, context lines) was considered and rejected
for v1: it adds parser burden for every analyzer, and the simpler scheme's
collision mode (two identical lines in one file, disambiguated by index) is
already handled. Revisit only if real-world collisions demand it — that is
what `scheme_version` is for.

## Consequences

- Baselines survive reformatting, reordering, and unrelated edits.
- Suppressions and escalation cooldowns are durable across runs.
- Renamed checks break identity unless added to the equivalence table —
  a maintenance duty accepted in exchange for scheme simplicity.
- Fingerprinting is a pure function over parsed findings: exhaustively unit-
  testable with no toolchain present.
