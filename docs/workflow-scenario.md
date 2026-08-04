# Workflow scenario

A full worked example of somebody using this, start to finish. The point is to
make the design concrete — every design problem in
[open-questions.md](open-questions.md) surfaced by walking through this.

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
claude mcp add cpp-analysis -- uvx cpp-analysis-mcp
```

The server starts.

It deliberately does **not** check the toolchain yet. Doing so means compiling
three small test programs before the user has typed anything, and a five-second
startup delay is the kind of friction that gets a tool uninstalled. Detection is
deferred to first use and cached to disk afterward.

---

## Step 1 — The user asks

> *"My order book sometimes reports the wrong total volume. Maybe 1 in 50 runs.
> I've stared at it for two days."*

---

## Step 2 — The AI checks what this machine can do

Before promising anything, it calls `cpp_capabilities`. This is the first call,
so detection actually runs — about two seconds to compile and execute the probe
programs.

```json
{
  "platform": "linux-x86_64",
  "toolchains": [
    {"name": "clang++", "version": "18.1.8", "path": "/usr/bin/clang++"},
    {"name": "g++",     "version": "13.2.0", "path": "/usr/bin/g++"}
  ],
  "build_systems": {"cmake": "3.28.3", "ninja": "1.11.1"},
  "sanitizers": {
    "thread":    {"available": true, "verified_by": "smoke_test"},
    "address":   {"available": true, "verified_by": "smoke_test"},
    "undefined": {"available": true, "verified_by": "smoke_test"},
    "leak":      {"available": true, "verified_by": "smoke_test"}
  },
  "static": {"clang_tidy": "18.1.8", "thread_safety_analysis": true},
  "profiler": {
    "backend": "perf",
    "version": "6.8",
    "available": false,
    "reason": "kernel.perf_event_paranoid = 4 blocks unprivileged profiling",
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

`cpp_static_check`. Seconds, nothing executes.

```json
{
  "findings": [
    {
      "tool": "clang-tidy",
      "severity": "warning",
      "category": "concurrency-mt-unsafe",
      "message": "Function 'localtime' is not thread-safe; use 'localtime_r'",
      "location": {"file": "src/logging.cpp", "line": 88}
    }
  ],
  "thread_safety_analysis": {
    "enabled": false,
    "reason": "no GUARDED_BY annotations found in project",
    "note": "Annotating shared members would enable compile-time lock checking"
  },
  "summary": {"total": 1, "errors": 0, "warnings": 1}
}
```

A real bug, but not *this* bug — wrong file, wrong symptom.

And `-Wthread-safety` found nothing because it *cannot*. It only checks rules you
have declared, and this project has no annotations, so it has no idea what is
supposed to be locked. Reported honestly rather than as a clean pass.

The escalation ladder is working as intended: cheap check ran, came back
inconclusive, move up.

---

## Step 4 — Escalate to ThreadSanitizer

```json
{"tool": "cpp_sanitize", "project_dir": ".", "sanitizer": "thread", "timeout_s": 180}
```

Internally: CMake configures a separate `build-tsan/` directory with
`-fsanitize=thread -g -O1`, builds it (about 40 seconds), then runs it with the
environment that travelled attached to the built binary — so the sanitizer flags
and the runtime options cannot drift out of sync.

```json
{
  "status": "completed",
  "build": {"ok": true, "duration_s": 41.2, "warnings": []},
  "run":   {"exit_code": 0, "duration_s": 3.1},
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
          "id": "T3", "op": "write", "size": 8,
          "locks_held": [],
          "frames": [
            {"function": "OrderBook::add_fill",     "file": "src/order_book.cpp", "line": 147},
            {"function": "FeedHandler::on_message", "file": "src/feed.cpp",       "line": 210}
          ]
        },
        {
          "id": "T1", "op": "read", "size": 8,
          "locks_held": ["OrderBook::book_mutex_"],
          "frames": [
            {"function": "OrderBook::snapshot", "file": "src/order_book.cpp", "line": 203}
          ]
        }
      ],
      "memory": {"kind": "heap", "allocated_at": {"file": "src/feed.cpp", "line": 44}}
    }
  ],
  "summary": {"total": 1, "by_category": {"data-race": 1}, "truncated": false},
  "raw_log_path": "/tmp/cpp-analysis-abc123/tsan.log.48211",
  "coverage_note": "TSan reports only code paths that executed during this run."
}
```

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

Re-runs `cpp_sanitize`. The build is incremental this time (4 seconds, CMake
caches), and findings come back empty.

It reports that honestly, because of `coverage_note`: ThreadSanitizer only
observed the code paths that ran. A clean result means *the exercised paths are
race-free*, not *the program is race-free*. An AI that says "fixed, guaranteed"
is overclaiming, and the returned data is shaped to discourage that.

---

## Step 6 — "It got slower"

> *"Latency went up. p99 went from 40µs to 110µs."*

Predictable. A mutex was just added to a hot path.

Now the profiler matters, and it is blocked. The AI surfaces the suggestion it
stored back in step 2:

> Profiling is disabled on this machine by `kernel.perf_event_paranoid=4`.
> Run `sudo sysctl -w kernel.perf_event_paranoid=1` and I'll re-check.

They run it. `cpp_profile` builds a release binary — critically with
`-fno-omit-frame-pointer`, because without frame pointers `perf`'s call graphs on
optimized builds are unusable garbage — then records and processes the samples.

```json
{
  "backend": "perf",
  "samples": 84210,
  "duration_s": 10.0,
  "hotspots": [
    {"function": "__lll_lock_wait", "self_pct": 34.1, "total_pct": 34.1,
     "note": "futex wait — lock contention"},
    {"function": "OrderBook::add_fill", "file": "src/order_book.cpp",
     "self_pct": 8.2, "total_pct": 47.9},
    {"function": "FeedHandler::decode", "self_pct": 19.4, "total_pct": 21.0}
  ],
  "flamegraph_path": "/tmp/cpp-analysis-abc123/flame.svg"
}
```

34% of runtime sitting in futex wait — threads queued up behind the new mutex.
The correctness fix created a performance bottleneck.

---

## Step 7 — The better fix

Now there are grounds for a real solution rather than a guess:
`std::atomic<uint64_t>` with `fetch_add(qty, std::memory_order_relaxed)`.
Race-free without serializing threads behind a lock.

Re-run ThreadSanitizer: clean. Re-run the profiler: futex wait gone, p99 back
under 45µs.

---

## What the arc demonstrates

Both halves of the tool were necessary and neither was sufficient.

- Static analysis alone: found an unrelated bug, missed this one
- ThreadSanitizer alone: found the race, then the fix silently made things slow
- Profiler alone: would have shown a slow mutex with no idea why it existed

The sequencing is the product. Any one tool in isolation is a partial picture,
and knowing which to reach for next given what the last one said is exactly the
judgment an AI with all of them wired up can supply.
