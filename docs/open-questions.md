# Open questions

Decisions not yet made. Input wanted on all of these — especially #1.

Each section gives the context, the options considered, current thinking, and the
specific thing that needs deciding.

---

## 1. Output volume vs. usefulness

**The tension.** Every finding returned costs tokens in the AI's limited context
window. Return too much and the findings crowd out the source code the AI needs
to reason about them — you get a detailed bug report and no room left to fix the
bug. Return too little and the AI misses the real problem, or burns turns making
follow-up calls.

**Scale of the problem.** Rough token costs:

| | tokens |
|---|---|
| One full finding, two stack traces, 5 frames each | ~400 |
| One-line summary of a finding | ~40 |
| 47 findings, full detail | ~18,000 |
| A typical usable context budget for tool output | ~5,000 |

A genuinely broken program blows the budget by 3–4x.

### Proposed approach

Four mechanisms, in the order they apply. The ordering matters — the first two
are nearly free and often make the rest unnecessary.

**a. Trim stack frames to user code.** A raw ThreadSanitizer stack is ~20 frames,
of which ~15 are `std::` internals, libc, and pthread machinery nobody reads.
Keeping only frames whose file path is inside the project directory, plus one
boundary frame for context, cuts a finding by roughly 60% and loses nothing
useful.

**b. Deduplicate.** Fifty reported races are usually three real bugs hit
repeatedly in a loop. Group by `(category, file, line)` and report an occurrence
count. Free, and frequently solves the whole problem.

After (a) and (b), the 47-finding example often collapses to ~6 unique findings
at ~2,400 tokens — comfortably within budget, no truncation needed.

**c. Two-tier output, only when still oversized.** A one-line index of every
finding plus full detail for the top N:

```json
{
  "index": [
    {"id": "tsan-1", "category": "data-race", "file": "src/order_book.cpp",
     "line": 147, "occurrences": 12},
    {"id": "tsan-2", "category": "data-race", "file": "src/cache.cpp",
     "line": 61, "occurrences": 3}
  ],
  "detailed": [ /* full findings for top 5 */ ],
  "truncated": true,
  "total_unique": 47
}
```

The AI sees that everything exists and can request detail on any specific id.

**d. Raw log as a path, never as content.** Return
`raw_log_path: "/tmp/.../tsan.log.48211"` rather than the log text. The AI
already has file-reading and grep tools; let it opt in to reading more. This
costs ~20 tokens instead of ~18,000 and preserves full access.

### What needs deciding

- **When (c) is necessary, how should findings be ranked?** Options: occurrence
  count, proximity to files the user is currently editing, severity, or diversity
  (one example per distinct location before any second example). Diversity seems
  right — five variations of the same bug is worse than five different bugs — but
  this is a guess.
- **What is the right N?** 5 and 10 both seem defensible.
- **Should the AI be able to ask for more via a parameter** (`detail_level`) or a
  separate tool (`cpp_get_finding(id)`)? A parameter is fewer round trips; a
  separate tool is a cleaner contract.
- **Is per-tool tuning needed?** A profiler's hotspot list is naturally bounded
  (top 20 functions is a complete answer). A sanitizer's finding list is not.
  These may not want the same policy.

---

## 2. Long operations inside a single call

A clean build with sanitizers took 40 seconds in the
[worked example](workflow-scenario.md). Large projects will take minutes.

MCP supports progress notifications, but the simple implementation just blocks
until done.

**Options:**
- Block, with a generous timeout. Simplest. The AI appears frozen.
- Stream progress notifications. Better experience, more complex.
- Return a job handle immediately, let the AI poll. Most flexible, most machinery,
  and risks the AI polling in a tight loop.

**Undecided.** Leaning toward blocking with progress notifications, since builds
are rarely longer than a few minutes and job handles add real complexity.

---

## 3. Safety model

This server **compiles and executes arbitrary code**, at an AI's discretion, on
machines belonging to strangers who installed it from GitHub.

That is a real risk surface and it deserves an explicit answer rather than an
afterthought.

**Established so far:**
- A `workspace` confinement root; nothing executes outside it
- Mandatory timeouts on every subprocess
- Build directories are separate (`build-tsan/`), never the user's own build

**Open:**
- Should running a built binary require explicit user consent, separately from
  building it? Building is comparatively safe; running is not.
- What happens with a test program that spawns network connections or writes
  outside the workspace? Do we care, or is that the user's problem since it is
  their own code?
- Is a container/sandbox mode worth offering for untrusted projects?

---

## 4. Capability detection staleness

Detection results cache to disk, fingerprinted on compiler path, compiler
version, and OS release.

That correctly invalidates when somebody installs a new compiler. It does **not**
catch runtime settings — the `kernel.perf_event_paranoid` example changes with a
`sysctl` command and nothing about the fingerprint moves.

**Current thinking:** split the cache. Toolchain facts (which sanitizers link)
are expensive to determine and stable, so cache them. Environment facts
(`perf_event_paranoid`, whether `/proc` is mounted, WSL presence) are cheap to
read and volatile, so check them every time.

**Needs confirmation** that the split is drawn in the right place.

---

## 5. Tool surface and granularity

Not yet designed. The rough shape:

```
cpp_capabilities()      what can this machine do
cpp_static_check()      clang-tidy + compiler warnings
cpp_sanitize()          build + run + parse, one call
cpp_profile()           measure where time goes
cpp_analyze_snippet()   fast path for a single self-contained file
```

**Open:**
- Should `cpp_sanitize` be one tool or split into `cpp_build` + `cpp_run`?
  Combined means fewer round trips, and the AI almost always wants both. Split
  means the AI can rebuild once and run several times with different inputs.
- Is `cpp_analyze_snippet` worth having, or does it just duplicate `cpp_sanitize`
  with a different input shape?
- Should there be one tool per sanitizer (`cpp_check_races`,
  `cpp_check_memory`) rather than one with a `sanitizer` parameter? Separate
  tools have clearer descriptions, and **tool descriptions are the only thing the
  AI uses to choose** — which argues for clarity over compactness. But it
  multiplies the surface.

---

## 6. Where to cut v1

Everything above describes the full vision. What actually ships first?

**Candidate v1:** Linux + macOS, clang + gcc, sanitizers and static analysis
only, profiling deferred.

Rationale: profiling is the only Tier 2 component — genuinely different programs
per platform, roughly doubling the work. Correctness tools share their
implementation across platforms almost entirely.

**Counter-argument:** "is it slow" was an explicit goal, and shipping without it
means the tool only does half of what it claims.

**Undecided.**

---

## Resolved

**Should we support gcc, or clang only?**
Both. Resolved 2026-08-04. gcc vendors LLVM's sanitizer runtime rather than
implementing its own, so the report format is the same and the parsers — the
expensive part — work unchanged. The only real loss is `-Wthread-safety`, which
has no gcc equivalent and will report as unavailable rather than silently
returning nothing. See
[architecture.md](architecture.md#operating-system-and-compiler-are-separate-axes).
