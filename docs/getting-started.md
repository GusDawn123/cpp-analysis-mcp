# Getting started

From nothing to a working server, then a first caught bug. Ten minutes, most of
it waiting on installs.

---

## What you need

- **uv** — runs the Python side. `brew install uv` on macOS, or
  `curl -LsSf https://astral.sh/uv/install.sh | sh` anywhere.
- **A C++ toolchain** — macOS: `xcode-select --install` gives you clang.
  Linux: `sudo apt install clang` (gcc works too).
- **CMake** — `brew install cmake` / `sudo apt install cmake`.
- **Claude Code** — the assistant you will wire the server into.
  [Install docs](https://docs.anthropic.com/en/docs/claude-code) if you do not
  have the `claude` command yet.

clang-tidy is optional. Stock macOS does not ship it; the server notices and
says so instead of failing. On Linux, `sudo apt install clang-tidy` if you want
that check.

## Download and set up

The repo is private for now, so your GitHub account needs access first.

```sh
git clone https://github.com/GusDawn123/cpp-analysis-mcp.git
cd cpp-analysis-mcp
uv sync
```

That is the whole install — `uv sync` creates the environment and pulls the two
runtime dependencies.

## Check it works on your machine

```sh
make test          # unit tests: no toolchain touched, everything simulated
make integration   # compiles and runs real programs with YOUR compiler
```

Both green means the server can do its job here. If `make integration` fails,
send back the failing output — it is the bug report.

## Wire it into Claude Code

From inside the clone:

```sh
claude mcp add cpp-analysis -- uv run --directory "$PWD" cpp-analysis-mcp
```

Then start a new Claude Code session and check the connection with `/mcp`.

The first start is slow **on purpose**: the server compiles and runs a small
planted-bug program for each analysis to prove which ones actually work on your
machine — a version number claiming ThreadSanitizer support is not the same as
catching a race. Takes a minute or two, cached in `~/.cache/cpp-analysis-mcp`,
so later starts are instant.

## Catch a first bug

Ask Claude:

> Using the cpp-analysis tools, which analyses work on this machine?

You should get a table of what was proved to work, with reasons for anything
that was not. Then paste this — the [README](../README.md)'s lost-update bug as
a complete program — and ask:

> Does this have a data race? Prove it with the tools, not by reading the code.

```cpp
#include <iostream>
#include <thread>
#include <vector>

int balance = 0;

void deposit(int amt) {
    balance += amt;        // no lock
}

int main() {
    std::vector<std::thread> ts;
    for (int i = 0; i < 100; i++) ts.emplace_back(deposit, 10);
    for (auto& t : ts) t.join();
    std::cout << balance << "\n";  // should print 1000. sometimes prints less.
}
```

Expect a ThreadSanitizer report naming the racing threads and the line they
collided on. That report — facts from an actual execution, not a plausible
guess from reading — is the entire point of the project.

Some analyses are platform-limited (leak detection is Linux-only, ThreadSanitizer's
deadlock detector is inert on macOS). Do not memorize that: the `capabilities`
tool is the honest list for the machine you are on.

## Developing

```sh
make fmt           # format and autofix lint
make all           # lint + types + unit tests — what CI runs on every PR
make integration   # the real-toolchain suite, run it before pushing
```

The layout is four layers, each only allowed to talk downward — `server.py`
(protocol) → `context.py` (startup) → `pipelines/` (workflow) → primitives
(tools, parsers, platforms). The reasons live in
[architecture.md](architecture.md); the layering rules are enforced by tests,
so a change that breaks one fails loudly rather than eroding quietly.

Branches: `feat/...` off `develop`, PR into `develop`, green CI required.
`main` only receives merges from `develop`.
