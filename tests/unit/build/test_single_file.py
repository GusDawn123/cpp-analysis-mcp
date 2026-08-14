"""Compile one file with no compiler anywhere: every runner here is a fake.

Every expectation is written down rather than read from the code under test -- the base
flags, the pinned TSan options, the sanitizer variables that must not survive, the exact
argument order handed to the compiler. A test that asked single_file.py what it composes
would agree with it forever, including the day it composes the wrong thing.

The Toolchain and Platform are built by hand, the way tests/unit/platforms/test_platforms.py
does it, so the Linux command line is exercised on macOS (rule 3).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp.build.single_file import compile_file, with_runtime_on_path
from cpp_analysis_mcp.models import BuildFailure, BuiltBinary, SanitizerKind, Severity
from cpp_analysis_mcp.platforms.base import FailureSignature, Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.toolchains.base import Toolchain

# ---------------------------------------------------------------- pinned expectations

# the flags every build carries, in the order they are passed; pinned so a reordered or
# thinned tuple in toolchains/base.py fails here instead of quietly changing every build
EVERY_BASE_FLAG = ("-std=c++20", "-g", "-O1", "-fno-omit-frame-pointer")

# clang's compile-time lock analysis; gcc has no equivalent, so its list stays empty
CLANG_WARNING_FLAGS = ("-Wthread-safety",)
GCC_WARNING_FLAGS: tuple[str, ...] = ()

# glibc needs this to link std::thread
LINUX_COMPILE_EXTRAS = ("-pthread",)

# what a TSan binary must be run with; written out here, never read from PINNED_RUNTIME_ENV
TSAN_OPTIONS = "history_size=7 second_deadlock_stack=1 exitcode=66 detect_deadlocks=1"

# none of these may reach a compile from the developer's shell
EVERY_SANITIZER_VAR = ("ASAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "UBSAN_OPTIONS")

UNRELATED_VAR = "CPP_ANALYSIS_MCP_UNRELATED"

DEFAULT_TIMEOUT_S = 120

# spelled through Path so the strings compare equal to str(Path(...)) on Windows too
CLANG_PATH = str(Path("/usr/bin/clang++"))
GCC_PATH = str(Path("/usr/bin/g++"))

SOURCE_STEM = "data_race"

# a sanitized build carries its variant in the name, so one source's builds coexist
TSAN_BINARY = "data_race.thread"
ASAN_BINARY = "data_race.address"

# a real link failure and the honest explanation a platform pairs with it
MISSING_TSAN_RUNTIME = "/usr/bin/ld: cannot find -ltsan: No such file or directory"
RUNTIME_PACKAGE_REASON = "the sanitizer runtime is a separate package here and is not installed"
RUNTIME_PACKAGE_SUGGESTION = "sudo apt install libtsan0"

# a failure no signature explains: a plain syntax error in the user's code
SYNTAX_ERROR = "data_race.cpp:12:9: error: expected ';' after expression\n1 error generated.\n"

THREAD_SAFETY_WARNING = (
    "data_race.cpp:21:5: warning: writing variable 'counter' requires holding "
    "mutex 'counter_mutex' exclusively [-Wthread-safety-analysis]\n"
)

SUCCESS = RunResult(exit_code=0, output="")
TIMED_OUT = RunResult(exit_code=None, output="\n[killed after 120s timeout]\n")


# --------------------------------------------------------------------------- the fakes


@dataclass
class FakeRunner:
    """Answer one canned RunResult and record everything it was asked to spawn."""

    result: RunResult = SUCCESS
    commands: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str] | None] = field(default_factory=list)
    timeouts: list[int] = field(default_factory=list)
    cwds: list[Path | None] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        self.commands.append(list(cmd))
        self.envs.append(dict(env) if env is not None else None)
        self.timeouts.append(timeout_s)
        self.cwds.append(cwd)
        return self.result

    @property
    def command(self) -> list[str]:
        """The one command this build should have run."""
        assert len(self.commands) == 1, f"expected one spawn, got {len(self.commands)}"
        return self.commands[0]

    @property
    def env(self) -> dict[str, str]:
        """The environment that one command was handed."""
        assert len(self.envs) == 1, f"expected one spawn, got {len(self.envs)}"
        env = self.envs[0]
        assert env is not None, "a compile must be given an environment, not the inherited one"
        return env


def a_clang() -> Toolchain:
    """A clang nobody had to find: fields written out, detect() never called."""
    return Toolchain(
        family="clang",
        compiler=Path(CLANG_PATH),
        version="Apple clang version 21.0.0 (clang-2100.1.1.101)",
        warning_flags=CLANG_WARNING_FLAGS,
    )


def a_gcc() -> Toolchain:
    return Toolchain(
        family="gcc",
        compiler=Path(GCC_PATH),
        version="g++ (Ubuntu 13.2.0-23ubuntu4) 13.2.0",
        warning_flags=GCC_WARNING_FLAGS,
    )


def a_linux() -> Platform:
    """Linux's compile flag and one link failure it knows how to explain."""
    return Platform(
        name="linux",
        compile_extras=LINUX_COMPILE_EXTRAS,
        failure_signatures=(
            FailureSignature(
                marker="cannot find -ltsan",
                reason=RUNTIME_PACKAGE_REASON,
                suggestion=RUNTIME_PACKAGE_SUGGESTION,
            ),
        ),
    )


