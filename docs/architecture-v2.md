# Architecture v2 — the review-gate restructure

The v1 architecture answered "how do we run sanitizers and analyzers for an AI
agent, on any machine, without lying about the results?" It works, and most of
it survives unchanged. What it cannot do is scale the *middle*: three hand-wired
pipelines (`static_check`, `sanitize`, `profile`) each know their tools by name,
so every new analyzer means editing a pipeline, and any behavior that spans
tools — deduplication, baseline comparison, correlation — has nowhere to live.

v2 restructures that middle layer once, so that everything afterward lands as a
plugin. The product it enables: a **review gate** — an agent runs one tool call
before declaring work done, and sees only the findings its change introduced,
confirmed across independent analyzers.

The reference points for this design are documented in the ADRs
([0001](adr/0001-planner-is-deterministic-code.md),
[0002](adr/0002-finding-fingerprints.md),
[0004](adr/0004-execution-engines.md),
[0005](adr/0005-escalation-retired.md)). This file describes the shape they add
up to.

## The five layers

```
┌────────────────────────────────────────────────────────────┐
│ 5. SURFACE      MCP tools, intent-named                    │
│    review() · audit() · verify() · profile()               │
│    capabilities() · get_finding(id)                        │
├────────────────────────────────────────────────────────────┤
│ 4. PLANNER      deterministic — never an LLM               │
│    applicability gates → plan → parallel dispatch →        │
│    plan trace                                              │
├────────────────────────────────────────────────────────────┤
│ 3. ANALYZERS    one contract, N plugins                    │
│    clang-tidy · warnings · cppcheck · IWYU · Infer ·       │
│    ASan · TSan · LSan · UBSan · perf                       │
├────────────────────────────────────────────────────────────┤
│ 2. STORE        normalized findings                        │
│    fingerprints · cross-tool correlation · baselines ·     │
│    suppressions · ranking → output shaping                 │
├────────────────────────────────────────────────────────────┤
│ 1. ENGINES      where things actually run                  │
│    local | wsl | container · toolchains · path             │
│    translation · process guardrails · capability probes   │
└────────────────────────────────────────────────────────────┘
```

**Surface** owns the MCP contract and nothing else: tool schemas, the output
shaping rules (frame trimming, grouping, the two-tier index), and the
`get_finding(id)` detail fetch. Tools are named for intents — an agent picks
`review` correctly from the description alone; a `scope` parameter it must
reason about is where agents fumble.

**Planner** is deterministic code (ADR-0001). It resolves scope to files,
applies each analyzer's gate chain, orders work by cost tier, dispatches in
parallel around shared-resource locks, and emits a **plan trace** — what ran,
on what, why, and what was skipped and why — before anything executes. Same
inputs, same plan, every run.

**Analyzers** all implement one contract: `applicable(scope)` → `cost_tier` →
`unit_of_work` → `run(scope, engine)` → findings. Static and dynamic tools are
the same kind of thing to every layer above; the contract does not care that
clang-tidy reads code and TSan watches it run. Adding an analyzer is one module
and one parser, zero planner changes.

**Store** is where findings from every analyzer become one thing. It owns
fingerprinting (ADR-0002), cross-tool correlation (two independent engines
flagging the same defect is one finding with two confirmations), baseline
subtraction (`new_since(ref)` — the operation that makes the review gate), the
suppression store, and ranking. Scope and baseline are store queries, not
modes: full-scan-with-baseline and diff-only both fall out of the same two
knobs.

**Engines** answer "where does this run?" — `local` when the host has the
tools, `wsl` on Windows, `container` everywhere else (ADR-0004). The layer owns
toolchains, path translation, process guardrails, and the capability probes.
Every finding records the engine that produced it.

## Target tree

```
src/cpp_analysis_mcp/
├── surface/        # layer 5: tool definitions, output shaping
├── planner/        # layer 4: scope, gates, scheduler, dispatch, plan trace
├── analyzers/      # layer 3: one module per analyzer, each owning its parser
├── store/          # layer 2: models, fingerprints, dedup, baselines, suppressions
├── engines/        # layer 1: local / wsl / container, toolchains, process, probes
└── project/        # cross-cutting state: config, compile_db, caches, cost history
```

Where v1 code lands:

| v1                                  | v2                       |
| ----------------------------------- | ------------------------ |
| `server.py`, prompt/output shaping  | `surface/`               |
| `pipelines/*`                       | dissolve into `planner/` + `analyzers/` |
| `parsers/*`                         | `analyzers/<name>/`      |
| `models.py`                         | `store/models.py`        |
| `platforms/`, `toolchains/`, `wsl.py`, `process.py` | `engines/` |
| `capabilities.py`                   | `engines/probes.py`      |
| `compile_db.py`, `build/`           | `project/`, `engines/build/` |
| `context.py`, settings, storage     | `project/`               |

Files move **when a phase touches them, never in a big-bang rename** — every
diff stays reviewable, and `git log --follow` keeps each file's history
legible.

## Layering rules

The four v1 rules stand. v2 adds three:

1. **Imports point down.** `surface → planner → analyzers → store → engines`.
   Nothing imports upward; nothing reaches around a layer.
2. **A file's directory answers "what layer is this?"** without opening it. If
   a module needs a comment explaining which layer it belongs to, it is in the
   wrong place.
3. **The only LLM in the system is the caller.** No layer holds an API key or
   calls a model. The one sanctioned future exception is MCP sampling — the
   server asking the *connected client's* model — reserved as a slot in the
   store's ranking stage and not built until evidence demands it.

## The determinism claim, scoped

Absolute determinism is the wrong promise — sanitizer output is
timing-sensitive by nature. The claim v2 makes and tests:

> Given fixed tool versions, configs, `compile_commands.json`, and scope, the
> **plan** is identical and the **static findings** are identical, every run.

Dynamic results are evidence, not cacheable fact: the *decision* to gather them
is deterministic; their content is not, and is never cached. What invalidates
cached decisions is an explicit list, not a vibe: compiler flags,
tool version bumps, config edits, fingerprint scheme changes. Pinned container
images are what make "fixed tool versions" true for every user (ADR-0004).

## What this is, relative to CodeRabbit

| CodeRabbit                       | Here                                        |
| -------------------------------- | ------------------------------------------- |
| ~50 tools behind a harness       | Analyzer registry (layer 3)                 |
| Sandboxed cloud execution        | Engine layer + pinned toolbox image         |
| Diff-scoped PR review            | Scope × baseline as store queries           |
| LLM judge filters findings       | The calling agent, armed with evidence + `get_finding` |
| `.coderabbit.yaml`               | `.cpp-analysis.toml`                        |
| Learnings                        | Suppression store                           |
| —                                | **Dynamic verification: sanitizers + profiler** |

The last row is the moat. A static-only reviewer can suspect a data race; this
system can *witness* it.

## Migration phases

| Phase | Ships                                                        |
| ----- | ------------------------------------------------------------ |
| 1     | Store core + analyzer contract; clang-tidy and warnings as the first plugins |
| 2     | Planner + `review()`/`audit()` surface; scope resolver; baseline cache — **the review gate** |
| 3     | Container engine + toolbox image; cppcheck proves the registry |
| 4     | Sanitizers and perf as plugins; suppressions; Infer          |
| 5     | Eval harness, PyPI, registry listings                        |

Every phase is releasable. Nothing built in one phase is rebuilt in a later
one — the end state is this document, and each phase pours into it.
