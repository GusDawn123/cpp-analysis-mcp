# ADR-0004: Symmetric execution engines, with a container floor

**Status:** Accepted · 2026-08-27

## Context

v1 already runs tools in more than one place: natively, and — on Windows —
through an auto-discovered WSL distro, with path translation and honest
capability reporting. That bridge is a special case of a general idea the
architecture needs twice over:

- **Zero-config**: a machine with none of the toolchain installed should still
  get full functionality.
- **Determinism** (architecture-v2, "the determinism claim"): "fixed tool
  versions" must be true for every user, or identical inputs produce different
  findings on different machines.

The open-questions safety discussion (§3) already resolved the related
question: containers here are a **capability unlock, not a security wall** —
measured evidence shows sanitizers break under default sandbox profiles (TSan
crashes on seccomp's `personality()` block; gcc's runtime needs
`vm.mmap_rnd_bits` lowered).

## Decision

Promote execution to a symmetric abstraction — three engines behind one
contract (mount workspace, translate paths, exec with guardrails, collect):

```
local      fastest; used when capability probes verify the host toolchain
wsl        Windows unlock for Linux-only tools (exists today)
container  works anywhere a container runtime exists; the zero-config floor
```

- **Resolution order:** `local` when probes pass → `container` as fallback →
  a plain, named-command failure when neither exists. Per-analyzer resolution
  is allowed (host has clang-tidy but not cppcheck); **deterministic mode**
  (everything in the container) is the review-gate default, mixed-engine the
  opt-in fast path.
- **One published toolbox image** (pinned by digest per server release)
  carries the entire analyzer roster: compilers, clang-tidy, cppcheck,
  sanitizer runtimes, perf, and future additions. Static breadth ships in the
  image, never as host install instructions.
- The image runs with the sanitizer-required relaxations
  (`--security-opt seccomp=unconfined`) baked in and documented — the golden
  files were captured exactly this way by hand.
- Source mounts **read-only**; builds go to a server-owned scratch mount.
  The §3 rule "the user's source tree is never written to" becomes a property
  of the mount table rather than of discipline.
- **Every finding records its engine.** A profile measured inside a Linux VM
  on macOS says so (`engine: linux-container`) — correctness findings
  transfer; latency numbers describe the VM. Facts, not advice.
- Uniformity has no exceptions: every analyzer invocation — including ones
  configured by repo-supplied files like `.clang-tidy` — goes through the
  engine layer's guardrails. CodeRabbit's production RCE came from exactly one
  tool being informally exempt from the boundary.

The honest floor: zero-*config* is achievable, zero-*prerequisite* is not.
A container runtime (Docker or Podman) is the one irreducible requirement for
hosts without toolchains — the same floor devcontainers and Testcontainers
accept. Embedding a runtime is enormous and fragile; shipping analysis to a
cloud service breaks privacy and offline use and is out of scope for this
tool.

## Consequences

- A bare machine with Docker gets full functionality on first call (one image
  pull); capability probes report the gap plainly when even that is missing.
- macOS *gains* LeakSanitizer and TSan's deadlock detection via the Linux
  container — previously unavailable natively.
- On Windows, the container engine can eventually subsume the WSL bridge
  (Docker Desktop runs on WSL2) — one backend replacing a special case.
- The toolbox image becomes a second release artifact with its own pipeline —
  accepted cost; digest pinning is what makes the determinism claim real.
- Container start overhead (~hundreds of ms) is noise against sanitizer build
  times; a warm-container optimization is deferred until benchmarks demand it.