def a_darwin() -> Platform:
    """macOS adds nothing to a compile line, and this build knows no macOS failures."""
    return Platform(name="darwin")


def a_windows(tmp_path: Path) -> Platform:
    """Windows's two measured link quirks: UBSan's libs by path, ASan's DLL beside the exe."""
    dll = tmp_path / "runtime" / "clang_rt.asan_dynamic-x86_64.dll"
    dll.parent.mkdir(parents=True, exist_ok=True)
    dll.write_bytes(b"not a real dll")
    return Platform(
        name="windows",
        executable_suffix=".exe",
        sanitize_link_extras={
            SanitizerKind.UNDEFINED: (str(dll.parent / "clang_rt.ubsan_standalone-x86_64.lib"),)
        },
        runtime_dlls={SanitizerKind.ADDRESS: (dll,)},
    )


def a_source(tmp_path: Path) -> Path:
    source = tmp_path / f"{SOURCE_STEM}.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")
    return source


def built(result: BuiltBinary | BuildFailure) -> BuiltBinary:
    assert isinstance(result, BuiltBinary), f"expected a BuiltBinary, got {result}"
    return result


def failed(result: BuiltBinary | BuildFailure) -> BuildFailure:
    assert isinstance(result, BuildFailure), f"expected a BuildFailure, got {result}"
    return result


# ---------------------------------------------------------------- command composition


def test_clang_tsan_on_linux_composes_this_exact_command(tmp_path: Path) -> None:
    source = a_source(tmp_path)
    build_dir = tmp_path / "build"
    runner = FakeRunner()

    compile_file(
        source,
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=build_dir,
        runner=runner,
    )

    assert runner.command == [
        CLANG_PATH,
        *EVERY_BASE_FLAG,
        "-fsanitize=thread",
        *CLANG_WARNING_FLAGS,
        *LINUX_COMPILE_EXTRAS,
        str(source),
        "-o",
        str(build_dir / TSAN_BINARY),
    ]


def test_no_sanitizer_means_the_base_flags_and_nothing_else(tmp_path: Path) -> None:
    """An unsanitized build must carry no -fsanitize anywhere: it is a different binary."""
    source = a_source(tmp_path)
    build_dir = tmp_path / "build"
    runner = FakeRunner()

    compile_file(
        source,
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=None,
        build_dir=build_dir,
        runner=runner,
    )

    assert runner.command == [
        CLANG_PATH,
        *EVERY_BASE_FLAG,
        *CLANG_WARNING_FLAGS,
        *LINUX_COMPILE_EXTRAS,
        str(source),
        "-o",
        str(build_dir / SOURCE_STEM),
    ]
    assert not [arg for arg in runner.command if "-fsanitize" in arg]


