"""Check the per-OS tables from a machine that is only one of those operating systems.

Every behavioural test here builds its own Platform out of the tables the platform modules
declare, instead of asking the host what it is. That is what rule 3 buys: the Linux failure
signatures below are exercised on macOS, where none of those crashes can happen.
"""

from __future__ import annotations

import platform as host_platform
import shutil
from pathlib import Path

import pytest

from cpp_analysis_mcp import platforms
from cpp_analysis_mcp.models import Analysis, SanitizerKind
from cpp_analysis_mcp.platforms import darwin, linux, windows
from cpp_analysis_mcp.platforms.base import Denial, FailureSignature, Platform

# vm.mmap_rnd_bits as Ubuntu 24.04 ships it -- the value the mapping crash was measured at
MEASURED_RND_BITS = "32"

# FATAL line TSan printed on that host before it could start
UNEXPECTED_MAPPING = (
    "FATAL: ThreadSanitizer: unexpected memory mapping 0x7f4c9d000000-0x7f4c9d022000"
)

# what the same runtime prints inside a container running Docker's default seccomp profile
SECCOMP_CHECK_FAILED = (
    "FATAL: ThreadSanitizer CHECK failed: "
    "/llvm/lib/tsan/rtl/tsan_platform_linux.cpp:429 "
    '"((personality(old_personality | ADDR_NO_RANDOMIZE))) != ((-1))" (0x0, 0x0)'
)

MISSING_TSAN_RUNTIME = "/usr/bin/ld: cannot find -ltsan: No such file or directory"

# an ordinary report: a platform failure it is not
BENIGN_OUTPUT = (
    "WARNING: ThreadSanitizer: data race (pid=1234)\n"
    "  Write of size 4 at 0xab5e401138e8 by thread T1:\n"
)


def a_linux() -> Platform:
    """Build Linux's tables without reading any host, at a known mmap_rnd_bits."""
    return Platform(
        name="linux",
        compile_extras=linux.COMPILE_EXTRAS,
        failure_signatures=linux.failure_signatures(MEASURED_RND_BITS),
        env_facts={linux.MMAP_RND_BITS_FACT: MEASURED_RND_BITS},
    )


def a_darwin() -> Platform:
    """Build macOS's arm64 tables without asking what machine this is."""
    return Platform(
        name="darwin",
        denied={Analysis.LSAN: darwin.NO_LEAK_SANITIZER},
        limitations={Analysis.TSAN: darwin.TSAN_LIMITATIONS},
        install_hints=darwin.INSTALL_HINTS,
    )


# ----------------------------------------------------------------------------------- darwin


def test_leak_sanitizer_is_denied_with_a_way_out() -> None:
    denial = a_darwin().denied[Analysis.LSAN]

    assert "LeakSanitizer" in denial.reason
    assert denial.suggestion is not None
    assert "Linux" in denial.suggestion


def test_thread_sanitizer_is_available_but_caveated() -> None:
    """Deadlock detection is inert here, so a silent TSan run must not read as "no deadlocks"."""
    darwin_like = a_darwin()
    limitations = darwin_like.limitations[Analysis.TSAN]

    assert Analysis.TSAN not in darwin_like.denied
    assert limitations
    assert any("deadlock" in note for note in limitations)


def test_darwin_says_how_to_install_clang_tidy() -> None:
    assert a_darwin().install_hints[Analysis.CLANG_TIDY] == "brew install llvm"


def test_darwin_looks_for_llvm_where_brew_leaves_it() -> None:
    """brew does not put clang-tidy on PATH, so those directories are searched by hand."""
    assert all(path.name == "bin" for path in darwin.BREW_LLVM_DIRS)
    assert all("llvm" in path.parts for path in darwin.BREW_LLVM_DIRS)
    assert all(path.is_dir() for path in darwin.detect().extra_tool_dirs)


# ------------------------------------------------------------------------------------ linux


def test_linux_needs_pthread() -> None:
    assert "-pthread" in a_linux().compile_extras


def test_the_aslr_mapping_crash_names_the_setting_and_the_fix() -> None:
    signature = a_linux().diagnose(UNEXPECTED_MAPPING)

    assert signature is not None
    assert "vm.mmap_rnd_bits" in signature.reason
    assert MEASURED_RND_BITS in signature.reason
    assert signature.suggestion == "sudo sysctl -w vm.mmap_rnd_bits=28"


@pytest.mark.parametrize("setting", (None, "28", "not-a-number"))
def test_an_unexplained_mapping_crash_invents_no_fix(setting: str | None) -> None:
    """At 28, unreadable, or garbage the setting cannot explain the crash; saying it does
    and prescribing a sysctl the host has already applied would be a fabricated diagnosis."""
    linux_like = Platform(name="linux", failure_signatures=linux.failure_signatures(setting))

    signature = linux_like.diagnose(UNEXPECTED_MAPPING)

    assert signature is not None  # the crash is still recognized and named
    assert signature.suggestion is None
    assert "is too high for" not in signature.reason


