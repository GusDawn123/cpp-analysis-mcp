"""Compile one source file into a BuiltBinary, or say why it did not build.

Choosing -fsanitize=thread and knowing the run needs TSAN_OPTIONS are one decision, so the
binary comes back already bound to its environment. Held apart, they drift: a build with
the sanitizer and a run without its options reports nothing at all, which reads exactly
like clean code. One object means a caller cannot pick up the binary and leave the
environment behind.

A build that failed is a BuildFailure, not an exception -- user code that does not compile
is an ordinary thing to observe. The Platform and Toolchain always arrive as arguments
(rule 3); nothing here looks up the host.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cpp_analysis_mcp import process
from cpp_analysis_mcp.models import BuildFailure, BuiltBinary, SanitizerKind
from cpp_analysis_mcp.parsers import diagnostics
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
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
    timeout_s: int = COMPILE_TIMEOUT_S,
    runner: Runner = process.run,
) -> BuiltBinary | BuildFailure:
    """Build one source file, under a sanitizer or under none.

    A build that succeeded can still carry findings: -Wthread-safety reports while
    compiling, so the compiler's own output is parsed into the returned warnings.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / _binary_name(source, sanitizer, platform.executable_suffix)

    result = runner(
        _command(source, binary, toolchain=toolchain, platform=platform, sanitizer=sanitizer),
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
    """Name the output by source and variant, so one file's sanitized builds coexist.

    A TSan and an ASan build of the same source into one directory must not overwrite
    each other: the survivor would sit at the other's reported path, bound to the wrong
    runtime environment. The platform's executable suffix goes on last: Windows will
    only execute a file that ends in .exe.
    """
    stem = f"{source.stem}.{sanitizer}" if sanitizer is not None else source.stem
    return f"{stem}{suffix}"


def place_runtime_dlls(platform: Platform, sanitizer: SanitizerKind | None, beside: Path) -> None:
    """Copy the DLLs a sanitized binary needs into the directory it will run from.

    Windows resolves a DLL from the executable's own directory first, and ASan's runtime
    lives nowhere else the loader looks; on the other platforms the table is empty and
    this is a no-op.
    """
    if sanitizer is None:
        return
    for dll in platform.runtime_dlls.get(sanitizer, ()):
        shutil.copy2(dll, beside)


def _command(
    source: Path,
    binary: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    sanitizer: SanitizerKind | None,
) -> list[str]:
    """Compose the invocation: sanitizer flags or the base ones, warnings, then this OS's."""
    flags = toolchain.sanitize_flags(sanitizer) if sanitizer is not None else BASE_FLAGS
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
