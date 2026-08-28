"""Build a real CMake project into a BuiltBinary, or say which cmake step died.

Where single_file.py runs one command, a project has two that fail for different reasons, so
a failure names the step: a configure that could not find the compiler and a build that hit
a syntax error are not the same problem to whoever reads the report.

Finding the binary afterwards is the part worth explaining. Guessing paths out of a build
tree is a losing game -- the layout moves with the generator, with subdirectories, and with
target properties like OUTPUT_NAME. So this asks instead. A File API query file written
before configuring makes cmake record every target it defined and where each artifact
lands, and the answer arrives as part of the configure that follows: no second invocation,
no guessing, and the executables can be told apart from the libraries.

A build that failed is a BuildFailure, not an exception -- user code that does not compile
is an ordinary thing to observe. The Platform and Toolchain always arrive as arguments
(rule 3); nothing here looks up the host.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cpp_analysis_mcp import process
from cpp_analysis_mcp.build.single_file import place_runtime_dlls, with_runtime_on_path
from cpp_analysis_mcp.parsers import diagnostics
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import BuildFailure, BuiltBinary, SanitizerKind
from cpp_analysis_mcp.toolchains.base import BASE_FLAGS, PINNED_RUNTIME_ENV, Toolchain

# a project is minutes of work where one file is seconds, and cold third-party dependencies
# configure slowly
CMAKE_TIMEOUT_S = 600

# which step died, for BuildFailure.stage; single_file.py owns "compile"
CONFIGURE_STAGE = "configure"
BUILD_STAGE = "build"

# cmake's File API lives at a fixed place inside the build tree
API_DIR = Path(".cmake", "api", "v1")
# the query is named by what it asks for; its contents are ignored, so the file is empty
CODEMODEL = "codemodel-v2"

# the one target type that produces something runnable
EXECUTABLE = "EXECUTABLE"

COMPILE_COMMANDS = "compile_commands.json"


@dataclass(frozen=True, slots=True)
class _Executable:
    """One runnable target the File API reported."""

    name: str
    # as cmake wrote it: relative to the build directory
    artifact: str


@dataclass(frozen=True, slots=True)
class _Configuration:
    """The codemodel configuration this build will use: its name and its runnable targets.

    Single-config generators produce one entry named "" (we pin CMAKE_BUILD_TYPE empty).
    Multi-config generators (Xcode, Ninja Multi-Config) ignore CMAKE_BUILD_TYPE and list
    several entries, each with its own artifact paths -- so the name is part of the answer:
    the build must be told to produce the configuration whose paths we are reporting.
    """

    name: str
    executables: tuple[_Executable, ...]


def build_project(
    project_dir: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    sanitizer: SanitizerKind | None,
    build_dir: Path,
    target: str | None = None,
    base_flags: tuple[str, ...] = BASE_FLAGS,
    timeout_s: int = CMAKE_TIMEOUT_S,
    runner: Runner = process.run,
) -> BuiltBinary | BuildFailure:
    """Configure and build a CMake project, under a sanitizer or under none.

    `timeout_s` bounds each cmake invocation on its own rather than the pair, so a project
    that configures slowly and then builds slowly gets the full allowance twice.

    With no `target`, a project holding exactly one executable builds it; anything else comes
    back as a failure naming the targets there are, since picking one for the caller is a
    guess and the wrong guess wastes minutes.

    `base_flags` is what an unsanitized build compiles with, and a sanitized one ignores it:
    every sanitizer flag set is built on BASE_FLAGS already. It exists so the profiler can
    ask for its own optimization level without a second build module.
    """
    _write_query(build_dir)

    configured = runner(
        _configure_command(
            project_dir,
            build_dir,
            toolchain=toolchain,
            platform=platform,
            sanitizer=sanitizer,
            base_flags=base_flags,
        ),
        timeout_s=timeout_s,
        # a build must not inherit the developer's sanitizer options
        env=process.hygienic_env({}),
    )
    if configured.timed_out:
        return BuildFailure(stage=CONFIGURE_STAGE, output=configured.output, timed_out=True)
    if configured.exit_code != 0:
        return _failure(CONFIGURE_STAGE, platform, configured.output)

    configuration = _configuration(build_dir / API_DIR / "reply")
    if configuration is None:
        return BuildFailure(
            stage=CONFIGURE_STAGE,
            output=configured.output,
            reason=(
                "cmake exited 0 but its File API reply was missing or unreadable, so the "
                "targets this project defines could not be read"
            ),
            suggestion=(
                "delete the build directory and build again -- a reply left half-written by "
                "an interrupted configure is not repaired in place"
            ),
        )

    chosen = _choose(configuration.executables, target, configured.output)
    if isinstance(chosen, BuildFailure):
        return chosen

    built = runner(
        _build_command(build_dir, chosen.name, configuration.name),
        timeout_s=timeout_s,
        # the sanitizer's runtime goes on PATH for this step alone: a build that runs what
        # it just linked -- gtest_discover_tests does -- needs the loader to find it before
        # anything has been copied beside the binary. The run afterwards uses the copy.
        env=with_runtime_on_path(platform, sanitizer, process.hygienic_env({})),
    )
    if built.timed_out:
        return BuildFailure(stage=BUILD_STAGE, output=built.output, timed_out=True)
    if built.exit_code != 0:
        return _failure(BUILD_STAGE, platform, built.output)

    binary = build_dir / chosen.artifact
    place_runtime_dlls(platform, sanitizer, binary.parent)
    compile_commands = build_dir / COMPILE_COMMANDS
    return BuiltBinary(
        path=binary,
        build_dir=build_dir,
        sanitizer=sanitizer,
        runtime_env=PINNED_RUNTIME_ENV[sanitizer] if sanitizer is not None else {},
        # older cmake and some generators ignore the export flag; claiming a database that
        # is not there sends clang-tidy after a file it cannot open
        compile_commands=compile_commands if compile_commands.is_file() else None,
        warnings=diagnostics.parse(built.output),
    )


def _write_query(build_dir: Path) -> None:
    """Ask cmake to write down what it built, before it configures and can still be asked."""
    query = build_dir / API_DIR / "query" / CODEMODEL
    query.parent.mkdir(parents=True, exist_ok=True)
    query.touch()


def _configure_command(
    project_dir: Path,
    build_dir: Path,
    *,
    toolchain: Toolchain,
    platform: Platform,
    sanitizer: SanitizerKind | None,
    base_flags: tuple[str, ...],
) -> list[str]:
    """Compose the configure: sanitizer flags or the base ones, warnings, then this OS's."""
    flags = toolchain.sanitize_flags(sanitizer) if sanitizer is not None else base_flags
    extras = platform.sanitize_link_extras.get(sanitizer, ()) if sanitizer is not None else ()
    # CMAKE_CXX_FLAGS is one space-separated string; a runtime library under
    # "C:\Program Files" must be quoted inside it or cmake splits the path in two
    every_flag = " ".join(
        [*flags, *toolchain.warning_flags, *platform.compile_extras, *map(_quoted, extras)]
    )
    return [
        "cmake",
        "-S",
        str(project_dir),
        "-B",
        str(build_dir),
        f"-DCMAKE_CXX_COMPILER={toolchain.compiler}",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        # empty on purpose: any build type appends its own preset, and Release's -O3 -DNDEBUG
        # would land after our -O1 -g and win, leaving a sanitizer report with no line numbers
        "-DCMAKE_BUILD_TYPE=",
        # cmake passes these to the linker as well as the compiler, which is what carries
        # -fsanitize through to the link -- a compile-only flag would build and then not link
        f"-DCMAKE_CXX_FLAGS={every_flag}",
        # the OS's own configure needs, e.g. Windows forcing a generator that obeys
        # CMAKE_CXX_COMPILER
        *platform.cmake_extras,
        # and what this particular sanitizer needs of the configure, which is not the same
        # question: these change how every object is compiled, so they cannot wait for the link
        *(platform.sanitize_cmake_extras.get(sanitizer, ()) if sanitizer is not None else ()),
    ]


