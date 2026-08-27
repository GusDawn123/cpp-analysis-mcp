# Phase 1 chunk map — store core and analyzer contract

Phase 1 builds the two load-bearing pieces of architecture v2: the finding
store (layer 2) and the analyzer contract (layer 3), proven by porting the two
cheapest analyzers onto them. At the end of the phase the eight existing MCP
tools behave identically — verified by the existing test suite — but every
finding flows through fingerprinting and the registry, and the review gate
(Phase 2) has something to stand on.

Later phases get their own maps when we reach them. Planning two phases ahead
is how plans rot.

## Working protocol (binding for every sub-chunk)

One branch per chunk (`feat/p1c1-finding-store`), one conventional commit per
sub-chunk. Per sub-chunk, six steps, no skipping:

1. **Plan** — goal, files touched, interface changes, test plan, performance
   notes (data-structure choices with complexity stated)
2. **Refine** — the plan is revised until approved; nothing is written before
3. **Implement** — tests first, code second, in the repo's existing voice
   - *3.5 self-review* — a deliberate pass for simplicity, naming, dead
     weight, and idiom-match before any external review
4. **Review** — CodeRabbit on exactly that diff
5. **Fix** — real findings addressed; rejected findings documented with reasons
6. **Commit** — one conventional commit

Chunk completion: full suite + golden validation green → PR with this spec
linked → merge. Files move only when a sub-chunk touches them (architecture-v2,
"migration by touch").

## Chunk 1.1 — finding store core

Branch: `feat/p1c1-finding-store`. New package: `store/`.

### 1.1a — model evolution

`models.py` relocates to `store/models.py` (imports shimmed so nothing else
moves yet). `Finding` grows: `fingerprint: str`, `fingerprint_scheme: int`,
`engine: str`, `confirmations: tuple[Confirmation, ...]` (which tools
independently agree, by what evidence). No behavior change anywhere —
the existing suite is the regression gate.

### 1.1b — fingerprints

`store/fingerprints.py`: the ADR-0002 scheme —
`hash(rule_id, relative_path, strip_ws(line_text), occurrence_index)`, scheme
version 1, pure functions only. Test plan: golden-style cases for line-shift
invariance (insert above, reformat, block move), occurrence disambiguation,
scheme-version stamping. Performance note: fingerprinting is O(1) per finding
with no I/O; the index built over them (1.1c) is where complexity lives.

### 1.1c — store operations

`store/store.py`: ingest findings from N parser runs → dedup within a run →
correlate across tools (equivalence table v0 may be empty — the mechanism
lands, mappings accrue later) → `new_since(baseline)` subtraction →
suppression respect (`// NOLINT` already parsed; a project suppression file
joins it) → severity/diversity ranking feeding the existing output shaping.
Performance note: baseline matching via dict keyed by fingerprint — O(n+m)
for n baseline / m head findings, benchmarked at 10k findings per the
latency-test practice.

## Chunk 1.2 — analyzer contract

Branch: `feat/p1c2-analyzer-contract`. New package: `analyzers/`.

### 1.2a — the interface and registry

`analyzers/base.py`: the contract —
`applicable(scope) → cost_tier → unit_of_work → run(scope, engine) → [Finding]`
— plus a registry that owns the gate chain (enabled → files match →
build-membership for compilation-dependent tools → prerequisites on the
resolved engine), recording every gate verdict for the future plan trace.

### 1.2b — clang-tidy as plugin #1

`analyzers/clang_tidy.py` wrapping the existing parser unchanged. Unit of
work: translation unit (from `compile_commands.json`). The port proves the
contract fits a compilation-dependent, per-TU static tool.

### 1.2c — compiler warnings as plugin #2

`analyzers/warnings.py` over the existing diagnostics parser. Proves the
contract fits a tool whose findings fall out of a build rather than a
dedicated invocation.

### 1.2d — route the existing tools through the registry

`static_check_file` / `_snippet` re-front onto the registry + store.
Acceptance is blunt: **zero behavior change**, existing tests and golden
fixtures green, plus new tests asserting findings now carry fingerprints and
gate verdicts.

## Explicitly out of Phase 1

The planner and `review()`/`audit()` surface (Phase 2), the container engine
(Phase 3), sanitizers-as-plugins and live escalation (Phase 4), cppcheck and
all new analyzers (Phase 3+). Phase 1 makes the ground they stand on.
