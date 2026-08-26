# cpp-analysis-mcp

An MCP server that lets AI coding agents actually test C++ code
instead of just reading it.

## The problem

Some C++ bugs are invisible in source code. This looks fine:

```cpp
void deposit(int amt) {
    balance += amt;   // no lock
}
```

Run it from 100 threads and it loses updates. The bug lives in
timing, not in the text, so an AI that only reads the file will
call it clean.

The tools that catch these bugs already exist and are excellent.
They are just command line programs that no AI assistant is wired
up to. This project is that wiring.

## What it does

Ten tools, four questions:

| Question                  | Tools                                     | Cost    |
|---------------------------|-------------------------------------------|---------|
| Does anything look wrong? | `static_check_file` / `_snippet`          | seconds |
| Is it actually wrong?     | `sanitize_file` / `_project` / `_snippet` | minutes |
| Where is it slow?         | `profile_file` / `_project`               | minutes |
| Which rewrite is faster?  | `benchmark_variants`                      | minutes |
| All of it, in one call    | `full_check_file`                         | minutes |

The agent starts cheap and escalates only when it has to, or calls
`full_check_file` to run both compile-time checks and all four
sanitizers in parallel and get one merged, deduplicated report. The
tenth tool, capabilities, reports what this machine can really run.

benchmark_variants is the one that ends arguments: it races up to
five versions of a program on your machine, feeds them the same
workload, and rejects any variant whose output stopped matching the
baseline. A rewrite that got faster by answering differently is not
faster, it is wrong.

Profiles come back interpreted, not just ranked. Instead of a wall
of mangled symbols, the report says things like "42% of self time
inside std::map tree machinery", names the rewrite families worth
trying, and tells you how much to trust the numbers given the sample
count.

Under the hood: ThreadSanitizer for data races, AddressSanitizer
for memory corruption, LeakSanitizer for leaks, UBSan for undefined
behavior, clang-tidy and -Wthread-safety at compile time, and perf
for profiling.

## Platform support

|                            | Linux | macOS   | Windows      |
|----------------------------|-------|---------|--------------|
| ASan, UBSan, static checks | yes   | yes     | yes          |
| TSan, LSan                 | yes   | yes     | yes, via WSL |
| Profiler (perf)            | yes   | not yet | yes, via WSL |

On Windows the server finds a WSL distro on its own and routes the
Linux-only tools through it. You keep passing normal C:\ paths.
When something cannot run, the server says so plainly and names the
command that would fix it.

## Setup

You need Python 3.11+, uv, and a C++ compiler (clang or gcc).

```bash
git clone https://github.com/GusDawn123/cpp-analysis-mcp
cd cpp-analysis-mcp
uv sync
```

Then register it with your agent. For Claude Code:

```bash
claude mcp add cpp-analysis -- uv run --directory <path-to-repo> cpp-analysis-mcp
```

For any other MCP client, add the same command to its config file:

```json
{ "mcpServers": { "cpp-analysis": {
    "command": "uv",
    "args": ["run", "--directory", "<path-to-repo>", "cpp-analysis-mcp"]
}}}
```

The first start takes a minute. The server compiles and runs a tiny
buggy program for each analysis to prove the tool really works on
your machine, then caches the results.

## Why you can trust an empty result

The server never takes a tool's word for it. At startup it plants a
real bug (a data race, a leak, an overflow) and requires each
detector to catch it. A detector that stays quiet on a known bug is
reported as unavailable, with the reason and the install command
that fixes it.

So an empty report means a detector that provably works here saw
nothing. That difference matters, because a silently broken tool
looks exactly like clean code.

## Architecture

```
your AI agent (Claude Code, Cursor, anything that speaks MCP)
        |
    server.py        ten tools, no logic of its own
        |
    pipelines/       the recipes: check, sanitize, profile
        |
   +----------+-------------+
   |          |             |
 build/    process.py    parsers/
 compiles  runs tools    raw tool output in,
 the code  safely, with  clean findings out
   |       timeouts
   |
   +-- platforms/    what Linux, macOS, and Windows each need
   +-- toolchains/   what clang and gcc each need
   +-- wsl.py        Windows borrowing Linux for what it lacks
```

The layer rules are enforced by tests that parse the source tree,
so they cannot rot: server.py holds no control flow, only
process.py may spawn a subprocess, parsers never touch a file, and
exactly one module is allowed to ask what machine this is.

## Tests

507 unit tests run anywhere, no compiler needed. Integration tests
compile and run fixtures with planted bugs, and golden outputs
captured on Linux, macOS, and Windows keep every parser honest.

```bash
make test
```

## License

MIT