def _quoted(flag: str) -> str:
    """Quote a flag that carries spaces, so a joined flags string keeps it as one word."""
    return f'"{flag}"' if " " in flag else flag


def _build_command(build_dir: Path, target: str, configuration: str) -> list[str]:
    """Compose the build; a multi-config generator must be told which configuration."""
    command = ["cmake", "--build", str(build_dir), "--target", target, "--parallel"]
    if configuration:
        # without this a multi-config generator builds its own default, and the binary at
        # the path we report is either stale or not there at all
        command += ["--config", configuration]
    return command


def _configuration(reply_dir: Path) -> _Configuration | None:
    """Return the configuration the File API reported first, or None when it cannot be read.

    None and a configuration with no executables are different answers: one is "cmake did
    not tell us", the other is "cmake told us, and this project has nothing to run".
    """
    index = _current_index(reply_dir)
    if index is None:
        return None
    codemodel = _dig(_load(index), "reply", CODEMODEL, "jsonFile")
    if not isinstance(codemodel, str):
        return None
    configurations = _dig(_load(reply_dir / codemodel), "configurations")
    if not isinstance(configurations, list) or not configurations:
        return None
    # the first entry is the one we build: its name and its artifact paths travel together
    chosen = configurations[0]
    entries = _dig(chosen, "targets")
    if not isinstance(entries, list):
        return None
    configuration_name = _dig(chosen, "name")

    found: list[_Executable] = []
    for entry in entries:
        name = _dig(entry, "jsonFile")
        if not isinstance(name, str):
            return None
        document = _load(reply_dir / name)
        if document is None:
            return None
        executable = _as_executable(document)
        if executable is not None:
            found.append(executable)
    return _Configuration(
        name=configuration_name if isinstance(configuration_name, str) else "",
        executables=tuple(found),
    )


