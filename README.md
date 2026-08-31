# cpp-analysis-mcp

An MCP server that lets AI coding agents actually test C++ code
instead of just reading it. It catches the bugs that don't show up
in source text — data races, memory leaks, use-after-free, undefined
behavior — tells you which findings *your* change added, and measures
where your latency actually goes instead of guessing. And if the
machine has no C++ tools installed at all, it runs everything inside
a Docker container: the only install is Docker itself.

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

Thirteen tools, six jobs:

| The problem                                        | Tools                                     | Cost    |
|----------------------------------------------------|-------------------------------------------|---------|
| Bug patterns and risky code, cheap first pass      | `static_check_file` / `_snippet`          | seconds |
| Findings *my* change added to the codebase         | `review` / `audit` / `get_finding`        | seconds |
| Data races, leaks, use-after-free, crashes — caught by running the code | `sanitize_file` / `_project` / `_snippet` | minutes |
| Latency: which functions eat the time              | `profile_file` / `_project`               | minutes |
| Two rewrites — which one is *really* faster        | `benchmark_variants`                      | minutes |
| The whole correctness battery in one call          | `full_check_file`                         | minutes |

Behind those rows: ThreadSanitizer watches data races happen,
AddressSanitizer catches use-after-free and buffer overflows,
LeakSanitizer catches leaks, UBSan catches undefined behavior like
signed overflow, clang-tidy and `-Wthread-safety` read the source,
and perf measures where the time goes. The agent starts cheap and
escalates only when it has to, or calls `full_check_file` for one
merged, deduplicated report. The thirteenth tool, `capabilities`,
reports what this machine can really run.

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

Two guided workflows ship as prompts, which clients surface as
slash commands. `checkup` runs the whole correctness pass and fixes
findings until the file comes back clean. `make-it-faster` walks the
loop this tool exists for: profile, write rewrite candidates, race
them, adopt only a proven winner, and prove it again.

## The review gate

Every other tool answers "is this code wrong?". The review gate
answers the question an agent has to answer before it says done:
did *I* break anything?

Point a linter at a real codebase and it returns hundreds of
findings that were there before anyone showed up. The signal an
agent needs is the handful its own edit just added.

Two calls. Once, on a clean checkout:

```
audit(project_dir)          scans everything git tracks,
                            records the result as a baseline
```

Then after any amount of work, as many times as you like:

```
review(project_dir, against="main")
```

`review` asks git which files changed, runs the compile-time
analyzers over exactly those, and subtracts the baseline. What
comes back is what your change added, and nothing else.

The report leads with counts by danger tier: critical, major,
minor, style, unrated. Witnessed beats suspected — critical is
reserved for what a runtime tool watched happen, so a compile-time
review never reports one, and a linter matching patterns in source
text tops out at major. Style is counted and indexed but never
spends a detail slot, so a hundred opinions about naming cannot
bury the one use-after-move.

Under the counts, four parts:

- **An index** of every new finding, one line each, with its tier
  and a fingerprint. `get_finding` fetches any one of them whole by
  that fingerprint, so a long report never crowds out the code you
  are reasoning about.
- **Full detail** for the top few, picked for danger and spread
  rather than the first few in file order. Each carries
  clang-tidy's own committable edit where the check offered one,
  and names the runtime tool that could watch this defect happen —
  silent where nothing runtime watches for it.
- **A plan trace**: what ran, what was skipped, and why, in words.
- **The compilation database** that decided how your files parsed,
  and a note when the root held more than one to choose between.

It also knows when to stop trusting itself. A baseline retires the
moment the world moves under it: a different compiler, changed
build flags, an edited .clang-tidy. Subtracting a stale baseline
would quietly hide real bugs, so instead review reports every
finding in the changed files and says the baseline is gone. No
baseline, no silent guess.

## Platform support

|                            | Linux | macOS   | Windows      |
|----------------------------|-------|---------|--------------|
| ASan, UBSan, static checks | yes   | yes     | yes          |
| TSan, LSan                 | yes   | yes     | yes, via WSL |
| Profiler (perf)            | yes   | not yet | yes, via WSL |
| Everything but the profiler, zero tools installed | yes, via Docker | yes, via Docker | yes, via Docker |

On Windows the server finds a WSL distro on its own and routes the
Linux-only tools through it. You keep passing normal C:\ paths.
When something cannot run, the server says so plainly and names the
command that would fix it.

And a machine with none of the tools installed still gets all of it:
with Docker running, the server runs every check inside a pinned
toolbox container instead -- same reports, host mounted read-only,
and each finding says which engine observed it. The profiler is the
one thing that stays out, because a container cannot see the host
kernel's performance counters, and the report says exactly that.

## Setup

You need Python 3.11+ and uv. For the C++ side: a compiler (clang or
gcc) -- or no tools at all and just Docker, which the server uses to
run the whole toolchain in a container when the host is missing it.

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
your machine, then caches the results. The container engine passes
the same audition: probes run inside the image before anything is
routed there. On a machine relying on Docker, pull the toolbox once:

```bash
docker pull ghcr.io/gusdawn123/cpp-analysis-toolbox:0.1
```

You never have to remember this -- any unavailable analysis names
this exact command in its status.

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
    server.py        thirteen tools, no logic of its own
        |
    battery.py       full_check_file only: fans out to every
        |            correctness pipeline below, in parallel
    pipelines/       the recipes: review, check, sanitize, profile
        |
    planner/         what runs and why: git scope, gates, cost
        |            tiers, parallel dispatch, the plan trace
        |
    analyzers/       one contract, N plugins, each owning its own
        |            invocation and its own gate
        |
    store/           where findings become one thing: fingerprints,
        |            baselines, the subtraction, tiers, ranking
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
   +-- container.py  renting Linux from Docker for whatever
                     the host cannot run at all
```

Imports point one way, down. The planner is ordinary deterministic
code and never an LLM, because a review gate that plans differently
on identical input is not a gate. The only LLM in the system is the
agent calling it, and no layer here holds an API key.

The layer rules are enforced by tests that parse the source tree,
so they cannot rot: server.py holds no control flow, only
process.py may spawn a subprocess, parsers never touch a file,
analyzer plugins may not import the server or the MCP SDK, and
exactly one module is allowed to ask what machine this is.

The reasoning behind the load-bearing choices lives in
[docs/architecture.md](docs/architecture.md),
[docs/architecture-v2.md](docs/architecture-v2.md),
[docs/design-patterns.md](docs/design-patterns.md), and the ADRs in
[docs/adr](docs/adr).

## Tests

845 unit tests run anywhere, no compiler needed. Integration tests
compile and run fixtures with planted bugs, and golden outputs
captured on Linux, macOS, and Windows keep every parser honest.

The review gate has an acceptance test with nothing faked in it: a
real git repository whose first commit already carries a real bug,
audited with a real clang-tidy, then a second bug planted on top.
Review has to come back with exactly the planted one.

```bash
make test
```

## Grading the agent

Tests prove the tools work. Nothing there proves an agent *uses*
them well, and that is its own failure mode: reaching for a
sanitizer before a linter, calling a clean compile-time result an
all-clear, claiming a rewrite is faster without racing it.

`evals/` grades that. Twenty-three tasks pair a prompt a user might
really send with the calls that should follow, and a grader reads
the session transcript to say which habits held.

The fake driver replays checked-in recordings, costs nothing, and
rides the unit suite. The real driver composes the headless session
and refuses to start one without being told to spend, so the
scorecard for a live agent is deliberately still blank.

## License

MIT
