# Windows support — design

Date: 2026-08-11. Status: approved approach, pending user review of this spec.

## Goal

The server starts, probes, and serves tools natively on Windows with LLVM clang,
reporting four analyses as available and two as honestly impossible. Unit tests
and CI stay green on Windows so Linux-side changes cannot silently break it.

## What Windows can and cannot do

Decided with the user on 2026-08-11:

- **Native only.** No WSL bridge. TSan and LSan are denied with a reason and a
  "use WSL" suggestion, exactly the shape `Platform.denied` already exists for.
- **LLVM clang only.** No MSVC (`cl.exe`) toolchain: MSVC has no
  `-Wthread-safety` and no UBSan, so it would add code without adding checks.
  The existing `toolchains/clang.py` is reused as-is.
- Expected native capability set: **ASan, UBSan, `-Wthread-safety`,
  clang-tidy available; TSan, LSan denied.** If a probe disproves one of the
  four on real Windows (UBSan is the least certain), the capability system
  reports that honestly and this spec's table gets corrected — the probes are
  the source of truth, not this document.

## Prerequisites (user installs, one-time)

- **LLVM for Windows** (`winget install LLVM.LLVM`) — provides `clang++` and
  `clang-tidy`. Must be on PATH (installer checkbox or manual); compiler
  discovery searches PATH only.
- **Visual Studio Build Tools** with the "Desktop development with C++"
  workload — headers, libraries, and linker clang's MSVC-ABI target needs.
  clang auto-detects the installation; no vcvars shell required.

MSYS2 g++ (already installed) stays discoverable but cannot link any sanitizer
runtime; `prefer()` picks clang, and if clang is absent the probes report gcc's
limits honestly.

## Changes, by seam

### 1. `platforms/windows.py` — new, mirrors `linux.py`

- `NAME = "windows"` (`platform.system().lower()` on Windows).
- `denied`: `TSAN` ("no ThreadSanitizer runtime exists for Windows in any
  compiler", suggestion: run under WSL), `LSAN` (same shape).
- `compile_extras = ()` — no `-pthread` on Windows.
- `extra_tool_dirs = (C:\Program Files\LLVM\bin,)` so clang-tidy is found even
  off PATH.
- `failure_signatures`: **starts empty.** House rule: every signature is a
  measured crash, not a guess. Signatures get added as implementation hits them.
- `env_facts`: none initially — no known volatile Windows setting affects the
  four supported analyses.
- Register in `platforms/__init__.py` `DETECTORS`.

### 2. `Platform.executable_suffix` — new field, default `""`

Windows binaries need `.exe`. `windows.py` sets `executable_suffix = ".exe"`;
`build/single_file.py:_binary_name` and the capability probes append it, and
`build/cmake.py`'s binary lookup accounts for CMake's automatic `.exe`. Exact
behavior of `clang++ -o name` (does it append `.exe` itself?) is verified
empirically during implementation; the field is set to whatever makes the built
binary's reported path the real file on disk.

### 3. `process.py` — Windows kill path

POSIX branch unchanged. On Windows there is no `os.killpg`/`SIGKILL`; a timed-out
process tree is killed with `taskkill /F /T /PID <pid>` (kills children too,
matching the POSIX process-group semantics). `start_new_session` is only passed
on POSIX. Same rule applies to the standalone copy in `scripts/fixtures.py`
(the docstring contract: kill-path fixes land in both).

### 4. Test suite — path-separator and platform fixes

The 25 current failures, three causes:

- 17 × `NotImplementedError: unsupported operating system: windows` — fixed by
  seam 1 alone.
- 6 × `/usr/bin/clang++` vs `\usr\bin\clang++` — tests compare command strings
  built from `Path`; fix by building expectations through `Path` (or comparing
  `Path` objects) so they hold on any OS. No production code involved.
- `test_process.py` hang-kill and `test_platforms.py` detect — fixed by seams
  3 and 1; `test_detect_returns_this_hosts_platform` learns the windows case.

### 5. Golden files + fixtures script

- Capture `windows-clang` goldens for the supported analyses (ASan cases, UBSan
  case, thread-safety, clang-tidy) via `scripts/fixtures.py capture` on Windows.
- `fixtures.py` gains platform awareness only where forced: skip TSan/LSan/
  deadlock cases on Windows, and the kill/`-pthread` differences from seam 3.

### 6. CI — `ci.yml`

- Add `windows-2025` to the unit-test matrix (GitHub's image ships LLVM).
- Add a fixtures job `windows-2025` + clang validating the `windows-clang`
  goldens.

## Non-goals

- MSVC (`cl.exe`) toolchain support.
- WSL bridging for TSan/LSan.
- Profilers (not built on any platform yet).
- Windows-gcc (MinGW) sanitizer support — proven impossible, runtime absent.

## Error handling

No new mechanisms. Denials, limitations, and failure signatures are the
existing `Platform` vocabulary; a Windows-specific crash gets a measured
`FailureSignature` when first seen, not a speculative one now.

## Success criteria (all verifiable)

1. `uv run pytest -m "not integration"` — green on this Windows machine.
2. `uv run pytest -m integration` — green here with LLVM installed.
3. The `capabilities` tool reports available: ASan, UBSan, thread-safety,
   clang-tidy; unavailable with reason + WSL suggestion: TSan, LSan.
4. End-to-end from Claude Code on this machine: the README's planted
   heap-overflow bug is caught by ASan through the MCP server.
5. CI green with the Windows jobs in the matrix.