def _current_index(reply_dir: Path) -> Path | None:
    """Return the reply's current index file.

    A reconfigure leaves the old indexes in place beside the new one, and cmake documents
    the current reply as the lexicographically greatest name -- the timestamp in it sorts
    the same way it runs.
    """
    if not reply_dir.is_dir():
        return None
    indexes = sorted(reply_dir.glob("index-*.json"))
    return indexes[-1] if indexes else None


def _as_executable(document: object) -> _Executable | None:
    """Read one target file, or None when it is not an executable with an artifact."""
    if _dig(document, "type") != EXECUTABLE:
        return None
    name = _dig(document, "name")
    artifacts = _dig(document, "artifacts")
    if not isinstance(name, str) or not isinstance(artifacts, list) or not artifacts:
        return None
    path = _dig(artifacts[0], "path")
    if not isinstance(path, str):
        return None
    return _Executable(name=name, artifact=path)


def _load(path: Path) -> object | None:
    """Parse one JSON file, or None when it is missing, unreadable or malformed."""
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # a reply we cannot parse is a reply we do not have; raising here would turn a
        # half-written build directory into a crash report about the wrong thing
        return None
    return document


def _dig(document: object, *keys: str) -> object | None:
    """Follow a chain of keys through parsed JSON, giving up at the first thing not there."""
    for key in keys:
        if not isinstance(document, dict):
            return None
        value: object = document.get(key)
        document = value
    return document


def _choose(
    executables: tuple[_Executable, ...], target: str | None, output: str
) -> _Executable | BuildFailure:
    """Pick the target to build, or explain why the project could not choose one for itself."""
    if not executables:
        return BuildFailure(
            stage=BUILD_STAGE,
            output=output,
            reason="this project defines no executable target, so a build produces nothing to run",
            suggestion="point at a project whose CMakeLists.txt declares an add_executable target",
        )
    if target is not None:
        for executable in executables:
            if executable.name == target:
                return executable
        return BuildFailure(
            stage=BUILD_STAGE,
            output=output,
            reason=(
                f"this project defines no executable target named {target!r}; "
                f"the ones it does define are {_listed(executables)}"
            ),
            suggestion=_pick_one(executables),
        )
    if len(executables) > 1:
        return BuildFailure(
            stage=BUILD_STAGE,
            output=output,
            reason=f"this project defines several executable targets: {_listed(executables)}",
            suggestion=_pick_one(executables),
        )
    return executables[0]


def _listed(executables: tuple[_Executable, ...]) -> str:
    return ", ".join(executable.name for executable in executables)


def _pick_one(executables: tuple[_Executable, ...]) -> str:
    """Hand back names the caller can pass straight into the next call."""
    return f"pass target= as one of: {_listed(executables)}"


def _failure(stage: str, platform: Platform, output: str) -> BuildFailure:
    """Carry the platform's diagnosis as strings: models.py may not import platforms."""
    signature = platform.diagnose(output)
    if signature is None:
        # no signature matched, so the output speaks for itself; inventing a reason here
        # would explain the failure wrongly and confidently
        return BuildFailure(stage=stage, output=output)
    return BuildFailure(
        stage=stage,
        output=output,
        reason=signature.reason,
        suggestion=signature.suggestion,
    )
