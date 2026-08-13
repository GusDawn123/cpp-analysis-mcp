# WSL bridge — design

Date: 2026-08-12. Status: approved approach ("go ahead and build the WSL bridge"),
measurements done before writing this.

## Goal

On Windows, TSan and LSan stop being denials. When a WSL distro with clang exists,
the server compiles and runs those two analyses inside it, transparently: the caller
passes ordinary Windows paths and gets ordinary reports back. Without such a distro,
the denials stand, now with a suggestion that names the exact setup commands.

## What was measured first (on this machine, 2026-08-12)

Every mechanism below was verified through `process.run`, the server's own runner:

1. **`wsl.exe` management output is UTF-16.** `wsl -l -q` decodes to NUL-riddled
   text through the runner. Setting `WSL_UTF8=1` in the environment makes it clean
   UTF-8. Discovery sets it; bridged commands don't need it (their output comes from
   Linux processes, which write UTF-8).
2. **`wsl --exec env` is the safe spawn shape.** `--exec` passes argv through
   without a shell, so arguments with spaces survive intact (measured). Prefixing
   `env` gives PATH lookup for the real command plus `K=V` pinning — one argv
   element per variable, spaces inside values preserved (measured with the real
   `TSAN_OPTIONS` pin: the run exited with its pinned `exitcode=66`).
3. **`--cd` accepts Windows paths.** `--cd C:\Users\grosa` lands in
   `/mnt/c/Users/grosa` (measured).
4. **Binaries on `/mnt/c` compile and execute.** clang inside Ubuntu writes the
   binary to the Windows-side build directory and runs it from there.
5. **TSan and LSan catch their planted bugs** under Ubuntu's clang 21, despite
   `vm.mmap_rnd_bits = 32` (older runtimes crash there; this one copes). Exit codes
   66 (pinned) and 23 respectively, markers present.
