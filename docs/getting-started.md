# Getting started

From nothing to a working server, then a first caught bug. Ten minutes, most of
it waiting on installs.

---

## What you need

- **uv** — runs the Python side. `brew install uv` on macOS,
  `winget install astral-sh.uv` on Windows, or
  `curl -LsSf https://astral.sh/uv/install.sh | sh` anywhere.
- **A C++ toolchain** — macOS: `xcode-select --install` gives you clang.
  Linux: `sudo apt install clang` (gcc works too).
  Windows: `winget install LLVM.LLVM` **and** Visual Studio (or its Build
  Tools) with the "Desktop development with C++" workload — clang borrows
  MSVC's headers and linker. Put `C:\Program Files\LLVM\bin` on PATH; the
  LLVM installer does not do it for you.
- **CMake** — `brew install cmake` / `sudo apt install cmake` /
  `winget install Kitware.CMake`.
- **Claude Code** — the assistant you will wire the server into.
  [Install docs](https://docs.anthropic.com/en/docs/claude-code) if you do not
  have the `claude` command yet.

clang-tidy is optional. Stock macOS does not ship it; the server notices and
says so instead of failing. On Linux, `sudo apt install clang-tidy` if you want
that check. On Windows it arrives with LLVM.

Windows runs four of the seven analyses natively — AddressSanitizer, UBSan,
`-Wthread-safety`, clang-tidy. ThreadSanitizer and LeakSanitizer have no
Windows runtime in any compiler, and the perf profiler is Linux-only — but the
server bridges those three into WSL by itself the moment a distro there can
compile. One-time setup, all seven analyses:

```powershell
wsl --install -d Ubuntu
wsl -d Ubuntu -- sudo apt-get update
wsl -d Ubuntu -- sudo apt-get install -y clang llvm cmake ninja-build linux-tools-generic
```

(`llvm` matters: without `llvm-symbolizer` the two detectors still catch bugs
but report `<null>` frames instead of file and line. `linux-tools-generic` is
perf.) Restart the server and `capabilities` reports all seven. You keep
passing ordinary `C:\` paths; findings from the bridged analyses name files in
WSL form, `/mnt/c/...` meaning `C:\...`. Without WSL, the denials stand and
carry these same setup commands.
MinGW gcc (MSYS2) cannot link any sanitizer on Windows — the server picks
clang when both are installed.

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
uv run pytest -m "not integration"   # unit tests: no toolchain touched, everything simulated
uv run pytest -m integration         # compiles and runs real programs with YOUR compiler
```

(`make test` and `make integration` are the same two commands, if you have
make — Windows does not, which is why the guide spells them out.)

Both green means the server can do its job here. If the integration suite
fails, send back the failing output — it is the bug report.

## Wire it into an MCP client

This is a standard MCP server talking stdio — it has no opinion about which
assistant is on the other end. The command any client needs to launch it is:

```sh
uv run --directory /path/to/cpp-analysis-mcp cpp-analysis-mcp
```

Most clients want that wrapped in JSON, keyed by whatever name you choose
(`cpp-analysis` below), in a config file:

```json
{
  "mcpServers": {
    "cpp-analysis": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/cpp-analysis-mcp", "cpp-analysis-mcp"]
    }
  }
}
```

- **Cursor** — that block goes in `.cursor/mcp.json` (project) or
  `~/.cursor/mcp.json` (global).
- **Claude Desktop** — same shape, in its `claude_desktop_config.json`.
- **Claude Code** — skip the JSON and run
  `claude mcp add cpp-analysis -- uv run --directory /path/to/cpp-analysis-mcp cpp-analysis-mcp`
  from inside the clone.

Check your client's docs for its exact config file location and reload
mechanism — that part is the one thing that isn't standardized.

The first start does real work **on purpose**: the server compiles and runs a
small planted-bug program for each analysis to prove which ones actually work
on your machine — a version number claiming ThreadSanitizer support is not the
same as catching a race. The probes run concurrently and take a few seconds at
most (under one second measured on an M-series Mac). Results are cached in
`~/.cache/cpp-analysis-mcp`, keyed on the compiler, the OS release, and the
clang-tidy install, so later starts skip the probes; installing or upgrading a
tool retires the cache on its own.

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

Some analyses are platform-limited today (leak detection is Linux-only,
ThreadSanitizer's deadlock detector is inert on macOS, perf is a Linux kernel
tool). Do not memorize that: the `capabilities` tool is the honest list for the
machine you are on. And those limits belong to the host, not to the product —
Linux runs every analysis, and the container engine in
[ADR-0004](adr/0004-execution-engines.md) is how every OS gets the full set
the way Windows already gets three of them through WSL.

## Developing

```sh
make fmt           # format and autofix lint
make all           # lint + types + unit tests — what CI runs on every PR
make integration   # the real-toolchain suite, run it before pushing
```

On Windows (no make), the same steps spelled out:

```powershell
uv run ruff format .; uv run ruff check --fix .
uv run ruff check .; uv run mypy; uv run pytest -m "not integration"
uv run pytest -m integration
```

The layout is layered, each layer only allowed to talk downward — `server.py`
(protocol) → `context.py` (startup) → `battery.py` and `pipelines/` (workflow)
→ primitives (`build/`, `parsers/`, `platforms/`, `toolchains/`, `process.py`,
`wsl.py`). Two pieces of the next architecture already sit alongside: static
checks resolve through the plugin registry in `analyzers/`, and every finding
they report carries an identity from `store/`. The reasons live in
[architecture.md](architecture.md) and [architecture-v2.md](architecture-v2.md);
the layering rules are enforced by tests, so a change that breaks one fails
loudly rather than eroding quietly.

Read [CLAUDE.md](../CLAUDE.md) at the repo root before changing anything: it
holds the per-change ritual, the comment policy, and the decisions that are
frozen.

Branches: `feat/...` off `develop`, PR into `develop`, green CI required.
`main` only receives merges from `develop`.
