# Workflow scenario

A full worked example of somebody using this, start to finish. The point is to
make the design concrete — every design problem in
[open-questions.md](open-questions.md) surfaced by walking through this.

Written before the code, and kept as the design's worked example. The tool
names and the install command below match the shipped server; the JSON bodies
are illustrative — the real shapes are the dataclasses in
`src/cpp_analysis_mcp/store/models.py` (`CapabilityStatus`, `AnalysisReport`,
`Finding`, `ProfileReport`).

---

## Setting

Ubuntu 24.04, x86_64. A developer maintains a market data handler in C++20 —
threads decoding exchange messages into a shared order book.

It works. Except roughly one run in fifty reports the wrong total volume, and
twice in production it crashed with a garbage memory address. They have read the
code four times and found nothing.

This is the classic profile of a concurrency bug. Reading harder does not help.

---

## Step 0 — Install

```bash
claude mcp add cpp-analysis -- uv run --directory /path/to/cpp-analysis-mcp cpp-analysis-mcp
```

The server starts.

It probes the toolchain right away: one planted-bug program per analysis,
compiled and run concurrently, off the event loop so the MCP handshake is not
stalled. On this machine that is a few seconds, once — the results are cached to
disk under a fingerprint of the compiler, the OS, and the tools involved. The
first draft of this design deferred detection to first use to save startup
time; measured, the probes were cheap enough that an honest answer at startup
won.

---

## Step 1 — The user asks

> *"My order book sometimes reports the wrong total volume. Maybe 1 in 50 runs.
> I've stared at it for two days."*

---

## Step 2 — The AI checks what this machine can do

Before promising anything, it calls `capabilities`. Detection already ran at
startup; this reads the answers back and spawns nothing.

```json
{
  "tsan":          {"available": true, "verified_by": "compiled and ran a planted data race; ThreadSanitizer reported it"},
  "asan":          {"available": true, "verified_by": "compiled and ran a planted heap buffer overflow; AddressSanitizer reported it"},
  "lsan":          {"available": true, "verified_by": "compiled and ran a planted memory leak; LeakSanitizer reported it"},
  "ubsan":         {"available": true, "verified_by": "compiled and ran a planted signed integer overflow; UndefinedBehaviorSanitizer reported it"},
  "thread-safety": {"available": true, "verified_by": "compiled a planted write to a guarded_by variable with no lock held; -Wthread-safety reported it"},
  "clang-tidy":    {"available": true, "verified_by": "checked a planted null pointer written as 0; clang-tidy reported it"},
  "profile": {
    "available": false,
    "reason": "the record step failed with exit 255: perf_event_open ... Permission denied",
    "suggestion": "sudo sysctl -w kernel.perf_event_paranoid=1"
  }
}
```

That last block is the single highest-value thing detection does.

`kernel.perf_event_paranoid` is a real Linux security setting, and Ubuntu ships
it at `4`, which blocks unprivileged users from profiling entirely. Without
detection, running the profiler returns a cryptic permissions error, the AI
invents a plausible-sounding wrong explanation, and the user concludes the tool
is broken.

Detecting it and returning the exact command to fix it turns a dead end into a
ten-second fix. Note that this required knowing *Linux*, not knowing MCP. Most of
the real value in this project will be gotchas like this, found one platform at a
time.

The AI does not touch the profiler here — this is a correctness problem, not a
speed problem. But it now knows the option exists and is currently blocked, which
matters in step 6.

---

## Step 3 — Cheapest check first

`static_check_file` on the suspect file, first with `clang-tidy`, then with
`thread-safety`. Seconds, nothing executes.

```json
{
  "analysis": "clang-tidy",
  "findings": [
    {
      "id": "clang-tidy-1",
      "tool": "clang-tidy",
      "severity": "warning",
      "category": "concurrency-mt-unsafe",
      "message": "Function 'localtime' is not thread-safe; use 'localtime_r'",
      "location": {"file": "src/logging.cpp", "line": 88, "column": 16},
      "fingerprint": "3f9c2a1e7b04d5c6",
      "fingerprint_scheme": 1
    }
  ],
  "exit_code": 0,
  "limitations": [],
  "verified_by": "checked a planted null pointer written as 0; clang-tidy reported it"
}
```

A real bug, but not *this* bug — wrong file, wrong symptom.

And `-Wthread-safety` found nothing because it *cannot*. It only checks rules you
have declared, and this project has no annotations, so it has no idea what is
supposed to be locked. An empty report here means a detector that was proved to
work saw nothing in what it was allowed to see — which is why `verified_by` and
`limitations` travel with every result rather than being looked up later.

The escalation ladder is working as intended: cheap check ran, came back
inconclusive, move up.

---

## Step 4 — Escalate to ThreadSanitizer

```json
{"tool": "sanitize_project", "source": ".", "analysis": "tsan"}
```

Internally: CMake configures a fresh scratch build directory with
`-fsanitize=thread -g -O1`, builds it (about 40 seconds), then runs it with the
environment that travelled attached to the built binary — so the sanitizer flags
and the runtime options cannot drift out of sync. The pipeline owns its own
timeouts; there is nothing for the caller to tune.