6. **`apt install clang` alone leaves stacks unsymbolized** — every frame is
   `<null>`, no file:line. Installing `llvm` (provides `llvm-symbolizer` at the
   runtime's baked-in path) restores full `file:line:col` frames. The setup
   instructions therefore include it.
7. A management-level failure (bad distro name) exits nonzero with readable text
   under `WSL_UTF8=1`; discovery treats any nonzero as "no bridge" rather than
   guessing.

## Architecture: one primitive, one routing table

### 1. `src/cpp_analysis_mcp/wsl.py` — new top-level primitive

A peer of `process.py` and `capabilities.py`. It spawns only through the `Runner`
it is handed (no subprocess import), calls nothing above itself, and never calls
`platforms.detect()`. Contents:

- **`Bridge`** (frozen dataclass): `analyses` (frozenset — `{TSAN, LSAN}`),
  `toolchain` (clang as seen inside the distro, compiler spelled `clang++` since
  the wrapped runner resolves it on the Linux PATH), `platform` (the WSL platform
  data below), `runner` (the wrapping runner below).
- **`discover(*, runner) -> Bridge | None`**: `wsl.exe` on PATH → list distros
  (`wsl -l -q` under `WSL_UTF8=1`) → for each, ask `clang++ --version` through the
  wrapped spawn shape; the first distro that answers with clang wins. Utility
  distros (docker-desktop) fail the clang question and are skipped by measurement,
  not by a name denylist. Reads `/proc/sys/vm/mmap_rnd_bits` from the winner for
  `env_facts`. Every miss returns None quietly — no WSL, no distro, no clang are
  all ordinary machines, and the native denials already say what to do.
- **WSL platform data** (a `Platform`, name `"wsl"`): `compile_extras=("-pthread",)`
  (it is Linux); `cmake_extras=("-G", "Ninja")` (base Ubuntu has no make; setup
  installs ninja); `denied` lists ASAN, UBSAN, thread-safety and clang-tidy as
  "handled natively on Windows" so `probe_all` answers them without spawning and
  the bridge only ever carries what Windows lacks; `limitations` on TSAN and LSAN
  note the distro name and that report paths appear in WSL form (`/mnt/c/...`);
  `failure_signatures` reuses `platforms/linux.py`'s table — the bridge is Linux,
  and those signatures (ASLR width, missing runtime packages) were measured there;
  `env_facts` carries the distro name and `vm.mmap_rnd_bits`, which puts both into
  the capability-cache fingerprint.
- **`bridged(runner, wsl_exe, distro) -> Runner`**: wraps any command as
  `[wsl_exe, -d, distro, (--cd, cwd)?, --exec, env, K=V..., *translated argv]`.
  The `K=V` pins are exactly the `process.SANITIZER_ENV_VARS` present in the call's
  env — the canonical list process.py already owns. The outer `wsl.exe` process
  keeps the caller's env and no cwd (the `--cd` flag carries it inside).
- **`to_wsl(arg)`**: an argument matching `^[A-Za-z]:[\\/]` becomes
  `/mnt/<drive-lowercase>/<rest, backslashes flipped>`; everything else passes
  through untouched. Whole-argument translation only — measured against every
  command shape `single_file.py` and `cmake.py` compose for the bridge platform,
  none of which embeds a Windows path inside a larger argument.

### 2. `context.py` — per-analysis engines, decided at startup

- **`Engine`** (frozen dataclass): `toolchain`, `platform`, `runner` — everything a
  pipeline call varies by.
- **`Context.engines: Mapping[Analysis, Engine]`** — new field. Constructed without
  it (every existing test), `__post_init__` fills a uniform mapping from the
  context's own toolchain/platform/runner, so "no bridge" is the default shape
  rather than a special case.
- **`resolve()`**: unchanged until after native capabilities, then: on Windows,
  `wsl.discover()`. With a bridge, probe capabilities again through the bridge's
  toolchain/platform/runner (cached under its own fingerprint), and for each bridge
  analysis whose probe came back **available**, take the bridged status and point
  that analysis's engine at the bridge. A bridged probe that failed leaves the
  native denial standing — its suggestion tells the user what to fix, and an
  engine is never routed anywhere its probe did not pass.

### 3. `server.py` — a lookup, not a branch

Each analysis handler reads `engine = app.engines[Analysis(analysis)]` and passes
`engine.toolchain / engine.platform / engine.runner` where it passed `app.*`
before. A subscript is declaration, not control flow, so rule 2 holds — and the
pipelines keep their signatures and their ignorance of WSL entirely.

### 4. Ratchets

`tests/unit/test_architecture.py` adds `wsl.py` to `TOP_LEVEL_PRIMITIVES` so the
layer rules cover it. The windows denial text in `platforms/windows.py` upgrades
its suggestion to the concrete setup: install a distro, install the packages,
restart the server, and the bridge picks it up on its own.

## Setup (user, one-time)

```powershell
wsl --install -d Ubuntu
wsl -d Ubuntu -- sudo apt-get install -y clang llvm cmake ninja-build
```

`llvm` is not optional in spirit: without `llvm-symbolizer` the detectors still
catch bugs but report `<null>` frames instead of file:line (measured).

## Non-goals

- Translating `/mnt/c/...` paths in findings back to `C:\...` — a limitation note
  on the capability says how to read them; revisit if it proves annoying.
- Running the bridge in CI — GitHub's Windows runners cannot nest WSL2, so CI
  exercises the bridge-less path (denials), which unit tests with fake runners
  cover the bridge against.
- New goldens or fixtures — bridged output is Linux clang output, which the
  existing linux-clang goldens and parsers already pin down.
- Installing the distro from inside the server — setup is documented, never
  performed behind the user's back.

## Error handling

No new mechanisms. A bridge that is not there is `None`; a bridged probe that
fails leaves the native denial in place; a bridged build failure carries the
Linux failure signatures. The one new failure text is the upgraded Windows
denial suggestion naming the setup commands.

## Success criteria (all verifiable)

1. `uv run pytest -m "not integration"` green on this machine.
2. `uv run pytest -m integration` green on this machine.
3. The `capabilities` tool reports all six analyses available here — TSan and
   LSan carrying the WSL limitation notes.
4. End-to-end over MCP stdio: `sanitize_snippet` catches a planted data race
   under tsan and a planted leak under lsan, with file:line in the findings.
5. `uv run ruff check` and `uv run mypy` clean.
6. CI stays green: without WSL the resolved context is byte-for-byte what it was
   before this change.