def test_the_seccomp_crash_is_read_as_a_sandbox_problem() -> None:
    signature = a_linux().diagnose(SECCOMP_CHECK_FAILED)

    assert signature is not None
    assert "seccomp" in signature.reason
    assert signature.suggestion is not None
    assert "seccomp=unconfined" in signature.suggestion


def test_a_missing_runtime_library_names_its_package() -> None:
    signature = a_linux().diagnose(MISSING_TSAN_RUNTIME)

    assert signature is not None
    assert signature.suggestion is not None
    assert "libtsan" in signature.suggestion


@pytest.mark.parametrize("library", ("tsan", "asan", "ubsan", "lsan"))
def test_every_sanitizer_runtime_has_a_package_name(library: str) -> None:
    signature = a_linux().diagnose(f"/usr/bin/ld: cannot find -l{library}: No such file")

    assert signature is not None
    assert signature.suggestion is not None
    assert f"lib{library}" in signature.suggestion


def test_ordinary_tool_output_is_not_a_platform_failure() -> None:
    assert a_linux().diagnose(BENIGN_OUTPUT) is None
    assert a_linux().diagnose("") is None


def test_the_first_matching_signature_wins() -> None:
    """A crash can carry both markers; the answer must be the first table entry, every time."""
    linux_like = a_linux()
    both = f"{UNEXPECTED_MAPPING}\n{SECCOMP_CHECK_FAILED}\n"
    reversed_order = f"{SECCOMP_CHECK_FAILED}\n{UNEXPECTED_MAPPING}\n"
    first = linux_like.failure_signatures[0]

    assert linux_like.diagnose(both) is first
    assert linux_like.diagnose(reversed_order) is first


def test_linux_reads_the_aslr_width_only_where_it_is_readable() -> None:
    """/proc is absent on macOS and root-only on GitHub's runners; an unreadable fact
    is omitted rather than guessed at or crashed on."""
    facts = linux.detect().env_facts

    assert set(facts) <= {linux.MMAP_RND_BITS_FACT}
    if linux.MMAP_RND_BITS_FACT in facts:
        assert facts[linux.MMAP_RND_BITS_FACT].isdigit()


def test_linux_detect_carries_its_tables() -> None:
    detected = linux.detect()

    assert detected.name == "linux"
    assert detected.compile_extras == ("-pthread",)
    assert detected.install_hints[Analysis.CLANG_TIDY] == "sudo apt install clang-tidy"
    assert detected.failure_signatures
    assert detected.denied == {}
    assert detected.limitations == {}


# ---------------------------------------------------------------------------------- windows


def a_windows() -> Platform:
    """Build Windows's tables without asking what machine this is."""
    return Platform(
        name="windows",
        executable_suffix=".exe",
        denied=windows.DENIED,
        install_hints=windows.INSTALL_HINTS,
    )


def test_windows_denies_both_missing_runtimes_with_a_way_out() -> None:
    """No compiler ships TSan or LSan for Windows; both denials must say so and point at WSL."""
    denied = a_windows().denied

    assert "ThreadSanitizer" in denied[Analysis.TSAN].reason
    assert "LeakSanitizer" in denied[Analysis.LSAN].reason
    for denial in denied.values():
        assert denial.suggestion is not None
        assert "WSL" in denial.suggestion


def test_windows_binaries_carry_the_exe_suffix() -> None:
    """CreateProcess only executes .exe; a suffixless binary would build and then not run."""
    assert a_windows().executable_suffix == ".exe"
    assert windows.detect().executable_suffix == ".exe"


def test_windows_says_how_to_install_clang_tidy() -> None:
    assert a_windows().install_hints[Analysis.CLANG_TIDY] == "winget install LLVM.LLVM"


def test_windows_looks_for_llvm_where_its_installer_leaves_it() -> None:
    """The LLVM installer's PATH checkbox is off by default, so its bin is searched by hand."""
    assert windows.LLVM_DIR.name == "bin"
    assert all(path.is_dir() for path in windows.detect().extra_tool_dirs)


