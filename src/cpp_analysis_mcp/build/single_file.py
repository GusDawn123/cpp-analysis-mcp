"""Compile one source file into a BuiltBinary, or say why it did not build. The binary
comes back bound to its runtime environment: a sanitized build run without its options
reports nothing, reading like clean code. Toolchain and Platform arrive as arguments.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from cpp_analysis_mcp import process
from cpp_analysis_mcp.parsers import diagnostics
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import BuildFailure, BuiltBinary, SanitizerKind
from cpp_analysis_mcp.toolchains.base import BASE_FLAGS, PINNED_RUNTIME_ENV, Toolchain

COMPILE_TIMEOUT_S = 120

# which step died, for BuildFailure.stage; cmake adds "configure" and "build" of its own
COMPILE_STAGE = "compile"


def compile_file(
    source: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    sanitizer: SanitizerKind | None,
    build_dir: Path,
    base_flags: tuple[str, ...] = BASE_FLAGS,
    timeout_s: int = COMPILE_TIMEOUT_S,
    runner: Runner = process.run,
) -> BuiltBinary | BuildFailure:
    """Build one source file, under a sanitizer or under none. A successful build can still
    carry findings: -Wthread-safety reports while compiling, so the compiler's own output
    is parsed into the returned warnings. Sanitized builds ignore `base_flags`.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / _binary_name(source, sanitizer, platform.executable_suffix)

    result = runner(
        _command(
            source,
            binary,
            toolchain=toolchain,
            platform=platform,
            sanitizer=sanitizer,
            base_flags=base_flags,
        ),
        timeout_s=timeout_s,
        # a compile must not inherit the developer's sanitizer options
        env=process.hygienic_env({}),
    )
    if result.timed_out:
        return BuildFailure(stage=COMPILE_STAGE, output=result.output, timed_out=True)
    if result.exit_code != 0:
        return _failure(platform, result.output)

    place_runtime_dlls(platform, sanitizer, binary.parent)
    return BuiltBinary(
        path=binary,
        build_dir=build_dir,
        sanitizer=sanitizer,
        runtime_env=PINNED_RUNTIME_ENV[sanitizer] if sanitizer is not None else {},
        compile_commands=None,
        warnings=diagnostics.parse(result.output),
    )


def _binary_name(source: Path, sanitizer: SanitizerKind | None, suffix: str) -> str:
    """Name the output by source and variant: a TSan and an ASan build of one source must
    not overwrite each other, or the survivor sits at the other's path bound to the wrong
    runtime environment. The suffix goes on last -- Windows only executes .exe files.
    """
    stem = f"{source.stem}.{sanitizer}" if sanitizer is not None else source.stem
    return f"{stem}{suffix}"


def place_runtime_dlls(platform: Platform, sanitizer: SanitizerKind | None, beside: Path) -> None:
    """Copy the DLLs a sanitized binary needs into the directory it will run from: Windows
    resolves a DLL from the executable's own directory first, and ASan's runtime lives
    nowhere else the loader looks. Elsewhere the table is empty and this is a no-op.
    """
    if sanitizer is None:
        return
    for dll in platform.runtime_dlls.get(sanitizer, ()):
        shutil.copy2(dll, beside)


def with_runtime_on_path(
    platform: Platform, sanitizer: SanitizerKind | None, env: dict[str, str]
) -> dict[str, str]:
    """Return `env` with this sanitizer's runtime-DLL directories prepended to PATH: a build
    can run what it just linked (gtest_discover_tests does, POST_BUILD, before any DLL is
    copied) and dies on STATUS_DLL_NOT_FOUND printing nothing. A no-op except on Windows.
    """
    if sanitizer is None:
        return env
    directories = sorted({str(dll.parent) for dll in platform.runtime_dlls.get(sanitizer, ())})
    if not directories:
        return env
    # prepended, not appended: another copy of the same runtime earlier on PATH is exactly
    # the mismatch the full-path link extras exist to avoid
    existing = env.get("PATH", "")
    return {**env, "PATH": os.pathsep.join([*directories, existing] if existing else directories)}


def _command(
    source: Path,
    binary: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    sanitizer: SanitizerKind | None,
    base_flags: tuple[str, ...],
) -> list[str]:
    """Compose the invocation: sanitizer flags or the base ones, warnings, then this OS's."""
    flags = toolchain.sanitize_flags(sanitizer) if sanitizer is not None else base_flags
    extras = platform.sanitize_link_extras.get(sanitizer, ()) if sanitizer is not None else ()
    return [
        str(toolchain.compiler),
        *flags,
        *toolchain.warning_flags,
        *platform.compile_extras,
        str(source),
        "-o",
        str(binary),
        # link inputs last, where a linker expects libraries to follow the objects
        *extras,
    ]


def _failure(platform: Platform, output: str) -> BuildFailure:
    """Carry the platform's diagnosis as strings: models.py may not import platforms."""
    signature = platform.diagnose(output)
    if signature is None:
        # no signature matched, so the output speaks for itself; inventing a reason here
        # would explain the failure wrongly and confidently
        return BuildFailure(stage=COMPILE_STAGE, output=output)
    return BuildFailure(
        stage=COMPILE_STAGE,
        output=output,
        reason=signature.reason,
        suggestion=signature.suggestion,
    )
