# cpp-analysis-mcp

**Status: working.** Sanitizers and compile-time checks run end to end behind an
MCP server; profilers are not built yet.
[docs/getting-started.md](docs/getting-started.md) takes you from clone to first
caught bug.

A tool that lets an AI coding assistant actually *test* C++ programs for
concurrency and memory bugs, instead of only reading the source.

---

## The problem

Some C++ bugs cannot be found by reading code. Here is one:

```cpp
int balance = 0;

void deposit(int amt) {
    balance += amt;        // no lock
}

int main() {
    std::vector<std::thread> ts;
    for (int i = 0; i < 100; i++) ts.emplace_back(deposit, 10);
    for (auto& t : ts) t.join();
    std::cout << balance;  // should print 1000. sometimes prints less.
}
```

`balance += amt` looks like one operation but is three: read, add, write. Two
threads can interleave and lose an update. The bug depends entirely on **timing**
— which thread the operating system happens to schedule first — so it might
appear once in fifty runs and never in testing.

An AI reading this file sees plausible code. There is nothing textually wrong
with it. The bug lives in the execution, not the text.

The tools that *do* find these bugs already exist and are excellent. They are
just command-line programs that no AI assistant is currently wired up to. This
project is that wiring.

---

## What it does

Three kinds of question.

### "Is my code wrong?"

- **ThreadSanitizer** finds data races like the one above
- **AddressSanitizer** finds memory corruption — buffer overflows, use-after-free
- **LeakSanitizer** finds memory that is allocated and never released
- **UndefinedBehaviorSanitizer** finds the bugs that appear only once you turn
  optimization on
- **clang-tidy** finds suspicious patterns without running anything
- **`-Wthread-safety`** catches missing locks at compile time, if you annotate

### "Is my code slow?"

- **Profilers** measure where a running program actually spends its time
  (`perf` on Linux, `xctrace` on macOS, ETW on Windows)

### "Is my code wasteful?"

Separate from whether memory handling is *broken* — this is how much you use,
where it comes from, and whether you are allocating somewhere you cannot afford
to.

- **Heap profilers** show which lines allocate the most, and how usage grows over
  time (`heaptrack` on Linux, Instruments Allocations on macOS)
- **Allocation-in-hot-path detection** — a single `malloc` in a latency-critical
  path can cost more than everything else in the function
- **False sharing detection** (`perf c2c`) — when two threads write to two
  *different* variables that happen to share a 64-byte cache line. No race, code
  is correct, throughput drops 10x, and every other tool here is blind to it.
  Largely x86-only; ARM support is thin and macOS has no equivalent.

---

## It works with the build you already have

You do not switch compilers or restructure your project to use this.

The server detects what your project already compiles with and adapts —
clang or gcc, and MSVC where it can. Where a given compiler genuinely cannot do
something, it says so plainly instead of returning an empty result that looks
like a clean bill of health.

| | clang | gcc | MSVC |
|---|---|---|---|
| ThreadSanitizer | yes | yes | no — needs WSL |
| AddressSanitizer | yes | yes | yes |
| UBSan / LeakSanitizer | yes | yes | no |
| `-Wthread-safety` | yes | no equivalent | no |
| clang-tidy | yes | yes | yes |

gcc support costs almost nothing because gcc does not write its own sanitizers —
it vendors LLVM's runtime library. Same code, same report format, so the parsers
work unchanged.

The value is not any single tool. It is that **no single tool finds most bugs** —
their coverage barely overlaps. Given the buggy program above:

| Tool | What it says |
|---|---|
| ThreadSanitizer | Data race on `balance`, thread T3 wrote while T7 read, no lock held |
| AddressSanitizer | Nothing. There is no memory corruption here. |
| `-Wthread-safety` | Nothing, unless you annotated `balance` as needing a lock |
| A profiler | Nothing. The code is wrong, not slow. |

Knowing *which* tool to reach for given a symptom is the hard part, and it is
exactly what an AI with all of them wired up can do well.

---

## Glossary

Terms used throughout these docs.

**MCP (Model Context Protocol)** — a standard way to give an AI assistant new
abilities. You write a program (an "MCP server") that exposes functions; the
assistant can call them. Think of it as a plugin interface for AI.

**Sanitizer** — not a separate program. A compiler feature. You compile with
`-fsanitize=thread` and the compiler injects checking code throughout your
binary. The resulting program does everything it normally does, plus watches
itself and reports violations with a stack trace.

**Data race** — two threads touching the same memory at the same time, at least
one writing, with no lock coordinating them. Produces corrupted or wrong values.

**Static analysis** — examining code without running it. Fast, but blind to
anything that depends on runtime behavior.

**Dynamic analysis** — examining a program while it runs. Catches real behavior,
but only on the code paths that actually execute.

**Profiling** — measuring a running program to find where its time goes.
Typically by sampling: interrupting the program thousands of times per second and
recording which function is executing. Aggregate those samples and you know your
bottleneck.

**Instrumented binary** — a program compiled with extra checking code inside it.
Sanitizers work this way, which is why you cannot point one at an existing
program — you have to rebuild.

---

## Documents

Read in this order:

1. **[docs/getting-started.md](docs/getting-started.md)** — install it, wire it
   into an assistant, catch a first bug
2. **[docs/architecture.md](docs/architecture.md)** — how it is built and why
3. **[docs/workflow-scenario.md](docs/workflow-scenario.md)** — a full worked
   example of somebody using it
4. **[docs/open-questions.md](docs/open-questions.md)** — decisions still open,
   where input is wanted

---

## Branches

- **`main`** — production. Only receives merges from `develop`.
- **`develop`** — integration, the default branch. Feature branches
  (`feat/...`) PR into here after review and green CI.

## Decisions made so far

| Decision | Choice | Why |
|---|---|---|
| Language | Python | The work is running command-line tools and parsing their text output. Python is best-in-class at both. |
| Build system | CMake first | Covers most real C++ projects. Bazel/Meson/Make deferred. |
| Compiler | clang default, gcc supported | clang is the only compiler on all three platforms and `-Wthread-safety` is clang-only. But gcc vendors LLVM's sanitizer runtime, so gcc support costs almost nothing. See [architecture.md](docs/architecture.md#operating-system-and-compiler-are-separate-axes). |
| Platforms | Linux + macOS first, Windows later | Windows has no ThreadSanitizer at all and needs its own story. |
| Distribution | Public GitHub, installed via `uvx` | No existing MCP server wraps these tools. |
