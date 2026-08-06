# Architecture

Written to be readable without prior context on the project. Start with the
[README](../README.md) if you have not.

---

## The core problem to solve

Roughly five analysis capabilities, three operating systems. Done naively that is
fifteen separate implementations to write and keep working. Projects in this
space usually die there.

The way out is noticing that **the variation between platforms is not evenly
distributed**. Some things are identical everywhere, some differ slightly, and
only one thing is genuinely different. Building one uniform abstraction over all
of it would be the wrong shape.

### Tier 0 — identical on every platform

Driving CMake. Invoking clang-tidy. Parsing ThreadSanitizer and AddressSanitizer
output.

ThreadSanitizer's report format is byte-for-byte identical on macOS and Linux,
because it is literally the same LLVM runtime library on both. Writing a
platform abstraction around this would be pure ceremony.

### Tier 1 — same idea, different details

Sanitizers exist everywhere but differ in:
- flag syntax (`-fsanitize=address` vs Microsoft's `/fsanitize=address`)
- which ones exist (Windows has no ThreadSanitizer)
- how symbol names get resolved (macOS uses `atos`, Linux uses `llvm-symbolizer`)

One implementation, plus a small per-platform table of differences.

### Tier 2 — genuinely different programs

Profiling. `perf` (Linux), `xctrace` (macOS), and ETW (Windows) share nothing —
different invocation, different output format, different permission model. These
need real separate implementations behind a common interface.

---

## Layers

```
  MCP tool surface        server.py
  what the AI sees                            protocol only, zero logic
  ─────────────────────────────────────────────────────────────────────
  Orchestration           pipelines/
  multi-step workflows                        "build, then run, then parse"
  ─────────────────────────────────────────────────────────────────────
  Primitives              build/  parsers/  platforms/
                          capabilities  process
  ─────────────────────────────────────────────────────────────────────
  Host tools              clang, cmake, perf, xctrace
```

### Why the platform split sits low

The instinct is to split at the top — `macos_server.py`, `linux_server.py`. It
feels like clean separation. It is a trap.

Splitting at the top triples the bug surface. A fix to output parsing now has to
be applied three times, and the three copies drift apart over months. You end up
maintaining three products.

Splitting low means each platform file contains *only* what genuinely differs —
about 150–200 lines each — while the roughly 80% that is shared stays in one
place, fixed once.

```
platforms/base.py        the contract every platform must satisfy

platforms/linux.py       llvm-symbolizer
                         perf profiler backend
                         detects kernel.perf_event_paranoid blocking

platforms/darwin.py      finds brew's llvm (clang-tidy is NOT on PATH
                           by default on macOS)
                         atos symbolization
                         xctrace profiler backend

platforms/windows.py     ETW profiler backend
                         WSL detection and guidance
```

---

## Operating system and compiler are separate axes

An early draft of this design folded compiler differences into the platform
files. That was wrong. They vary independently:

- **Which OS** you are on determines the profiler, the symbolizer, and where
  tools are installed.
- **Which compiler** you use determines the sanitizer flags and which analyses
  are available at all.

Linux can use clang *or* gcc. macOS only has clang. Windows has MSVC or clang-cl.
Folding these together would mean duplicating gcc knowledge into the Linux file
and clang knowledge into all three.

So they get separate directories:

```
platforms/       OS concerns
  base.py  linux.py  darwin.py  windows.py

toolchains/      compiler concerns
  base.py  clang.py  gcc.py  msvc.py
```

### What each compiler supports

| Capability | clang | gcc | MSVC |
|---|---|---|---|
| ThreadSanitizer | yes | **yes** | no |
| AddressSanitizer | yes | **yes** | yes |
| UndefinedBehaviorSanitizer | yes | **yes** | no |
| LeakSanitizer | yes | **yes** | no |
| `-Wthread-safety` | yes | **no equivalent** | no |
| clang-tidy | native | works, with a caveat | works, with a caveat |

**gcc support is close to free for sanitizers.** GCC does not implement its own —
it vendors LLVM's `compiler-rt` runtime library. Same code, same report format.
The parsers written for clang output work on gcc output essentially unchanged,
which is the expensive part of supporting a second compiler and we get it for
nothing.

Two real differences to handle:

- GCC's copy of the sanitizer runtime lags LLVM's, since it is merged
  periodically rather than continuously. Minor format drift is possible across
  versions, which is a reason to keep captured sample output from both compilers
  in the test fixtures rather than assuming they match forever.
- Some distributions ship the runtimes as separate packages (`libtsan0`,
  `libasan8`). Capability detection catches this — the smoke test fails to link
  and reports the missing package by name rather than producing a confusing
  error.

**`-Wthread-safety` is the one real loss.** GCC has no equivalent, not a weaker
version. On a gcc toolchain that analysis reports as unavailable with the reason
stated, rather than silently returning no findings — which would read as "your
code is fine."

**clang-tidy works regardless of build compiler**, since it reads
`compile_commands.json` rather than caring who compiled. The caveat is that
gcc-specific flags clang does not recognize (`-mno-fma4`, various `-f` options)
cause it to error out. The fix is a known one: filter unrecognized flags out of
the compilation database before invoking it.

### Why clang is still the default

1. **It is the only compiler present on all three target platforms.** One primary
   path instead of three.
2. **`-Wthread-safety` is clang-only**, and it is the only tool in the set that
   catches lock bugs at compile time in seconds.
3. **The sanitizers are LLVM's**, so clang runs the reference implementation
   while gcc runs a periodic snapshot of it.
4. **clang-tidy is native** — no flag filtering needed.

But clang is a *preference*, never a requirement. Plenty of C++ projects build
only under gcc and would fail or subtly misbehave if forced onto clang. The
server detects what the project already uses, works with it, and reports honestly
what that choice costs.

---

## Four rules that keep it from tangling

These are enforced by tests that inspect the code's structure, not by discipline.
Discipline erodes; a failing test does not.

**1. Layers only point downward.**
Pipelines may use primitives. No primitive may import from `pipelines/`. No
pipeline may import another pipeline.

**2. No logic in `server.py`.**
Tool handlers define their inputs and hand off. If a handler grows an `if`
statement, workflow logic has leaked into the wrong layer.

**3. Nothing looks up the platform globally.**
No primitive ever calls `detect_platform()`. The platform is passed in as an
argument.

This one has a concrete payoff: it is what lets the Linux and Windows code be
developed and tested from a macOS laptop. A test can just hand `LinuxPlatform()`
to the code and check the result. If the code looked up the platform itself, it
would always find macOS and the test would be impossible.

**4. Parsers are pure functions.**
Text in, findings out. No subprocess calls, no filesystem access.

This is what makes the cross-platform goal actually testable. Capture real `perf`
output on Linux *once*, commit that text file, and the Linux profiler parser is
then fully testable on macOS, in milliseconds, with no Linux anywhere.

---

## Two data structures worth explaining

### `BuiltBinary` — binds a binary to the environment it needs

```python
@dataclass(frozen=True)
class BuiltBinary:
    path: Path
    build_dir: Path
    sanitizer: SanitizerKind | None
    runtime_env: Mapping[str, str]   # TSAN_OPTIONS, symbolizer path
    compile_commands: Path | None
    warnings: list[Finding]          # -Wthread-safety fires at build time
```

This is not just plumbing. It makes a specific bad bug impossible to write.

The decision to compile with `-fsanitize=thread` and the knowledge that the
process needs `TSAN_OPTIONS` set are the same decision. If those live in
different places, eventually you get a build with the sanitizer and a run
*without* the options — which reports **zero findings** and is indistinguishable
from clean code.

A false all-clear is the worst possible failure for a tool like this. Binding the
binary to its environment in one immutable object means you cannot hold one
without the other.

Note `warnings` — the build step already produces findings, because
`-Wthread-safety` reports at compile time, not runtime.

### `Context` — resolved once, passed down

```python
@dataclass(frozen=True)
class Context:
    platform: Platform            # the OS does not change mid-run
    capabilities: Capabilities    # what this machine can actually do
    workspace: Path               # where every tool call builds and runs
```

Prevents these being threaded through every function as separate arguments.

An early draft carried a `default_timeout_s` here too. That was the wrong shape: a
sanitized run is minutes and a syntax check is seconds, so one number is too tight for the
first and meaningless for the second. Each pipeline knows what its own steps cost and owns
its own.

The workspace covers every *request*, which is not quite everything. Capability detection
compiles and runs its probes at startup, before there is a request to attribute them to,
and each of those gets its own scratch directory under the system temp dir.

---

## Capability detection

`capabilities.py` does not check version numbers. It **compiles and runs a
five-line test program** with each sanitizer to see whether it genuinely works on
this machine.

Version sniffing lies constantly — a compiler can report a version that supports
ThreadSanitizer while the runtime library is missing, or the platform is
unsupported, or a system setting blocks it. A smoke test cannot lie.

Results cache to disk, fingerprinted on compiler path, compiler version, and OS
release, so only the first run pays the cost.

The payoff is honest failure. Instead of a cryptic permissions error the AI then
hallucinates an explanation for, the user gets:

```json
"profiler": {
  "backend": "perf",
  "available": false,
  "reason": "kernel.perf_event_paranoid = 4 blocks unprivileged profiling",
  "suggestion": "sudo sysctl -w kernel.perf_event_paranoid=1"
}
```

That is a real Linux setting Ubuntu ships in a state that blocks profiling. Most
of the hard-won value in this project will be gotchas exactly like it, found one
platform at a time.

---

## A principle: facts, not advice

The server reports what it observed. It does not suggest code fixes.

It is tempting to have a race report end with "consider making this atomic." But
the server sees one finding — it does not know whether the variable is in a hot
loop, what locking convention the surrounding code follows, or whether the real
fix is restructuring ownership. Advice from that position is frequently wrong,
and an AI that learns to trust wrong advice is worse off than one given none.

Instead the tool returns maximally decision-relevant *facts*. For a data race
that means reporting which locks each thread was holding:

```json
"threads": [
  {"id": "T3", "op": "write", "locks_held": []},
  {"id": "T1", "op": "read",  "locks_held": ["OrderBook::book_mutex_"]}
]
```

One thread held the lock, the other did not. That *is* the diagnosis, delivered
as an observation rather than an opinion, and the AI does the reasoning it is
actually good at.

**One exception:** the server may advise about *itself*. "Static analysis was
inconclusive; ThreadSanitizer would settle this" is fine — it knows its own
tools. It just does not know your codebase.

---

## Who decides what runs

The AI does.

MCP gives the assistant a menu of tools and their descriptions. The assistant
chooses which to call and in what order, based on the conversation. The server
orchestrates each individual *operation*; it never orchestrates the
*investigation*.

A consequence worth stating: **tool descriptions are load-bearing.** They are the
only information the AI uses to choose. A vague description produces wrong tool
choices no matter how good the implementation underneath.

The intended usage pattern is an escalation ladder, cheapest first:

```
  compile-time checks        seconds       clang-tidy, -Wthread-safety
         ↓ inconclusive
  sanitizers                 minutes       ThreadSanitizer, AddressSanitizer
         ↓ correct but slow
  profiler                   minutes       perf / xctrace
```

Running the profiler first would be like calling forensics before checking
whether the door was locked.

---

## Planned structure

**Not yet created.** This is the intended layout, recorded here for review.

```
cpp-analysis-mcp/
├── pyproject.toml · README.md · LICENSE
│
├── src/cpp_analysis_mcp/
│   ├── server.py           MCP tool definitions, delegate, serialize
│   ├── models.py           Finding, Hotspot, Capability, BuiltBinary, Context
│   ├── capabilities.py     host probing via smoke tests
│   ├── process.py          subprocess, timeouts, confinement
│   │
│   ├── pipelines/          the orchestrators
│   │   └── sanitize · static_check · profile
│   │       (snippets are an entry point of each pipeline, not a pipeline of
│   │        their own: a snippet is a source shape, and rule 1 -- no
│   │        pipeline imports another -- would leave a snippet module
│   │        rebuilding the same build-run-parse chain instead of reusing it)
│   │
│   ├── build/              cmake · single_file
│   ├── parsers/            tsan · asan · ubsan · clang_tidy · perf · xctrace
│   ├── platforms/          base · linux · darwin · windows      (OS concerns)
│   └── toolchains/         base · clang · gcc · msvc            (compiler concerns)
│
└── tests/
    ├── unit/               mirrors src/ — no toolchain needed, runs anywhere
    │   └── parsers/ · platforms/ · toolchains/
    ├── integration/        needs a real compiler, skipped where unavailable
    └── fixtures/
        ├── cpp/            deliberately-buggy programs with known bugs
        │                   data_race · heap_overflow · use_after_free
        │                   deadlock · hot_loop
        └── golden/         captured real tool output, per compiler and OS
                            tsan_data_race.linux-clang.txt
                            tsan_data_race.linux-gcc.txt
                            tsan_data_race.darwin-clang.txt
```

Two things worth noting about `tests/`:

**The split is by what a test needs, not just by what it covers.** Unit tests
require no compiler and run in milliseconds on any machine. Integration tests
need a real toolchain, so they are slow, platform-dependent, and skip cleanly
where the tools are absent rather than failing.

**`fixtures/cpp/` is the correctness ground truth.** Each program contains a
*known* bug, so "did ThreadSanitizer find the race in `data_race.cpp`" is a real
pass/fail rather than a judgement call.

**`fixtures/golden/` is what makes cross-platform development possible from one
machine.** Capture real `perf` output on Linux once, commit the text file, and
the Linux parser is then fully testable on macOS — where `perf` cannot run at
all.
