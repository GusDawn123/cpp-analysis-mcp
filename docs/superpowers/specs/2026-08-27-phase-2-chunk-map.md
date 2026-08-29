# Phase 2 chunk map — the planner and the review gate

Phase 2 turns the Phase 1 groundwork into the product's first real face: an
agent runs one tool call before declaring work done, and sees only the findings
its change introduced, with a trace explaining what ran, what was skipped, and
why. Four chunks: resolve scope, plan deterministically, remember baselines,
and expose `review()` / `audit()` / `get_finding()` on the surface.

At the end of the phase the existing tools still behave identically, and the
new surface is demonstrable on a fixture project: plant a bug, call `review()`,
get exactly the planted finding back as new — with the plan trace attached.

Later phases get their own maps when we reach them.

## Working protocol

The CLAUDE.md ritual, binding for every sub-chunk: plan → refine (nothing is
written before the maintainer's blessing) → implement tests-first with a 3.5
self-review → CodeRabbit on exactly that diff → fix findings-as-data → one
conventional commit. One branch per chunk, PRs to `develop`, the maintainer
merges. Latency bounds ride the unit suite with ~20x headroom.

## Chunk 2.1 — scope resolver

Branch: `feat/p2c1-scope-resolver`. New package: `planner/`.

### 2.1a — resolved scope and path canon

A `ResolvedScope`: the caller's intent (named files, a diff ref, or the whole
project) turned into concrete project-relative POSIX paths, exactly once. Every
path entering fingerprinting becomes relative here — closing the deferral
recorded in 1.2d, where absolute paths were accepted because no resolver owned
relativization yet. Performance: pure path arithmetic, O(files), no I/O.

### 2.1b — git-aware scope

"What changed since `ref`" via the injected `Runner` running `git diff
--name-status` (a subprocess like every other tool — no git library
dependency). Handles renames, deletes, untracked files, and paths outside the
project root. Test plan: a scripted fake runner serves captured git output;
integration proves it against a real repo. A machine without git refuses in
words; it never silently widens to a full scan.

### 2.1c — the resolver feeds the registry

`AnalyzerContext` (translation units, capabilities) is built by the resolver in
one place, from the compilation database nearest the scope, instead of ad hoc
in each pipeline. Zero behavior change on existing tools is the bar.

## Chunk 2.2 — the planner

Branch: `feat/p2c2-planner`. ADR-0001 applies: deterministic code, never an LLM.

### 2.2a — plan and trace models

Frozen dataclasses: a `Plan` (units of work ordered by cost tier) and a
`PlanTrace` (every analyzer's gate verdict — ran on what, skipped and why, in
words). The trace is emitted before anything executes; refusals explain
themselves. No overlap with the frozen `Finding` schema.

### 2.2b — the scheduler

Gate chain → cost-tier ordering → dispatch. Parallel within a tier, and results
are re-sorted by (tier, analyzer name, file) so completion order can never leak
into output order — same inputs, same plan, same report, every run.
Performance: bookkeeping is O(n) over units of work; a latency test plans a
10,000-file scope under a bounded time.

### 2.2c — escalation proposals (mechanism only)

The ADR-0003 YAML table loads, with the mandatory fixture tests, and matching
rules emit *proposals* into the trace ("TSan would verify this; it was not
run"). Nothing dynamic auto-runs — execution of proposals is Phase 4, when
sanitizers are plugins. Judgment call for refinement: this could move wholesale
to Phase 4 and leave the trace without proposals until then.

## Chunk 2.3 — baseline cache

Branch: `feat/p2c3-baseline-cache`.

### 2.3a — persisting a run

A baseline: the fingerprint set of a ref, stored under the cache dir with the
facts that decide whether it is still true — tool versions, config hash,
compile flags, fingerprint scheme version. The architecture-v2 invalidation
list is the contract, and each item on it is its own test: change the flag,
lose the cache. Dynamic results are never cached (the determinism claim,
scoped).

### 2.3b — subtraction against real baselines

`FindingStore.new_since()` wired to the cached set: O(n+m) via the fingerprint
dict, already latency-tested at 10k findings. Full-scan-with-baseline and
diff-only fall out of the same two knobs — scope and baseline are store
queries, not modes.

## Chunk 2.4 — the review-gate surface

Branch: `feat/p2c4-review-surface`.

### 2.4a — `review()`

The one call: resolve scope from a diff ref → plan → run the static plugins →
subtract the baseline → shaped output with the plan trace attached. The tool
description is product copy and lives in `server.py` alone.

### 2.4b — `audit()`

The same machinery with scope = everything: the full picture, baseline still
subtracted when one exists, nothing hidden.

### 2.4c — `get_finding(id)` and the two-tier output

When output oversizes: a one-line index of everything plus full detail for the
top N, ranked diversity-first (one example per distinct location before any
second example — open-questions #1). `get_finding(id)` fetches full detail for
anything indexed. Judgment call for refinement: where findings persist between
the `review()` call and a later `get_finding()` — in-process for the server's
lifetime, or on disk beside the baseline cache.

## Explicitly out of Phase 2

The container engine and cppcheck (Phase 3); sanitizers and perf as plugins,
live escalation, Infer, and the suppression file (Phase 4); the eval harness
and PyPI (Phase 5). The existing eight tools keep working unchanged throughout
— every chunk's acceptance includes the pre-existing suite green.
