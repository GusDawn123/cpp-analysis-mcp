"""Linux: the extra compile flag, the host facts a result depends on, and the crashes we hit.
Every failure signature below was measured while capturing the goldens -- real tool crashes
whose output says nothing actionable, mapped to what actually went wrong.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.platforms.base import FailureSignature, Platform
from cpp_analysis_mcp.store.models import Analysis

NAME = "linux"

# glibc needs this to link std::thread; macOS does not
COMPILE_EXTRAS = ("-pthread",)

# ASLR width. TSan maps its shadow memory at fixed addresses and fails to start when the
# kernel randomizes over more bits than its runtime was built for.
MMAP_RND_BITS = Path("/proc/sys/vm/mmap_rnd_bits")
MMAP_RND_BITS_FACT = "vm.mmap_rnd_bits"
# gcc-13's libtsan started at 28 and crashed at Ubuntu 24.04's shipped 32
ASLR_LIMIT_BITS = 28

# linker error -> the package Debian-family distributions split that runtime into
RUNTIME_PACKAGES = {
    "cannot find -ltsan": "libtsan0",
    "cannot find -lasan": "libasan8",
    "cannot find -lubsan": "libubsan1",
    "cannot find -llsan": "liblsan0",
}

PERF_INSTALL = "sudo apt install linux-perf || sudo apt install linux-tools-generic"

INSTALL_HINTS = {
    Analysis.CLANG_TIDY: "sudo apt install clang-tidy",
    # split out of linux-tools on newer Debian-family releases, still inside it on older
    # ones; the second command is harmless where the first already worked
    Analysis.PROFILE: PERF_INSTALL,
}

# both spellings of "perf is not installed": the first is what this package's own spawn
# reports when the binary is not on PATH, the second is what `env` says when the same
# command is run one machine over through the WSL bridge
PERF_MISSING = ("No such file or directory: 'perf'", "env: 'perf'")

# perf refuses to open a counter for an unprivileged user above this setting. Distributions
# ship 4 (nothing allowed) or 2 (userspace allowed) and containers commonly inherit neither.
PERF_PARANOID = Path("/proc/sys/kernel/perf_event_paranoid")
PERF_PARANOID_FACT = "kernel.perf_event_paranoid"
PERF_DENIED_MARKER = "perf_event_paranoid"

# the volatile host settings a capability result depends on: change either and a cached
# answer about this machine stops being about this machine
HOST_SETTINGS = {MMAP_RND_BITS_FACT: MMAP_RND_BITS, PERF_PARANOID_FACT: PERF_PARANOID}


def detect() -> Platform:
    """Read this host. The only place that does -- everything else takes a Platform."""
    facts = env_facts()
    return Platform(
        name=NAME,
        compile_extras=COMPILE_EXTRAS,
        failure_signatures=failure_signatures(facts.get(MMAP_RND_BITS_FACT)),
        install_hints=INSTALL_HINTS,
        env_facts=facts,
    )


def env_facts() -> dict[str, str]:
    """Read the volatile host settings a capability result depends on, each on its own:
    one being unreadable says nothing about the other, and dropping both because of one
    would retire a cache entry that is still valid.
    """
    facts: dict[str, str] = {}
    for name, path in HOST_SETTINGS.items():
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            # absent on non-Linux kernels, root-only on GitHub's runners -- either way the
            # fact is unknown, and an unknown fact must not pretend to be a value
            continue
        if value:
            facts[name] = value
    return facts


def failure_signatures(mmap_rnd_bits: str | None) -> tuple[FailureSignature, ...]:
    """Build the signature table; the ASLR reason quotes this host's current setting."""
    return (
        _mapping_signature(mmap_rnd_bits),
        FailureSignature(
            marker="tsan_platform_linux",
            reason=(
                "a seccomp sandbox (Docker's default profile) blocks the personality() "
                "syscall TSan calls at startup to turn ASLR off"
            ),
            suggestion="run the container with --security-opt seccomp=unconfined",
        ),
        *(
            FailureSignature(
                marker=marker,
                reason=(
                    "the sanitizer runtime library is a separate package on this "
                    "distribution and is not installed, so the link step has nothing to find"
                ),
                suggestion=f"sudo apt install {package}",
            )
            for marker, package in RUNTIME_PACKAGES.items()
        ),
        *(
            FailureSignature(
                marker=marker,
                reason=(
                    "perf is not installed, so there is nothing here to sample with; it "
                    "ships separately from the compiler and from the kernel"
                ),
                suggestion=PERF_INSTALL,
            )
            for marker in PERF_MISSING
        ),
        FailureSignature(
            marker=PERF_DENIED_MARKER,
            reason=(
                "the kernel refused to open a performance counter for this user: "
                "perf_event_paranoid is set high enough to forbid it, which is the default "
                "in most containers and on some hardened distributions"
            ),
            suggestion=(
                "sudo sysctl -w kernel.perf_event_paranoid=1, or run the container with "
                "--cap-add SYS_ADMIN"
            ),
        ),
    )


def _mapping_signature(mmap_rnd_bits: str | None) -> FailureSignature:
    """Blame ASLR width only when this host's setting can actually explain the crash."""
    bits = int(mmap_rnd_bits) if mmap_rnd_bits and mmap_rnd_bits.isdigit() else None
    if bits is not None and bits > ASLR_LIMIT_BITS:
        return FailureSignature(
            marker="unexpected memory mapping",
            reason=(
                f"vm.mmap_rnd_bits = {bits} is too high for this TSan runtime, "
                "which cannot place its shadow memory under that much randomization"
            ),
            suggestion=f"sudo sysctl -w vm.mmap_rnd_bits={ASLR_LIMIT_BITS}",
        )
    return FailureSignature(
        marker="unexpected memory mapping",
        reason=(
            "this TSan runtime could not place its shadow memory; on other hosts this crash "
            "has meant vm.mmap_rnd_bits was too high, but this host's setting "
            f"({mmap_rnd_bits or 'unreadable'}) does not explain it"
        ),
    )
