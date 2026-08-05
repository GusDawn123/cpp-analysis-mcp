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

The rule is a path comparison, not a judgement: keep frames whose file path is
inside the project directory, drop the rest, and replace them with a marker
recording how many were hidden and that they were library code.

```
  #0 OrderBook::add_fill       src/order_book.cpp:147     kept
  #1 FeedHandler::on_message   src/feed.cpp:210           kept
  [+7 frames of std::thread machinery]                    marker
```

Those seven frames appear in every threaded C++ program ever written. Cuts a
finding by roughly 60%.

Known limits: if the bug is inside a third-party library, or in a header-only
library like Boost or Eigen, the relevant frames get trimmed. The caller's frame
in user code survives, which is the line a developer would edit anyway. An
`include_system_frames` parameter is the escape hatch.

**b. Group identical findings.** Fifty reported races are usually three real bugs
hit repeatedly in a loop. Report an occurrence count instead of fifty copies.

The grouping key must be **the pair of locations**, not one:

```
(category, write_location, read_location)
```

An earlier draft keyed on a single location, which was wrong. A race is between
two places, so one write site can race with two different read sites — genuinely
two bugs. Keying on one location would merge them and silently discard the
second. Keying on the pair means grouping only ever merges findings identical in
every way that matters.

### The safety property underneath all of this

**Nothing is ever deleted.** The complete tool output is always written to disk
and its path returned. Trimming and grouping decide what to show *first*; they
never destroy data. If the summarized view does not explain the bug, the AI opens
the raw log and reads or greps all of it — it already has file tools.

This is testable rather than hoped for. The `fixtures/cpp/` programs contain
deliberately planted bugs at known lines, so a test asserts that the *trimmed*
output still identifies them. Any rule that cuts too aggressively turns CI red.

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

**Resolved 2026-08-05.** In one line: isolate files and processes, do **not**
sandbox the sanitized program itself, and offer containers later as a
cross-platform feature rather than a security wall.

### Start from the threat model

The program this server runs is the **user's own code, on the user's own
machine, at their request** — the same binary they build and run by hand all
day. Heavy sandboxing would defend the user from themselves. The realistic
risks are mundane — a hung process, runaway output, files written where they
should not be — and two cheap layers cover them completely:

1. **File isolation.** All builds go to scratch directories the server owns
   (`build-tsan/` and friends) inside the `workspace` confinement root. The
   user's source tree and their real build directory are never written to.
2. **Process guardrails.** Every subprocess gets a mandatory timeout whose
   expiry kills the whole process group, so a hung test cannot leave orphaned
   children; a cleaned environment, so `ASAN_OPTIONS`/`LSAN_OPTIONS` inherited
   from the user's shell cannot silently distort results; and output captured
   to files rather than pipes that fill and stall. None of this is
   speculative — it is already implemented and CI-tested in
   `scripts/fixtures.py`; the build layer will promote that code, not invent
   new code.

### Why not a real sandbox?

Because sandboxes break the detectors — measured, not guessed:

- TSan under Docker's default seccomp profile **crashes on startup**: it calls
  `personality()` to control address-space randomization, and the sandbox
  blocks that syscall. Capturing the Linux golden files required
  `--security-opt seccomp=unconfined`.
- gcc's TSan runtime additionally required lowering the kernel's
  `vm.mmap_rnd_bits` setting — a privileged operation no sandbox permits.

Sanitizers work by rearranging the process's memory world and intercepting
syscalls; sandboxes exist to forbid exactly that. A tightly sandboxed
sanitizer run fails strangely, and a strange failure is one step from the
false all-clear this project treats as its worst outcome.

### The three open bullets, answered

- **Separate consent for running vs. building?** No. Consent lives in the MCP
  client: Claude Code and its peers already ask the user before a tool call
  executes. A second permission system inside the server would double-prompt
  the same action; the host's prompt is the real one.
- **A test program that opens network connections or writes outside the
  workspace?** It is the user's own code, and it could have done the same when
  they ran it by hand. Not ours to prevent; timeouts and scratch directories
  bound the blast radius of accidents.
- **Container mode?** Yes — on the roadmap, but reframed. Its killer use is
  not security, it is **capability**: a Linux container on a Mac unlocks the
  Linux-only tools (LeakSanitizer, TSan's deadlock detector) that macOS cannot
  run natively, and gives every platform a reproducible environment. The
  golden files were captured exactly this way by hand; the feature would
  automate it, including the two security relaxations TSan needs. It slots
  into `platforms/` as one more answer to "where does this run?" — a new
  backend behind an existing seam, not a redesign.

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

## 6. Memory profiling — a third category we nearly missed

The original goal was tooling for **multithreaded, low-latency, and
memory-efficient** C++. The first draft of this design covered the first two and
silently dropped the third.

"Is my memory handling broken?" (AddressSanitizer, LeakSanitizer) is a different
question from **"how much memory am I using, where does it come from, and am I
allocating somewhere I cannot afford to?"** For low-latency work the second is
often the more important one — a single allocation in a hot path can cost more
than the rest of the function.

| Question | Linux | macOS | Windows |
|---|---|---|---|
| Total usage and growth over time | heaptrack, `/proc/<pid>/smaps` | `footprint`, `vmmap` | VMMap |
| Which lines allocate the most | heaptrack | Instruments Allocations | ETW |
| Short-lived allocation churn | Valgrind DHAT | Instruments | — |
| False sharing between threads | `perf c2c` | no equivalent | — |

**False sharing deserves specific attention.** Two threads writing to two
*different* variables that happen to share a 64-byte cache line. There is no
race — the code is correct — but the cache line ping-pongs between cores on every
write and throughput can drop tenfold. TSan sees nothing because there is no
race. A normal profiler shows a slow function with no explanation. `perf c2c` is
purpose-built for it.

It is also the sharpest platform-portability problem in the project: it depends
on specific Intel/AMD performance-counter events, so it is largely x86-only, ARM
Linux support is thin, and macOS has nothing comparable.

**Open:**
- Does memory profiling belong in v1, or with general profiling in v2? It is
  Tier 2 work — heaptrack, Instruments, and VMMap share nothing.
- Is false sharing detection worth shipping as an x86-Linux-only feature? It is
  high value for the target audience and genuinely hard to find another way, but
  it would be the first capability with no path to the other platforms at all.
- Is "am I allocating in a hot path" better served by heap profiling, or by
  cross-referencing profiler output against allocation sites?

---

## 7. Where to cut v1

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

**What is the safety model for executing user code?**
Resolved 2026-08-05 — see [section 3](#3-safety-model) for the full reasoning.
File and process isolation only; no sandbox around the sanitized program
(sandboxes demonstrably break the sanitizers); containers later as a
cross-platform capability unlock, not a security wall.

**Should we support gcc, or clang only?**
Both. Resolved 2026-08-04. gcc vendors LLVM's sanitizer runtime rather than
implementing its own, so the report format is the same and the parsers — the
expensive part — work unchanged. The only real loss is `-Wthread-safety`, which
has no gcc equivalent and will report as unavailable rather than silently
returning nothing. See
[architecture.md](architecture.md#operating-system-and-compiler-are-separate-axes).
