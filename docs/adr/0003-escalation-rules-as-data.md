# ADR-0003: Escalation rules are declarative data with a lifecycle

**Status:** Accepted · 2026-08-27

## Context

The planner's escalation step — a static finding triggering a proposal to run
an expensive dynamic tool ("`bugprone-use-after-move` fired → verify with
ASan") — has no productized precedent. None of the mature orchestrators
studied (CodeRabbit, MegaLinter, Trunk, CodeChecker, SonarQube, pre-commit)
does it. The nearest prior art:

- **Academic SAST-guided fuzzing** (SASTFuzz): static findings direct an
  expensive dynamic tool at the *flagged functions*, not the whole target —
  the value comes from narrowing scope, not from breadth.
- **Sigma rules** (SIEM detection-as-data): declarative YAML with a UUID, a
  severity, and a `status` lifecycle (`experimental → test → stable →
  deprecated`), versioned in git, continuously evaluated for false positives.
- **SOAR playbooks** (alert → response): three execution models — automatic,
  semi-automatic behind analyst approval, manual — with the governing rule
  that *anything with real blast radius stops and waits for a person*.
- **Kudelski Security's CodeRabbit RCE**: a user-supplied tool config that
  could execute code at init time (`.rubocop.yml` `require:`) became remote
  code execution. Configuration that can execute is an attack surface.

## Decision

Escalation rules are **YAML data — never code**. Schema (full spec:
`superpowers/specs/2026-08-27-escalation-rule-schema.md`):

```yaml
id: <uuid4>
title: use-after-move → verify with ASan
status: stable            # experimental | stable | deprecated
when:
  tool: clang-tidy
  rules: [bugprone-use-after-move]
  min_severity: medium
  min_count: 1
then:
  run: asan
  scope: enclosing_target # smallest reproducible unit — never repo-wide
  action: propose         # propose | auto | off
provenance: {author, created, modified, references}
```

Adopted from the precedents:

1. **Lifecycle (Sigma).** New rules ship `experimental` and are *incapable* of
   `action: auto` — they propose only. Promotion to `stable` requires evidence
   (see 3). Users override per rule in `.cpp-analysis.toml`.
2. **Blast-radius gating (SOAR).** Minutes of sanitizer compute is blast
   radius: `propose` is the default action; `auto` is an explicit opt-in per
   rule per project.
3. **Rule performance is data.** Every escalation records whether the dynamic
   run *confirmed* the static finding. Per-rule confirm/refute ratios live in
   project state; a rule whose escalations never confirm is measurable noise
   and a demotion candidate.
4. **Fixture tests are mandatory (Sigma's FP/FN discipline, made CI).** Every
   rule ships with findings that must trigger it and near-misses that must
   not. A rule without fixtures does not merge.
5. **Cooldown by fingerprint.** A proposal declined, or an escalation already
   run, for finding fingerprint X is not re-raised on the next run
   (ADR-0002 provides the identity).
6. **No expressiveness creep.** The `when` clause is matching, not
   computation: no templating, no embedded expressions, no user code paths.
   The Kudelski incident is the standing reason.

Every fired, skipped, and cooled-down rule appears in the plan trace by ID.

## Consequences

- Escalation coverage grows by adding rules — reviewable one YAML file at a
  time, testable by fixtures, shippable as versioned rule packs.
- The planner remains deterministic (ADR-0001): rule evaluation is pure
  matching.
- Deliberately limited expressiveness means some sophisticated triggers
  ("escalate only if the flagged function is reachable from a thread entry
  point") cannot be expressed. Accepted; revisit with evidence, not
  speculation.