def test_gcc_gets_no_thread_safety_flag(tmp_path: Path) -> None:
    """gcc has no equivalent, so passing the flag would fail the build outright."""
    source = a_source(tmp_path)
    build_dir = tmp_path / "build"
    runner = FakeRunner()

    compile_file(
        source,
        toolchain=a_gcc(),
        platform=a_linux(),
        sanitizer=SanitizerKind.ADDRESS,
        build_dir=build_dir,
        runner=runner,
    )

    assert runner.command == [
        GCC_PATH,
        *EVERY_BASE_FLAG,
        "-fsanitize=address",
        *LINUX_COMPILE_EXTRAS,
        str(source),
        "-o",
        str(build_dir / ASAN_BINARY),
    ]
    assert "-Wthread-safety" not in runner.command


def test_windows_names_the_binary_exe_and_links_its_own_runtime(tmp_path: Path) -> None:
    """The platform's link extras ride at the end of the command, where a linker expects
    libraries; without them Windows resolves the runtime to MSVC's incompatible copy."""
    source = a_source(tmp_path)
    build_dir = tmp_path / "build"
    windows = a_windows(tmp_path)
    runner = FakeRunner()

    compile_file(
        source,
        toolchain=a_clang(),
        platform=windows,
        sanitizer=SanitizerKind.UNDEFINED,
        build_dir=build_dir,
        runner=runner,
    )

    expected_extras = list(windows.sanitize_link_extras[SanitizerKind.UNDEFINED])
    assert runner.command[-len(expected_extras) :] == expected_extras
    assert str(build_dir / f"{SOURCE_STEM}.undefined.exe") in runner.command


def test_the_asan_dll_lands_beside_a_built_binary(tmp_path: Path) -> None:
    """ASan's runtime is a DLL the Windows loader only finds next to the executable; a
    build that succeeded without placing it dies before main with nothing printed."""
    source = a_source(tmp_path)
    build_dir = tmp_path / "build"

    compile_file(
        source,
        toolchain=a_clang(),
        platform=a_windows(tmp_path),
        sanitizer=SanitizerKind.ADDRESS,
        build_dir=build_dir,
        runner=FakeRunner(),
    )

    assert (build_dir / "clang_rt.asan_dynamic-x86_64.dll").is_file()


def test_a_failed_build_places_no_dll(tmp_path: Path) -> None:
    """The DLL travels with a binary; a build that produced nothing gets nothing."""
    source = a_source(tmp_path)
    build_dir = tmp_path / "build"

    compile_file(
        source,
        toolchain=a_clang(),
        platform=a_windows(tmp_path),
        sanitizer=SanitizerKind.ADDRESS,
        build_dir=build_dir,
        runner=FakeRunner(result=RunResult(exit_code=1, output=SYNTAX_ERROR)),
    )

    assert not (build_dir / "clang_rt.asan_dynamic-x86_64.dll").exists()


def test_macos_adds_no_compile_extras(tmp_path: Path) -> None:
    source = a_source(tmp_path)
    runner = FakeRunner()

    compile_file(
        source,
        toolchain=a_clang(),
        platform=a_darwin(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=tmp_path / "build",
        runner=runner,
    )

    assert "-pthread" not in runner.command


def test_the_default_timeout_is_pinned(tmp_path: Path) -> None:
    runner = FakeRunner()

    compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_darwin(),
        sanitizer=None,
        build_dir=tmp_path / "build",
        runner=runner,
    )

    assert runner.timeouts == [DEFAULT_TIMEOUT_S]


def test_a_caller_can_shorten_the_timeout(tmp_path: Path) -> None:
    runner = FakeRunner()

    compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_darwin(),
        sanitizer=None,
        build_dir=tmp_path / "build",
        timeout_s=7,
        runner=runner,
    )

    assert runner.timeouts == [7]


