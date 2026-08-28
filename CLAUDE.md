# CLAUDE.md — how this repo is worked

Instructions for Claude (any machine, any session). These encode the maintainer's
standing decisions; they override default behavior. When they conflict with something
newer the maintainer says, the maintainer wins — then update this file.

## Read first, always

- `docs/getting-started.md` — the contributing workflow lives at the bottom. It was
  missed once; it is never missed again.
- `git branch -r` before assuming anything about branches. This repo had in-flight
  feature branches a session once ignored.
- `docs/architecture.md` (v1 layers, four rules) and `docs/architecture-v2.md`
  (five-layer target: surface / planner / analyzers / store / engines).
- ADRs in `docs/adr/` are binding decisions, each with its precedents.

## Source control (non-negotiable)

- git-flow: `feat/...` branches fork from `develop`, PRs target `develop`, green CI
  required. `main` only receives merges from `develop` at release.
- **Never merge a PR.** The maintainer merges, or delegates explicitly per-PR.
- One branch per chunk, one conventional commit per sub-chunk.
- Commit/PR titles: `type(scope): imperative summary` (feat, fix, docs, refactor,
  chore, test). Never internal plan vocabulary ("Phase 1", "Chunk 1.2") in titles —
  plan words live in specs and milestones, not git history.

## The working ritual (per sub-chunk, six steps, no skipping)

1. **Plan** — goal, files touched, interface changes, test plan, performance notes
   (data structures with complexity stated), risks and judgment calls.
2. **Refine** — the maintainer tears it up; nothing is written before their blessing.
3. **Implement** — tests first, code second, in the repo's voice (narrative, reasoned
   docstrings; comments say *why*; match what surrounds you).
   - **3.5 Self-review** — a deliberate pass for simplicity, naming, dead weight,
     idiom-match, before any external review.
4. **Review** — CodeRabbit CLI on exactly that diff (`coderabbit review --agent -t
   uncommitted`). Code diffs only — never run it on docs-only changes.
5. **Fix** — verify every finding against the code before acting; accept what is
   real, reject with a *recorded reason* (docstring, test, or commit message) what
   is not. Findings are data, not orders.
6. **Commit** — one conventional commit whose message tells the story, including
   design corrections and rejected-finding reasons.

Chunk completion: full suite + goldens green → PR to `develop` linking the chunk's
spec → the maintainer merges.

## Quality bars

- Quality is the hard constraint; latency is the priority. When they conflict,
  quality wins.
- **Never O(n²) on a hot path.** State complexity in plans; dicts/sets over scans.
- Latency tests ride the unit suite and run every increment — hot paths get
  benchmarked bounds with ~20x headroom so CI noise cannot flake them. Do not move
  them to an opt-in job.
- Never tail-pipe or truncate build/test output when diagnosing — capture whole,
  then grep.
- Where a change declares "zero behavior change," it is an acceptance bar enforced
  by the existing suite, not a hope.
- `make fmt` then `make all` before every commit; `make integration` before pushing.

## Architecture rules (enforced by tests where possible)

- Imports point down: surface → planner → analyzers → store → engines. A file's
  directory answers "what layer is this?"
- Analyzer plugins never import `server`, `context`, `pipelines`, or the MCP SDK
  (`tests/unit/test_architecture.py` enforces this). Their tooling arrives as
  injected callables; no toolchain discovery at import time.
- The registry stays minimal (register / analyzers / resolve) and analyzer-agnostic —
  it knows names, tiers, and gates, never tools.
- The only LLM in the system is the calling agent. No layer holds an API key.
- Refusals explain themselves: every gate that says no says why, in words.
- No false all-clear, ever: an unconsulted probe reads as unavailable; a detector
  that was not watching says so.

## Frozen decisions (change = new ADR + explicit maintainer approval)

- **`Finding` schema is frozen** (ADR-0002). Adjacent types (`AnalyzerRun`,
  `FileOutcome`) must not grow fields that duplicate or paraphrase it.
- **Fingerprint encoding is normative** (ADR-0002): bit-for-bit spec with pinned
  known-answer digests. Any change — including adding a rule-equivalence mapping —
  is a scheme bump, never a refactor.
- **"Fingerprint" means two things on purpose**: finding identity
  (`store/fingerprints.py`) and profile patterns (`Fingerprint` in the shared
  vocabulary, which has seniority). No rename without an ADR.
- **Escalation rules are data, never code** (ADR-0003): YAML with a Sigma-style
  lifecycle, propose-by-default, fixture tests mandatory.
- **The planner is deterministic code, never an LLM** (ADR-0001).

## Current state pointers

- Roadmap: `docs/architecture-v2.md` (migration phases) and
  `docs/superpowers/specs/2026-08-27-phase-1-chunk-map.md` (the ritual applied).
- Open design questions the maintainer wants input on: `docs/open-questions.md`.
- Milestones on GitHub track phases; PRs attach to them.
