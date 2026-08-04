# cpp-analysis-mcp — Design

**Date:** 2026-08-04
**Status:** In progress — architecture settled, tool surface pending

An MCP server that gives an AI agent access to C++ correctness and performance
tooling: sanitizers, static analysis, and profilers.

## Goal

C++ concurrency and memory bugs cannot be found by reading code. They are
properties of runtime timing and memory state, so an agent limited to reading
source misses them entirely. This server closes that gap by letting the agent
build, instrument, run, and measure real code.

Published publicly on GitHub. Strangers install it on machines we cannot test,
which drives most of the design constraints below.

## Decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Language | Python 3.11+ | The work is subprocess orchestration and text parsing. Distribution via `uvx`. |
| Build model | CMake-first, plus a single-file fast path | Covers most real C++ projects without absorbing Bazel/Meson/Make. |
| Platforms | Linux + macOS first, Windows after | Windows lacks TSan entirely and needs a separate story. |
| Scope | Correctness *and* performance | Both "is it wrong" and "is it slow". |

### Platform reality

| Capability | Linux | macOS (arm64) | Windows |
|---|---|---|---|
| ThreadSanitizer | yes | yes | **no** — WSL required |
| AddressSanitizer | yes | yes | yes (MSVC `/fsanitize=address`) |
| UBSan | yes | yes | partial (clang-cl only) |
| LeakSanitizer | yes | verify on arm64 | no |
| clang-tidy | yes | yes (via brew llvm) | yes |
| `-Wthread-safety` | clang only | yes | clang-cl only |
| Profiler | `perf` | `xctrace` | ETW / WPR |

## Architecture

### Variation is tiered, not uniform

The reason this does not become 5 tools x 3 platforms = 15 implementations:

- **Tier 0 — identical everywhere.** Driving CMake, invoking clang-tidy, parsing
  TSan/ASan output. TSan's report format is byte-identical across platforms
  because it is the same LLVM runtime. No platform code at all.
- **Tier 1 — same concept, different details.** Sanitizer flag syntax, which
  sanitizers exist, symbolizer selection. One implementation plus a per-platform
  config table.
- **Tier 2 — genuinely different programs.** Profiling. `perf`, `xctrace`, and
  ETW share nothing. Real per-platform implementations behind one interface.

### Layers

```
MCP tool surface       server.py               protocol only, no logic
Orchestration          pipelines/              multi-step workflows
Primitives             build/ parsers/ platforms/ capabilities process
Host tools             clang, cmake, perf, xctrace
```

The platform seam sits low on purpose. Splitting at the top
(`macos_server.py`, `linux_server.py`) would triple the bug surface, since a
parser fix would need applying three times and would drift. Splitting low means
each platform file holds only what genuinely differs — roughly 150-200 lines —
while the ~80% that is shared stays shared.

### Repository structure

```
cpp-analysis-mcp/
├── pyproject.toml · README.md · LICENSE
├── src/cpp_analysis_mcp/
│   ├── server.py           MCP schemas, delegate, serialize
│   ├── models.py           Finding, Hotspot, Capability, BuiltBinary, Context
│   ├── capabilities.py     host probing via smoke tests
│   ├── process.py          subprocess, timeouts, confinement
│   ├── pipelines/          sanitize · static_check · profile · snippet
│   ├── build/              cmake · single_file
│   ├── parsers/            tsan · asan · ubsan · clang_tidy
│   └── platforms/          base · darwin · linux · windows
└── tests/
    ├── unit/               mirrors src/, no toolchain needed
    ├── integration/        needs a real compiler, platform-gated
    └── fixtures/
        ├── cpp/            deliberately-buggy programs
        └── golden/         captured real tool output
```

### Invariants

These are enforced by tests in `tests/unit/test_architecture.py`, not by
convention alone.

1. **Acyclic layers.** Pipelines may depend on primitives. No primitive imports
   from `pipelines/`. No pipeline imports another pipeline.
2. **No logic in `server.py`.** Tool handlers define schema and delegate. Any
   branching means logic leaked out of a pipeline.
3. **No global platform lookup.** Primitives never call `detect_platform()`.
   Platform arrives as a parameter. This is what lets the Linux and Windows
   backends be developed and tested from a macOS machine.
4. **Parsers are pure functions.** Text in, `Finding[]` out. No subprocess, no
   filesystem. Tested against committed golden files, so the Linux `perf` parser
   is fully testable on macOS.

### Key models

```python
@dataclass(frozen=True)
class Context:
    platform: Platform            # resolved once at startup
    capabilities: Capabilities    # probed once, refreshable
    workspace: Path               # confinement root
    default_timeout_s: int

@dataclass(frozen=True)
class BuiltBinary:
    path: Path
    build_dir: Path
    sanitizer: SanitizerKind | None
    runtime_env: Mapping[str, str]   # TSAN_OPTIONS, symbolizer — travels WITH the binary
    compile_commands: Path | None    # clang-tidy reuses this
    warnings: list[Finding]          # -Wthread-safety fires at build time
```

`BuiltBinary` binding the binary to its required environment makes a real bug
unrepresentable: building with TSan but running without `TSAN_OPTIONS` produces
zero findings and looks identical to clean code. A false all-clear is the worst
failure this tool could have.

### Capability probing

`capabilities.py` does not read version strings. It compiles and runs a five-line
program with each sanitizer to verify it genuinely works on that host. Version
sniffing lies; a smoke test does not.

Results cache to disk, keyed by a fingerprint of compiler path, compiler version,
and OS release, so only the first run pays the cost.

## Open questions

- MCP tool surface: exact tool list, arguments, return shapes
- `Finding` and `Hotspot` field-level schemas
- Output volume control — a program with 50 races must not flood agent context
- Long-running builds and MCP progress reporting
- Safety model for compiling and executing untrusted code
- v1 cut line

## Prior art

The research survey found no existing MCP server wrapping sanitizers, Valgrind,
`perf`, or clang-tidy diagnostics. Several exist for debuggers (`gdb-mcp`,
`MDB-MCP`, `lldb-mcp-server`, `claude-debugs-for-you`) and one for clangd code
navigation (`clangd-mcp-server`). The analysis and profiling space is open.