# ------------------------------------------------------------------------ env hygiene


def test_the_compile_never_sees_a_sanitizer_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded in this process's environment first, so this proves stripping, not absence."""
    for name in EVERY_SANITIZER_VAR:
        monkeypatch.setenv(name, "verbosity=1")
    monkeypatch.setenv(UNRELATED_VAR, "keep me")
    runner = FakeRunner()

    compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_darwin(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=tmp_path / "build",
        runner=runner,
    )

    assert [name for name in EVERY_SANITIZER_VAR if name in runner.env] == []
    # the rest of the environment is still there, so the compiler finds its own tools
    assert runner.env[UNRELATED_VAR] == "keep me"


# --------------------------------------------------------------------------- failures


def test_a_timeout_is_a_build_failure(tmp_path: Path) -> None:
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=tmp_path / "build",
        runner=FakeRunner(TIMED_OUT),
    )

    failure = failed(result)
    assert failure.stage == "compile"
    assert failure.timed_out is True
    assert "[killed after 120s timeout]" in failure.output
    assert failure.reason is None


def test_a_matching_signature_explains_the_failure(tmp_path: Path) -> None:
    """The platform knows what `cannot find -ltsan` means; the compiler's text does not."""
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=tmp_path / "build",
        runner=FakeRunner(RunResult(exit_code=1, output=MISSING_TSAN_RUNTIME)),
    )

    failure = failed(result)
    assert failure.stage == "compile"
    assert failure.timed_out is False
    assert failure.reason == RUNTIME_PACKAGE_REASON
    assert failure.suggestion == RUNTIME_PACKAGE_SUGGESTION
    # the raw text survives alongside the explanation
    assert failure.output == MISSING_TSAN_RUNTIME


def test_an_unexplained_failure_reports_the_output_and_no_reason(tmp_path: Path) -> None:
    """No signature matched, so the compiler's own words are all there honestly is."""
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=None,
        build_dir=tmp_path / "build",
        runner=FakeRunner(RunResult(exit_code=1, output=SYNTAX_ERROR)),
    )

    failure = failed(result)
    assert failure.stage == "compile"
    assert failure.reason is None
    assert failure.suggestion is None
    assert failure.timed_out is False
    assert failure.output == SYNTAX_ERROR


# ---------------------------------------------------------------------------- success


def test_a_tsan_build_carries_the_pinned_options(tmp_path: Path) -> None:
    """The half of the decision that makes the run report anything at all."""
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=tmp_path / "build",
        runner=FakeRunner(),
    )

    binary = built(result)
    assert dict(binary.runtime_env) == {"TSAN_OPTIONS": TSAN_OPTIONS}
    assert binary.sanitizer is SanitizerKind.THREAD


def test_an_unsanitized_build_carries_no_options(tmp_path: Path) -> None:
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=None,
        build_dir=tmp_path / "build",
        runner=FakeRunner(),
    )

    binary = built(result)
    assert dict(binary.runtime_env) == {}
    assert binary.sanitizer is None


def test_an_undefined_build_carries_its_own_options(tmp_path: Path) -> None:
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=SanitizerKind.UNDEFINED,
        build_dir=tmp_path / "build",
        runner=FakeRunner(),
    )

    assert dict(built(result).runtime_env) == {"UBSAN_OPTIONS": "print_stacktrace=1"}


def test_the_binary_lands_in_the_build_dir_under_the_source_stem(tmp_path: Path) -> None:
    build_dir = tmp_path / "build"
    source = a_source(tmp_path)

    result = compile_file(
        source,
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=None,
        build_dir=build_dir,
        runner=FakeRunner(),
    )

    binary = built(result)
    assert binary.path == build_dir / SOURCE_STEM
    assert binary.build_dir == build_dir
    assert binary.compile_commands is None


