# ADR-0005: The escalation subsystem is retired

**Status:** Accepted · 2026-08-30

Supersedes [0003](0003-escalation-rules-as-data.md).

## Context

ADR-0003 made escalation rules declarative data: a static finding matched a
YAML rule, and the rule proposed the dynamic run that would witness the bug
("`bugprone-use-after-move` fired → verify with ASan"). It shipped a schema, a
Sigma-style lifecycle, a mandatory fixture contract, and exactly one
`experimental` rule.

A field trial on a real C++ project — a limit order book, ~17 translation
units — produced zero proposals. The one rule never matched, and no finding in
that run made a second rule worth writing.

Read back, the subsystem's only load-bearing justification was compute cost:
sanitizer minutes are expensive, so something should decide which findings
deserve them. That is an optimization. What distinguishes this product is the
dynamic verification itself and the store's cross-tool correlation, and
neither needs a rule table to work.

## Decision

Remove the subsystem — the rule module, the rule directory and its fixtures,
the `proposals` field on the review report, and the YAML dependency.

- The review gate always runs its full static tier. Nothing decides, per
  finding, what further analysis that finding deserves.
- Dynamic verification stays a deliberate step the caller invokes. The
  escalation *ladder* in the tool descriptions is untouched: it is prose about
  what each rung costs and cannot see, not a rules engine.
- Cross-tool agreement is the store's job (ADR-0002) — two engines flagging
  one defect is one finding with two confirmations, computed from
  fingerprints, no rules involved.

## Consequences

- Less code, one dependency fewer (pyyaml), and a product story that fits in a
  sentence: audit remembers, review subtracts.
- Review notes carry baseline facts only; the rule-provenance lines go with
  the rules. What ran and what was skipped is still in the plan trace, in
  full.
- If per-finding dynamic targeting is wanted again it returns as a new ADR,
  carrying field evidence: a cost that actually hurt, and findings a rule
  would have caught.
