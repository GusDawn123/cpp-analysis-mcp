# cpp-analysis-mcp

**Status: design phase. Nothing is built yet.** These documents exist to get the
architecture right before writing code.

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

Two kinds of question:

**"Is my code wrong?"**
- **ThreadSanitizer** finds data races like the one above
- **AddressSanitizer** finds memory corruption — buffer overflows, use-after-free
- **clang-tidy** finds suspicious patterns without running anything
- **`-Wthread-safety`** catches missing locks at compile time, if you annotate

**"Is my code slow?"**
- **Profilers** measure where a running program actually spends its time
  (`perf` on Linux, `xctrace` on macOS, ETW on Windows)

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

1. **[docs/architecture.md](docs/architecture.md)** — how it is built and why
2. **[docs/workflow-scenario.md](docs/workflow-scenario.md)** — a full worked
   example of somebody using it
3. **[docs/open-questions.md](docs/open-questions.md)** — decisions still open,
   where input is wanted

---

## Decisions made so far

| Decision | Choice | Why |
|---|---|---|
| Language | Python | The work is running command-line tools and parsing their text output. Python is best-in-class at both. |
| Build system | CMake first | Covers most real C++ projects. Bazel/Meson/Make deferred. |
| Compiler | clang default, gcc supported | clang is the only compiler on all three platforms and `-Wthread-safety` is clang-only. But gcc vendors LLVM's sanitizer runtime, so gcc support costs almost nothing. See [architecture.md](docs/architecture.md#operating-system-and-compiler-are-separate-axes). |
| Platforms | Linux + macOS first, Windows later | Windows has no ThreadSanitizer at all and needs its own story. |
| Distribution | Public GitHub, installed via `uvx` | No existing MCP server wraps these tools. |
