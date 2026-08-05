"""Linux: the extra compile flag, the host facts a result depends on, and the crashes we hit.

Every failure signature below was measured while capturing the goldens. Each one is a real
tool crash whose output says nothing a reader can act on, mapped to what actually went wrong.
"""

from __future__ import annotations

from pathlib import Path

from cpp_analysis_mcp.models import Analysis
from cpp_analysis_mcp.platforms.base import FailureSignature, Platform

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

INSTALL_HINTS = {Analysis.CLANG_TIDY: "sudo apt install clang-tidy"}


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
    """Read the volatile host settings a capability result depends on."""
    try:
        value = MMAP_RND_BITS.read_text(encoding="utf-8").strip()
    except OSError:
        # absent on non-Linux kernels, root-only on GitHub's runners -- either way the
        # fact is unknown, and an unknown fact must not pretend to be a value
        return {}
    return {MMAP_RND_BITS_FACT: value}


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
