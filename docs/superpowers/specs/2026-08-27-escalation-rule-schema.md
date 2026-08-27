# Escalation rule schema

The planner's escalation table is data: YAML rules that map static findings to
proposed dynamic verification. This spec defines the format. The reasoning —
why data and not code, why a lifecycle, why propose-by-default — is
ADR-0003; this file is the contract an implementation and its fixtures are
tested against.

## A complete rule

```yaml
id: 4f0a2c1e-9a2b-4b7e-8c3d-6f5e1d2a9b0c
title: use-after-move → verify with ASan
description: >
  bugprone-use-after-move has a low false-positive rate and its bug class is
  exactly what AddressSanitizer witnesses at runtime. Worth a build.
status: stable

when:
  tool: clang-tidy
  rules:
    - bugprone-use-after-move
    - bugprone-dangling-handle
  min_severity: medium
  min_count: 1

then:
  run: asan
  scope: enclosing_target
  action: propose

provenance:
  author: gustavo
  created: 2026-08-27
  modified: 2026-08-27
  references:
    - https://clang.llvm.org/extra/clang-tidy/checks/bugprone/use-after-move.html
```

## Field semantics

### Top level

| Field         | Required | Meaning                                                        |
| ------------- | -------- | -------------------------------------------------------------- |
| `id`          | yes      | UUID4, globally unique, never reused — the cooldown and metrics key |
| `title`       | yes      | One line, `<trigger> → <action>` shape, shown in the plan trace |
| `description` | yes      | Why this escalation earns its cost — a human argument, not a restatement |
| `status`      | yes      | `experimental` \| `stable` \| `deprecated` (lifecycle below)   |
| `when`        | yes      | Match clause — pure matching, no computation                   |
| `then`        | yes      | What to run, on what, how automatically                        |
| `provenance`  | yes      | Author, dates, references                                      |

### `when` — the match clause

| Field          | Required | Meaning                                                     |
| -------------- | -------- | ----------------------------------------------------------- |
| `tool`         | yes      | Analyzer name whose findings this rule inspects             |
| `rules`        | yes      | List of rule/check IDs; a finding matches if its rule is in the list |
| `min_severity` | no       | Findings below this severity don't count (default: any)     |
| `min_count`    | no       | Matching findings required **within one unit of work** before the rule fires (default 1). `min_count: 2` on lock-related checks means "two lock findings in the same TU", not "two anywhere in the repo" |

Deliberately absent: regexes over messages, templating, arithmetic,
conditionals, includes. The `when` clause matches; it never computes
(ADR-0003, consequence of the Kudelski incident).

### `then` — the action

| Field    | Required | Meaning                                                          |
| -------- | -------- | ---------------------------------------------------------------- |
| `run`    | yes      | Target analyzer: `asan` \| `tsan` \| `lsan` \| `ubsan`           |
| `scope`  | yes      | `enclosing_target` (the build target owning the flagged TU) \| `translation_unit`. Never repo-wide — escalation narrows (SASTFuzz discipline) |
| `action` | yes      | `propose` (default posture) \| `auto` \| `off`. `experimental` rules are clamped to `propose` regardless of this field |

## Lifecycle

```
experimental ──(evidence)──▶ stable ──(retired)──▶ deprecated
```

- **experimental**: newly authored. Proposes only — `action: auto` is ignored
  with a plan-trace note. Ships in the rule pack but announces its status.
- **stable**: promoted when its record supports it. The promotion bar
  (confirm-ratio threshold, minimum escalation count) is an open question
  below.
- **deprecated**: kept for fingerprint/cooldown continuity, never fires.
  Rules are deprecated, not deleted — their `id` stays reserved.

## Evaluation semantics

1. The planner evaluates rules **after** the static tier of a run completes,
   against the store's *new* findings for the current scope (baseline-
   subtracted — pre-existing findings never trigger escalation).
2. All matching rules fire; identical `(run, scope-unit)` proposals from
   different rules merge into one proposal citing both rule IDs.
3. **Cooldown:** a proposal keyed by `(rule id, finding fingerprint)` that was
   declined, or an escalation that already ran for that key, is suppressed
   with a plan-trace note. Cooldown state lives in project state, keyed by
   fingerprints (ADR-0002), and clears when the finding itself clears.
4. Every fired, merged, clamped, and cooled-down rule appears in the plan
   trace by `id`.

## Metrics

Each executed escalation records to project state:

```
(rule id, finding fingerprint, ran at, outcome: confirmed | not-confirmed | errored)
```

"Confirmed" means the dynamic run produced a finding correlated (by location
or fingerprint equivalence) with the triggering static finding. Per-rule
confirm/refute ratios are readable via `capabilities`/project reporting and
are the evidence for lifecycle transitions — rules earn `stable` and lose it
on their record, not on intuition.

## Configuration precedence

Explicit stack, later wins (pre-commit's model):

1. Shipped rule pack defaults
2. Project `.cpp-analysis.toml` (`[escalation]`: disable rules by id, flip
   `propose`→`auto` for stable rules, cap concurrent escalations)
3. Per-call tool arguments (an agent may decline escalation entirely for a run)

A project can *demote* a rule's action; only explicit project config can
*promote* one to `auto`, and never for `experimental` rules.

## Fixture contract

A rule without fixtures does not merge. Per rule, under
`planner/rules/fixtures/<rule-id>/`:

- `triggers/` — parsed-finding fixtures that MUST fire the rule
- `near_misses/` — fixtures that MUST NOT (wrong check, below severity, below
  count, wrong tool)

CI evaluates every rule against its fixtures on every run — the same
plant-a-known-bug-and-assert discipline the golden files use.

## Open questions

- **Promotion bar:** what confirm-ratio and minimum-N earn `stable`?
  Placeholder thinking: ≥3 executed escalations, ≥⅓ confirmed — deliberately
  low because a confirmed race is worth many refuted proposals. Needs real
  data.
- **Initial rule pack:** which clang-tidy checks map to which sanitizers with
  defensible descriptions? First candidates: use-after-move/dangling-handle →
  ASan; thread-safety-analysis + concurrency-* → TSan; signed-overflow-adjacent
  bugprone checks → UBSan. Each needs its own argued `description`.
- **Cross-tool triggers:** may a rule match on correlated findings ("flagged
  by both clang-tidy and cppcheck")? Deferred — needs the store's equivalence
  table (ADR-0002) to exist first.
