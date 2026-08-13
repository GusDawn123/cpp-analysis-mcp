"""Windows: no TSan or LSan runtime exists here at all, and LLVM installs off PATH.

Both denials are structural facts about the platform, not missing packages: no compiler ships
a ThreadSanitizer or LeakSanitizer runtime for Windows, so there is nothing to install and
the honest suggestion is a different operating system. WSL is that answer without leaving
the machine.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cpp_analysis_mcp.models import Analysis, SanitizerKind
from cpp_analysis_mcp.platforms.base import Denial, FailureSignature, Platform

NAME = "windows"

# the LLVM installer's default home; its PATH checkbox is off by default, so this is
# where clang-tidy lives on most machines that have it
LLVM_ROOT = Path(r"C:\Program Files\LLVM")
LLVM_DIR = LLVM_ROOT / "bin"

# where LLVM keeps the sanitizer runtimes, one directory per clang major version
RUNTIME_DIRS = "lib/clang/*/lib/windows"

# the libraries a UBSan link must be handed by full path. Measured: left to resolve the
# bare name, the linker takes MSVC's copy (its lib directories are searched first) and
# fails on symbols only MSVC's own build of the runtime provides
UBSAN_LIBS = (
    "clang_rt.ubsan_standalone-x86_64.lib",
    "clang_rt.ubsan_standalone_cxx-x86_64.lib",
)

# ASan's runtime is always a DLL on Windows, and it lives here rather than anywhere on
# PATH. Measured: a sanitized binary without it beside itself dies on STATUS_DLL_NOT_FOUND
# before main, printing nothing at all
ASAN_DLL = "clang_rt.asan_dynamic-x86_64.dll"

# Visual Studio ships a ninja that is not on PATH; any edition and year matches
VS_ROOT = Path(r"C:\Program Files\Microsoft Visual Studio")
NINJA_GLOB = "*/*/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"

# actionable because the server bridges into WSL by itself once a distro can compile:
# context.resolve() discovers it at the next start and this denial disappears
WSL_SUGGESTION = (
    "install a WSL distro with clang and restart this server -- it will run this check "
    "through WSL automatically: wsl --install -d Ubuntu; then wsl -d Ubuntu -- sudo "
    "apt-get install -y clang llvm cmake ninja-build"
)

NO_THREAD_SANITIZER = Denial(
    reason="no compiler ships a ThreadSanitizer runtime for Windows; races need Linux or macOS",
    suggestion=WSL_SUGGESTION,
)

NO_LEAK_SANITIZER = Denial(
    reason="LeakSanitizer has no Windows runtime; leak detection needs Linux",
    suggestion=WSL_SUGGESTION,
)

DENIED = {Analysis.TSAN: NO_THREAD_SANITIZER, Analysis.LSAN: NO_LEAK_SANITIZER}

INSTALL_HINTS = {Analysis.CLANG_TIDY: "winget install LLVM.LLVM"}

# MinGW gcc's linker errors, measured with MSYS2 g++ 15: its Windows port carries no
# sanitizer runtime at all, so unlike Linux there is no package that would provide one
MINGW_MISSING_RUNTIMES = ("cannot find -lasan", "cannot find -lubsan")

FAILURE_SIGNATURES = tuple(
    FailureSignature(
        marker=marker,
        reason=(
            "MinGW-w64 gcc ships no sanitizer runtimes on Windows, so the link has "
            "nothing to find and no package would provide it"
        ),
        suggestion="build with LLVM clang instead: winget install LLVM.LLVM",
    )
    for marker in MINGW_MISSING_RUNTIMES
)


def detect() -> Platform:
    """Read this host. The only place that does -- everything else takes a Platform."""
    runtime = runtime_dir(LLVM_ROOT)
    return Platform(
        name=NAME,
        executable_suffix=".exe",
        extra_tool_dirs=(LLVM_DIR,) if LLVM_DIR.is_dir() else (),
        denied=DENIED,
        install_hints=INSTALL_HINTS,
        failure_signatures=FAILURE_SIGNATURES,
        sanitize_link_extras=link_extras(runtime),
        runtime_dlls=runtime_dlls(runtime),
        cmake_extras=cmake_extras(find_ninja()),
    )


def find_ninja() -> Path | None:
    """PATH first, then the copy Visual Studio ships without linking it into PATH."""
    on_path = shutil.which("ninja")
    if on_path is not None:
        return Path(on_path)
    # sorted for determinism when several Visual Studio editions carry one
    return next(iter(sorted(VS_ROOT.glob(NINJA_GLOB))), None)


def cmake_extras(ninja: Path | None) -> tuple[str, ...]:
    """Force the Ninja generator, which obeys CMAKE_CXX_COMPILER.

    Measured: cmake's Windows default is the Visual Studio generator, which ignores the
    chosen compiler and hands the build to cl.exe -- the configure then dies on cl
    rejecting -Wthread-safety, three lines of MSBuild deep. Without a ninja to point at,
    the default stands and the configure failure speaks for itself.
    """
    if ninja is None:
        return ()
    return ("-G", "Ninja", f"-DCMAKE_MAKE_PROGRAM={ninja}")


def runtime_dir(root: Path) -> Path | None:
    """Return the newest clang version's runtime directory, or None without an LLVM install.

    Newest by the version number in the path, not by glob order: "9" sorts after "22" as
    text, and the machine that has both wants the one its clang binary will link against.
    """
    versioned = [
        (int(path.parts[-3]), path) for path in root.glob(RUNTIME_DIRS) if path.parts[-3].isdigit()
    ]
    return max(versioned)[1] if versioned else None


def link_extras(runtime: Path | None) -> dict[SanitizerKind, tuple[str, ...]]:
    """Name UBSan's runtime libraries by full path, so the link cannot take MSVC's copy."""
    if runtime is None:
        return {}
    return {SanitizerKind.UNDEFINED: tuple(str(runtime / library) for library in UBSAN_LIBS)}


def runtime_dlls(runtime: Path | None) -> dict[SanitizerKind, tuple[Path, ...]]:
    """Name the DLL an ASan binary must find beside itself before it can start."""
    if runtime is None:
        return {}
    dll = runtime / ASAN_DLL
    return {SanitizerKind.ADDRESS: (dll,)} if dll.is_file() else {}
