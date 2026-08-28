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

## Normative encoding (scheme 1)

This section is the specification an independent implementation reproduces
bit-for-bit. `tests/unit/store/test_store_fingerprints.py` holds a
spec-literal reimplementation of it plus pinned known-answer digests; a change
to either side of that equality is a scheme change, not a refactor.

**Fields, in this order:**

1. the rule id (the finding's `category`, verbatim)
2. the normalized path
3. the stripped flagged-line text
4. the occurrence index, rendered as a decimal string with no sign or padding

**Normalization:**

- *Path:* replace every backslash (`\`, U+005C) with a forward slash (`/`,
  U+002F); then remove one leading `./` if present. Case is preserved. Paths
  are project-relative by caller contract.
- *Text:* remove every whitespace character, where whitespace is what Python's
  `str.split()` splits on (the Unicode White_Space set). No other character is
  altered.
- A finding with no location contributes an empty path and empty text.

**Encoding and digest:** encode each normalized field as UTF-8; prefix each
with the ASCII decimal byte length of its encoding followed by one colon
(U+003A); concatenate the four prefixed fields in order; hash with SHA-256;
render as lowercase hexadecimal; keep the first 16 characters.

**Worked example** — rule `bugprone-use-after-move`, path
`src/order_book.cpp`, text `process(std::move(order));`, index 0 produces the
byte string:

```text
23:bugprone-use-after-move18:src/order_book.cpp26:process(std::move(order));1:0
```

whose SHA-256 begins `e56adf7bdc0bf0a3` — the fingerprint.

## Scheme identity includes the equivalence table

`fingerprint_scheme` identifies **both** the algorithm above **and** the
version of the cross-tool rule-equivalence table applied before hashing
(scheme 1: no table — categories hash verbatim). This is load-bearing:
introducing or amending a mapping rewrites identities for every finding whose
category it touches, and doing so under an unchanged scheme number would
silently orphan baselines and suppressions with no warning. Adding or
changing a mapping is therefore a scheme bump. Persisted stores and caches
record the scheme that produced them; on mismatch the reader re-fingerprints
from source — schemes are never mixed within one comparison.

## Finding schema change control

The `Finding` schema is frozen as of 2026-08-27 (fourteen fields, including
`symbol` and `confirmations`, which are partially unused today — that is
accepted). Any addition, removal, or retyping of a field requires a new ADR
and the maintainer's explicit approval before implementation. Adjacent types
(`AnalyzerRun`, `FileOutcome`) reference `Finding` and the shared report
vocabulary; they must never grow fields that duplicate or paraphrase
`Finding`'s — a shadow schema is a schema change without the ADR.

## Two meanings of "fingerprint" — deliberate, not accidental

This codebase uses the word twice (maintainer decision, 2026-08-27):

- **Finding identity** — this ADR: the `fingerprint`/`fingerprint_scheme`
  fields on `Finding` and the `store/fingerprints.py` module. A content hash
  answering "is this the same finding as that one?"
- **Profile patterns** — the `Fingerprint` class in the shared vocabulary and
  `cpp_analysis_mcp/fingerprints.py`: a recognized pattern in a profile's
  time distribution, with rewrite candidates. Predates this ADR and has
  seniority.

The namespace split (store vs. package top level) keeps them apart in code.
No rename: the `Finding` schema is frozen per the section above, and renaming
the profile concept would churn shipped surface. Revisit only through an ADR
if the ambiguity starts costing real confusion.

## Consequences

- Baselines survive reformatting, reordering, and unrelated edits.
- Suppressions and escalation cooldowns are durable across runs.
- Renamed checks break identity unless added to the equivalence table — and
  adding one is a scheme bump, per above.
- Fingerprinting is a pure function over parsed findings: exhaustively unit-
  testable with no toolchain present, and reproducible from this document
  alone.