def test_two_sanitizers_builds_of_one_source_do_not_collide(tmp_path: Path) -> None:
    """TSan then ASan into one directory: the first binary must survive the second build.

    Overwriting would leave the first BuiltBinary's path holding the second's binary --
    bound to the wrong runtime environment, which is the silent-run bug this layer exists
    to make impossible.
    """
    build_dir = tmp_path / "build"
    source = a_source(tmp_path)

    def compiled(sanitizer: SanitizerKind) -> BuiltBinary:
        result = compile_file(
            source,
            toolchain=a_clang(),
            platform=a_linux(),
            sanitizer=sanitizer,
            build_dir=build_dir,
            runner=FakeRunner(),
        )
        return built(result)

    tsan = compiled(SanitizerKind.THREAD)
    asan = compiled(SanitizerKind.ADDRESS)

    assert tsan.path != asan.path
    assert tsan.path == build_dir / TSAN_BINARY
    assert asan.path == build_dir / ASAN_BINARY


def test_a_missing_build_dir_is_created(tmp_path: Path) -> None:
    build_dir = tmp_path / "nested" / "build"
    assert not build_dir.exists()

    compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=None,
        build_dir=build_dir,
        runner=FakeRunner(),
    )

    assert build_dir.is_dir()


def test_a_quiet_build_has_no_warnings(tmp_path: Path) -> None:
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=None,
        build_dir=tmp_path / "build",
        runner=FakeRunner(),
    )

    assert built(result).warnings == ()


def test_a_successful_build_still_reports_its_warnings(tmp_path: Path) -> None:
    """-Wthread-safety fires while compiling, so exit 0 does not mean nothing was found."""
    result = compile_file(
        a_source(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=None,
        build_dir=tmp_path / "build",
        runner=FakeRunner(RunResult(exit_code=0, output=THREAD_SAFETY_WARNING)),
    )

    warnings = built(result).warnings
    assert len(warnings) == 1
    assert warnings[0].tool == "compiler"
    assert warnings[0].severity is Severity.WARNING
    assert warnings[0].category == "thread-safety-analysis"
    assert warnings[0].location is not None
    assert (warnings[0].location.line, warnings[0].location.column) == (21, 5)


# --------------------------------------------------------------- the runtime a build must find


def test_the_runtime_directory_goes_on_path_for_a_build_that_runs_what_it_links(
    tmp_path: Path,
) -> None:
    """Copying the DLL beside the binary covers running it afterwards and nothing else.

    gtest_discover_tests executes each freshly linked test program during the build to
    enumerate the tests in it, before anything has been copied anywhere. The loader then
    cannot find ASan's runtime and the program dies on STATUS_DLL_NOT_FOUND printing nothing,
    which reads as a build broken inside a test framework.
    """
    runtime = tmp_path / "llvm" / "windows"
    platform = Platform(
        name="windows",
        runtime_dlls={SanitizerKind.ADDRESS: (runtime / "clang_rt.asan_dynamic-x86_64.dll",)},
    )

    env = with_runtime_on_path(platform, SanitizerKind.ADDRESS, {"PATH": "C:\\already"})

    assert env["PATH"].split(os.pathsep)[0] == str(runtime)
    assert "C:\\already" in env["PATH"]


def test_a_platform_with_no_runtime_dlls_is_left_alone(tmp_path: Path) -> None:
    """Linux and macOS keep their sanitizer runtimes where the loader already looks."""
    env = {"PATH": "/usr/bin"}

    assert with_runtime_on_path(a_linux(), SanitizerKind.ADDRESS, env) == env
    assert with_runtime_on_path(a_linux(), None, env) == env


def test_an_environment_with_no_path_gets_one(tmp_path: Path) -> None:
    """hygienic_env copies the real environment, but a caller may hand over anything."""
    runtime = tmp_path / "windows"
    platform = Platform(
        name="windows", runtime_dlls={SanitizerKind.ADDRESS: (runtime / "asan.dll",)}
    )

    env = with_runtime_on_path(platform, SanitizerKind.ADDRESS, {})

    assert env["PATH"] == str(runtime)