def test_the_runtime_tables_follow_the_clang_discovery_will_find(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two LLVMs on one machine: toolchain discovery takes the clang++ PATH offers, so the
    runtime the probes link must come from that same install, not the installer default --
    bound apart, ASan and UBSan link a different version's runtime than their compiler."""
    (tmp_path / "lib" / "clang" / "22" / "lib" / "windows").mkdir(parents=True)
    on_path = tmp_path / "bin" / "clang++.exe"
    monkeypatch.setattr(
        shutil, "which", lambda name: str(on_path) if name == windows.COMPILER else None
    )

    assert windows.llvm_root() == tmp_path


def test_a_path_clang_with_no_runtime_beside_it_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clang in a layout the table does not know (MSVC's bundled one) must not erase the
    default: the installer's runtime is still the best guess there is."""
    monkeypatch.setattr(shutil, "which", lambda name: str(tmp_path / "bin" / "clang++.exe"))

    assert windows.llvm_root() == windows.LLVM_ROOT


def test_no_clang_on_path_leaves_the_installer_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert windows.llvm_root() == windows.LLVM_ROOT


def test_the_newest_llvm_runtime_directory_wins(tmp_path: Path) -> None:
    """ "9" sorts after "22" as text; the machine with both wants what its clang links."""
    for version in ("9", "22"):
        (tmp_path / "lib" / "clang" / version / "lib" / "windows").mkdir(parents=True)

    chosen = windows.runtime_dir(tmp_path)

    assert chosen is not None
    assert chosen.parts[-3] == "22"


def test_no_llvm_install_means_empty_runtime_tables(tmp_path: Path) -> None:
    """Without LLVM the tables stay empty and the capability probes explain what is missing;
    inventing paths here would turn that honest failure into a mystery about a file."""
    assert windows.runtime_dir(tmp_path) is None
    assert windows.link_extras(None) == {}
    assert windows.runtime_dlls(None) == {}


def test_ubsan_is_linked_against_llvms_own_runtime_by_full_path(tmp_path: Path) -> None:
    """Left as a bare name the linker takes MSVC's copy -- searched first, built for a
    different compiler -- and fails on symbols only MSVC's own objects define."""
    named = windows.link_extras(tmp_path)[SanitizerKind.UNDEFINED]

    assert [Path(library).parent for library in named] == [tmp_path, tmp_path]
    assert [Path(library).name for library in named] == list(windows.UBSAN_LIBS)


def test_cmake_on_windows_is_forced_onto_the_ninja_generator(tmp_path: Path) -> None:
    """cmake's Windows default generator ignores CMAKE_CXX_COMPILER and builds with cl.exe;
    naming Ninja is what makes the chosen compiler the one that actually compiles."""
    ninja = tmp_path / "ninja.exe"

    extras = windows.cmake_extras(ninja)

    assert extras[:2] == ("-G", "Ninja")
    assert extras[2] == f"-DCMAKE_MAKE_PROGRAM={ninja}"


def test_without_a_ninja_the_cmake_default_stands() -> None:
    """No generator argument is better than one naming a program that is not there."""
    assert windows.cmake_extras(None) == ()


def test_asan_names_its_dll_only_when_the_install_carries_it(tmp_path: Path) -> None:
    """A promised DLL that is not there would fail every build's copy step; an absent one
    leaves the run to fail and the probe to report what it saw."""
    assert windows.runtime_dlls(tmp_path) == {}

    (tmp_path / windows.ASAN_DLL).write_bytes(b"present")

    assert windows.runtime_dlls(tmp_path) == {SanitizerKind.ADDRESS: (tmp_path / windows.ASAN_DLL,)}


# --------------------------------------------------------------------------- the base shape


def test_a_bare_platform_denies_nothing() -> None:
    bare = Platform(name="linux")

    assert bare.compile_extras == ()
    assert bare.executable_suffix == ""
    assert bare.extra_tool_dirs == ()
    assert bare.sanitize_link_extras == {}
    assert bare.runtime_dlls == {}
    assert bare.cmake_extras == ()
    assert bare.denied == {}
    assert bare.limitations == {}
    assert bare.failure_signatures == ()
    assert bare.install_hints == {}
    assert bare.env_facts == {}
    assert bare.diagnose("anything at all") is None


def test_platform_pieces_are_frozen_slotted_data() -> None:
    instances = (
        Platform(name="linux"),
        Denial(reason="no"),
        FailureSignature(marker="boom", reason="it broke"),
    )

    for instance in instances:
        assert not hasattr(instance, "__dict__"), f"{type(instance).__name__} lost its slots"


def test_the_tables_cannot_be_edited_after_construction() -> None:
    """Platforms are built from shared module-level tables; a writable mapping on one
    instance would silently corrupt every Platform constructed after it."""
    darwin_like = a_darwin()

    with pytest.raises(TypeError):
        darwin_like.denied[Analysis.TSAN] = Denial(reason="oops")  # type: ignore[index]
    with pytest.raises(TypeError):
        darwin_like.install_hints[Analysis.CLANG_TIDY] = "changed"  # type: ignore[index]
    # and the source table survived the attempt
    assert Analysis.CLANG_TIDY in darwin.INSTALL_HINTS


def test_a_denial_and_a_signature_may_carry_no_suggestion() -> None:
    """Not every dead end has a fix worth naming, and inventing one would be advice."""
    assert Denial(reason="no").suggestion is None
    assert FailureSignature(marker="boom", reason="it broke").suggestion is None


# ------------------------------------------------------------------------------- dispatcher


def test_detect_returns_this_hosts_platform() -> None:
    detected = platforms.detect()

    assert detected.name == host_platform.system().lower()
    assert isinstance(detected.compile_extras, tuple)
    assert isinstance(detected.extra_tool_dirs, tuple)
    assert all(isinstance(path, Path) for path in detected.extra_tool_dirs)
    assert isinstance(detected.failure_signatures, tuple)
    assert all(isinstance(value, str) for value in detected.env_facts.values())


def test_an_unsupported_os_is_named_rather_than_guessed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host_platform, "system", lambda: "FreeBSD")

    with pytest.raises(NotImplementedError, match="freebsd"):
        platforms.detect()