```json
{
  "analysis": "tsan",
  "findings": [
    {
      "id": "tsan-1",
      "tool": "tsan",
      "severity": "error",
      "category": "data-race",
      "message": "Write of size 8 by thread T3 while thread T1 was reading",
      "location": {"file": "src/order_book.cpp", "line": 147, "column": 9},
      "symbol": "OrderBook::total_volume_",
      "threads": [
        {
          "thread_id": "T3", "op": "write", "size": 8,
          "locks_held": [],
          "frames": [
            {"function": "OrderBook::add_fill",     "location": {"file": "src/order_book.cpp", "line": 147}},
            {"function": "FeedHandler::on_message", "location": {"file": "src/feed.cpp",       "line": 210}}
          ]
        },
        {
          "thread_id": "T1", "op": "read", "size": 8,
          "locks_held": ["OrderBook::book_mutex_"],
          "frames": [
            {"function": "OrderBook::snapshot", "location": {"file": "src/order_book.cpp", "line": 203}}
          ]
        }
      ],
      "allocated_at": {"file": "src/feed.cpp", "line": 44},
      "occurrences": 12,
      "engine": "local"
    }
  ],
  "build_warnings": [],
  "exit_code": 66,
  "limitations": [],
  "verified_by": "compiled and ran a planted data race; ThreadSanitizer reported it"
}
```

Twelve reports of the same race in the loop collapsed into one finding with
`occurrences: 12`, and `engine` says where it was witnessed — the host here; a
WSL distro or a Linux container when one of those ran it.

Look at `locks_held`. Thread T1 held `book_mutex_`. Thread T3 held nothing.

That single pair of facts *is* the diagnosis — somebody added a write path that
forgot the lock the read path uses. Two days of staring, found in forty seconds.

Note what the tool did **not** return: a suggested fix. See the "facts, not
advice" section in [architecture.md](architecture.md#a-principle-facts-not-advice).

---

## Step 5 — Fix, then verify

The AI reads `order_book.cpp:147`, sees the unguarded `total_volume_ += qty`,
and — noting from surrounding code that `book_mutex_` is the established
convention — adds the lock.

Re-runs `sanitize_project`, and findings come back empty.

It reports that honestly, because the tool description says what a sanitizer
is: ThreadSanitizer only observed the code paths that ran. A clean result means
*the exercised paths are race-free*, not *the program is race-free*. An AI that
says "fixed, guaranteed" is overclaiming, and both the tool descriptions and the
returned data (`verified_by`, `limitations`) are shaped to discourage that.

---

## Step 6 — "It got slower"

> *"Latency went up. p99 went from 40µs to 110µs."*

Predictable. A mutex was just added to a hot path.

Now the profiler matters, and it is blocked. The AI surfaces the suggestion it
stored back in step 2:

> Profiling is disabled on this machine by `kernel.perf_event_paranoid=4`.
> Run `sudo sysctl -w kernel.perf_event_paranoid=1` and I'll re-check.

They run it — and restart the server, since the probe result is cached under the
kernel setting that just changed, so the cache retires itself. `profile_project`
builds at `-O2` — critically with `-fno-omit-frame-pointer`, because without
frame pointers `perf`'s call graphs on optimized builds are unusable garbage —
then records and processes the samples.

```json
{
  "analysis": "profile",
  "hotspots": [
    {"function": "__lll_lock_wait", "self_pct": 34.1, "total_pct": 34.1,
     "note": "futex wait -- lock contention"},
    {"function": "FeedHandler::decode", "self_pct": 19.4, "total_pct": 21.0},
    {"function": "OrderBook::add_fill", "self_pct": 8.2, "total_pct": 47.9,
     "location": {"file": "src/order_book.cpp", "line": 147}}
  ],
  "samples": 84210,
  "event": "cpu/cycles/P",
  "fingerprints": [
    {"category": "lock-contention", "share_pct": 34.1,
     "statement": "34% of self time waiting on a futex: threads are queued behind a lock",
     "candidates": ["shrink the critical section", "a lock-free counter", "per-thread accumulation"]}
  ],
  "confidence": "84210 samples: percentage differences of a point or more are real",
  "exit_code": 0
}
```

34% of runtime sitting in futex wait — threads queued up behind the new mutex.
The correctness fix created a performance bottleneck. Note `samples` and
`event`: the ranking is worth what it was measured with, and a run inside a VM
with no hardware counters would say `cpu-clock` here instead.

---

## Step 7 — The better fix

Now there are grounds for a real solution rather than a guess:
`std::atomic<uint64_t>` with `fetch_add(qty, std::memory_order_relaxed)`.
Race-free without serializing threads behind a lock.

Rather than trust the reasoning, race it: `benchmark_variants` with the mutex
version as the baseline and the atomic version as the challenger, same workload,
outputs compared byte for byte. A rewrite that got faster by answering
differently is rejected no matter how fast it ran. Then re-run ThreadSanitizer:
clean. Re-run the profiler: futex wait gone, p99 back under 45µs. The
`make-it-faster` prompt ships exactly this loop as a slash command.

---

## What the arc demonstrates

Both halves of the tool were necessary and neither was sufficient.

- Static analysis alone: found an unrelated bug, missed this one
- ThreadSanitizer alone: found the race, then the fix silently made things slow
- Profiler alone: would have shown a slow mutex with no idea why it existed

The sequencing is the product. Any one tool in isolation is a partial picture,
and knowing which to reach for next given what the last one said is exactly the
judgment an AI with all of them wired up can supply.
