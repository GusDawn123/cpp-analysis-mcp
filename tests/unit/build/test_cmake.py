"""Build a cmake project with no cmake anywhere: every runner here is a fake.

The trick that makes this possible is that cmake answers the "which targets did you build
and where did they land" question through files. A fake configure can write that reply tree
itself, so target selection, the executable filter and the index rule are all exercised at
unit speed, with the real thing left to the integration suite.

Every expectation is written down rather than read from the code under test -- the configure
argv, the joined flag string, the pinned TSan options, the sanitizer variables that must not
survive. A test that asked cmake.py what it composes would agree with it forever, including
the day it composes the wrong thing.

The Toolchain and Platform are built by hand, the way tests/unit/build/test_single_file.py
does it, so the Linux command line is exercised on macOS (rule 3).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp.build.cmake import build_project
from cpp_analysis_mcp.models import BuildFailure, BuiltBinary, SanitizerKind, Severity
from cpp_analysis_mcp.platforms.base import FailureSignature, Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.toolchains.base import Toolchain

# ---------------------------------------------------------------- pinned expectations

# where cmake's File API lives inside a build tree, spelled out rather than imported: this
# layout is cmake's contract, so a change here is a change of protocol, not of style
QUERY_FILE = Path(".cmake") / "api" / "v1" / "query" / "codemodel-v2"
REPLY_DIR = Path(".cmake") / "api" / "v1" / "reply"

# the flags every build carries, in the order they are passed
EVERY_BASE_FLAG = ("-std=c++20", "-g", "-O1", "-fno-omit-frame-pointer")

CLANG_WARNING_FLAGS = ("-Wthread-safety",)
LINUX_COMPILE_EXTRAS = ("-pthread",)

# one string, because cmake takes one -D per variable; pinned whole so a reordering or a
# lost flag shows up as a diff of the thing cmake actually receives
TSAN_CXX_FLAGS = (
    "-DCMAKE_CXX_FLAGS=-std=c++20 -g -O1 -fno-omit-frame-pointer -fsanitize=thread "
    "-Wthread-safety -pthread"
)
PLAIN_CXX_FLAGS = (
    "-DCMAKE_CXX_FLAGS=-std=c++20 -g -O1 -fno-omit-frame-pointer -Wthread-safety -pthread"
)

# what a TSan binary must be run with; written out here, never read from PINNED_RUNTIME_ENV
TSAN_OPTIONS = "history_size=7 second_deadlock_stack=1 exitcode=66 detect_deadlocks=1"

# none of these may reach a build from the developer's shell
EVERY_SANITIZER_VAR = ("ASAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "UBSAN_OPTIONS")

UNRELATED_VAR = "CPP_ANALYSIS_MCP_UNRELATED"

DEFAULT_TIMEOUT_S = 600

# spelled through Path so the string compares equal to str(Path(...)) on Windows too
CLANG_PATH = str(Path("/usr/bin/clang++"))

# the target the fabricated reply describes, and where cmake said its binary lands: a
# subdirectory, so a build that returned the bare name instead of joining would fail here
APP_NAME = "app"
APP_ARTIFACT = "bin/app"

LIBRARY_NAME = "helper"
LIBRARY_ARTIFACT = "libhelper.a"

# cmake stamps its reply index with the time it configured, which sorts the way it ran
EARLIER_STAMP = "2026-01-02T03-04-05-0000"
LATER_STAMP = "2026-06-07T08-09-10-0000"

# a multi-config generator writes each configuration's binaries into its own subdirectory
RELEASE_ARTIFACT = "Release/bin/app"
DEBUG_ARTIFACT = "Debug/bin/app"

STALE_ARTIFACT = "bin/app_from_the_older_configure"

# a real cmake failure and the honest explanation a platform pairs with it
MISSING_COMPILER = "CMake Error: CMAKE_CXX_COMPILER not set, after EnableLanguage"
COMPILER_REASON = "the compiler cmake was pointed at is not on this machine"
COMPILER_SUGGESTION = "install clang or pass a compiler that exists"

# a failure no signature explains: a plain syntax error in the user's code
SYNTAX_ERROR = "main.cpp:12:9: error: expected ';' after expression\n1 error generated.\n"

THREAD_SAFETY_WARNING = (
    "src/orders.cpp:21:5: warning: writing variable 'counter' requires holding "
    "mutex 'counter_mutex' exclusively [-Wthread-safety-analysis]\n"
)

SUCCESS = RunResult(exit_code=0, output="")
TIMED_OUT = RunResult(exit_code=None, output="\n[killed after 600s timeout]\n")


# ------------------------------------------------------------ the reply cmake writes


@dataclass(frozen=True)
class FakeTarget:
    """One entry of the codemodel, as the fabricated reply will spell it out."""

    name: str
    kind: str  # cmake's "type": EXECUTABLE, STATIC_LIBRARY, ...
    artifact: str | None = None


def an_executable(name: str = APP_NAME, artifact: str = APP_ARTIFACT) -> FakeTarget:
    return FakeTarget(name=name, kind="EXECUTABLE", artifact=artifact)


def a_library() -> FakeTarget:
    return FakeTarget(name=LIBRARY_NAME, kind="STATIC_LIBRARY", artifact=LIBRARY_ARTIFACT)


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def write_reply(build_dir: Path, targets: Sequence[FakeTarget], stamp: str) -> None:
    """Leave behind the index, codemodel and target files a real configure would have."""
    reply_dir = build_dir / REPLY_DIR
    entries: list[dict[str, object]] = []
    for target in targets:
        name = f"target-{target.name}-{stamp}.json"
        body: dict[str, object] = {"name": target.name, "type": target.kind}
        if target.artifact is not None:
            body["artifacts"] = [{"path": target.artifact}]
        write_json(reply_dir / name, body)
        entries.append({"name": target.name, "jsonFile": name})

    codemodel = f"codemodel-v2-{stamp}.json"
    write_json(reply_dir / codemodel, {"configurations": [{"name": "", "targets": entries}]})
    write_json(
        reply_dir / f"index-{stamp}.json", {"reply": {"codemodel-v2": {"jsonFile": codemodel}}}
    )


Writer = Callable[[Path], None]


def a_reply(*targets: FakeTarget, stamp: str = LATER_STAMP, database: bool = False) -> Writer:
    """Return what a configure does to the build directory: a reply, and maybe a database."""

    def write(build_dir: Path) -> None:
        write_reply(build_dir, targets, stamp)
        if database:
            (build_dir / "compile_commands.json").write_text("[]", encoding="utf-8")

    return write


def two_replies(build_dir: Path) -> None:
    """Two configures' worth of reply, the older one describing a binary that has moved.

    Nothing prunes the old index, so picking the wrong one is a live way to hand back a path
    to a file that is not there.
    """
    write_reply(build_dir, [an_executable(artifact=STALE_ARTIFACT)], EARLIER_STAMP)
    write_reply(build_dir, [an_executable()], LATER_STAMP)


def a_multi_config_reply(build_dir: Path) -> None:
    """What a multi-config generator leaves: several configurations, Release listed first.

    Each configuration carries its own artifact paths, so reading one and building another
    hands back a path the build never wrote. Release-first proves nothing assumes Debug.
    """
    reply_dir = build_dir / REPLY_DIR
    entries: dict[str, list[dict[str, object]]] = {}
    for configuration, artifact in (("Release", RELEASE_ARTIFACT), ("Debug", DEBUG_ARTIFACT)):
        name = f"target-{APP_NAME}-{configuration}-{LATER_STAMP}.json"
        write_json(
            reply_dir / name,
            {"name": APP_NAME, "type": "EXECUTABLE", "artifacts": [{"path": artifact}]},
        )
        entries[configuration] = [{"name": APP_NAME, "jsonFile": name}]

    codemodel = f"codemodel-v2-{LATER_STAMP}.json"
    write_json(
        reply_dir / codemodel,
        {
            "configurations": [
                {"name": configuration, "targets": targets}
                for configuration, targets in entries.items()
            ]
        },
    )
    write_json(
        reply_dir / f"index-{LATER_STAMP}.json",
        {"reply": {"codemodel-v2": {"jsonFile": codemodel}}},
    )


def a_garbled_index(build_dir: Path) -> None:
    """A reply interrupted mid-write: the file exists and is not JSON."""
    path = build_dir / REPLY_DIR / f"index-{LATER_STAMP}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"reply": {"codemodel', encoding="utf-8")


def an_index_naming_a_missing_codemodel(build_dir: Path) -> None:
    write_json(
        build_dir / REPLY_DIR / f"index-{LATER_STAMP}.json",
        {"reply": {"codemodel-v2": {"jsonFile": "codemodel-v2-gone.json"}}},
    )


def a_codemodel_with_no_configurations(build_dir: Path) -> None:
    codemodel = f"codemodel-v2-{LATER_STAMP}.json"
    write_json(build_dir / REPLY_DIR / codemodel, {"configurations": []})
    write_json(
        build_dir / REPLY_DIR / f"index-{LATER_STAMP}.json",
        {"reply": {"codemodel-v2": {"jsonFile": codemodel}}},
    )


def a_codemodel_naming_a_missing_target(build_dir: Path) -> None:
    codemodel = f"codemodel-v2-{LATER_STAMP}.json"
    write_json(
        build_dir / REPLY_DIR / codemodel,
        {"configurations": [{"targets": [{"name": APP_NAME, "jsonFile": "target-gone.json"}]}]},
    )
    write_json(
        build_dir / REPLY_DIR / f"index-{LATER_STAMP}.json",
        {"reply": {"codemodel-v2": {"jsonFile": codemodel}}},
    )


# --------------------------------------------------------------------------- the fake


@dataclass
class FakeCmake:
    """Stand in for both cmake runs, writing on the configure what a real one leaves behind.

    The query file check happens from inside the call: afterwards there is no way to tell a
    query written before the configure -- the only time cmake reads it -- from one written
    after, and a query cmake never saw produces no reply at all.
    """

    on_configure: Writer | None = None
    configure_result: RunResult = SUCCESS
    build_result: RunResult = SUCCESS
    commands: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str] | None] = field(default_factory=list)
    timeouts: list[int] = field(default_factory=list)
    query_seen: list[bool] = field(default_factory=list)

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

        if "--build" in cmd:
            return self.build_result

        build_dir = Path(cmd[cmd.index("-B") + 1])
        self.query_seen.append((build_dir / QUERY_FILE).is_file())
        if self.on_configure is not None:
            self.on_configure(build_dir)
        return self.configure_result

    @property
    def configure_command(self) -> list[str]:
        assert self.commands, "nothing was spawned at all"
        return self.commands[0]

    @property
    def build_command(self) -> list[str]:
        assert len(self.commands) == 2, f"expected a configure then a build, got {self.commands}"
        return self.commands[1]

    def env_of(self, index: int) -> dict[str, str]:
        env = self.envs[index]
        assert env is not None, "a cmake run must be given an environment, not the inherited one"
        return env


# ------------------------------------------------------------------------- the inputs


def a_clang() -> Toolchain:
    """A clang nobody had to find: fields written out, detect() never called."""
    return Toolchain(
        family="clang",
        compiler=Path(CLANG_PATH),
        version="Apple clang version 21.0.0 (clang-2100.1.1.101)",
        warning_flags=CLANG_WARNING_FLAGS,
    )


def a_linux() -> Platform:
    """Linux's compile flag and one configure failure it knows how to explain."""
    return Platform(
        name="linux",
        compile_extras=LINUX_COMPILE_EXTRAS,
        failure_signatures=(
            FailureSignature(
                marker="CMAKE_CXX_COMPILER not set",
                reason=COMPILER_REASON,
                suggestion=COMPILER_SUGGESTION,
            ),
        ),
    )


def a_project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return project_dir


def build(
    tmp_path: Path,
    runner: FakeCmake,
    *,
    sanitizer: SanitizerKind | None = SanitizerKind.THREAD,
    target: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> BuiltBinary | BuildFailure:
    """Run the build under test against the fake, with the arguments the tests vary."""
    return build_project(
        a_project(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=sanitizer,
        build_dir=tmp_path / "build",
        target=target,
        timeout_s=timeout_s,
        runner=runner,
    )


def built(result: BuiltBinary | BuildFailure) -> BuiltBinary:
    assert isinstance(result, BuiltBinary), f"expected a BuiltBinary, got {result}"
    return result


def failed(result: BuiltBinary | BuildFailure) -> BuildFailure:
    assert isinstance(result, BuildFailure), f"expected a BuildFailure, got {result}"
    return result


# ---------------------------------------------------------------- command composition


def test_clang_tsan_on_linux_composes_this_exact_configure(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    build_dir = tmp_path / "build"
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    build_project(
        project_dir,
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=build_dir,
        runner=runner,
    )

    assert runner.configure_command == [
        "cmake",
        "-S",
        str(project_dir),
        "-B",
        str(build_dir),
        f"-DCMAKE_CXX_COMPILER={CLANG_PATH}",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        # empty on purpose: a build type's own -O3 -DNDEBUG would land after ours and win
        "-DCMAKE_BUILD_TYPE=",
        TSAN_CXX_FLAGS,
    ]


def test_no_sanitizer_means_the_base_flags_and_nothing_else(tmp_path: Path) -> None:
    """An unsanitized build must carry no -fsanitize anywhere: it is a different binary."""
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    build(tmp_path, runner, sanitizer=None)

    assert PLAIN_CXX_FLAGS in runner.configure_command
    assert not [arg for arg in runner.configure_command if "-fsanitize" in arg]


def test_the_build_names_its_target_and_asks_for_parallelism(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    build(tmp_path, runner)

    assert runner.build_command == [
        "cmake",
        "--build",
        str(tmp_path / "build"),
        "--target",
        APP_NAME,
        "--parallel",
    ]


def test_a_multi_config_generator_builds_the_configuration_it_reports(tmp_path: Path) -> None:
    """The artifact path and the --config must come from the same configuration entry.

    A user's shell can hand cmake a multi-config generator through CMAKE_GENERATOR, where
    CMAKE_BUILD_TYPE is ignored and an untold build produces its own default -- analyzing a
    binary from a different configuration than the one reported is the wrong-binary bug
    this layer exists to prevent.
    """
    runner = FakeCmake(on_configure=a_multi_config_reply)

    result = build(tmp_path, runner)

    assert built(result).path == tmp_path / "build" / RELEASE_ARTIFACT
    assert runner.build_command == [
        "cmake",
        "--build",
        str(tmp_path / "build"),
        "--target",
        APP_NAME,
        "--parallel",
        "--config",
        "Release",
    ]


def test_the_query_is_in_place_before_cmake_configures(tmp_path: Path) -> None:
    """cmake reads the query while configuring; one written afterwards is answered by nobody."""
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    build(tmp_path, runner)

    assert runner.query_seen == [True]
    assert (tmp_path / "build" / QUERY_FILE).read_text(encoding="utf-8") == ""


def test_each_cmake_run_gets_the_whole_timeout(tmp_path: Path) -> None:
    """The budget is per invocation: a slow configure must not eat the build's allowance."""
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    # called without timeout_s, so this pins the default as well as the per-run split
    build_project(
        a_project(tmp_path),
        toolchain=a_clang(),
        platform=a_linux(),
        sanitizer=SanitizerKind.THREAD,
        build_dir=tmp_path / "build",
        runner=runner,
    )

    assert runner.timeouts == [DEFAULT_TIMEOUT_S, DEFAULT_TIMEOUT_S]


def test_a_caller_can_shorten_the_timeout(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    build(tmp_path, runner, timeout_s=7)

    assert runner.timeouts == [7, 7]


# ------------------------------------------------------------------------ env hygiene


def test_neither_cmake_run_sees_a_sanitizer_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded in this process's environment first, so this proves stripping, not absence."""
    for name in EVERY_SANITIZER_VAR:
        monkeypatch.setenv(name, "verbosity=1")
    monkeypatch.setenv(UNRELATED_VAR, "keep me")
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    build(tmp_path, runner)

    for index, stage in enumerate(("configure", "build")):
        env = runner.env_of(index)
        assert [name for name in EVERY_SANITIZER_VAR if name in env] == [], f"leaked into {stage}"
        # the rest of the environment is still there, so cmake finds its own tools
        assert env[UNRELATED_VAR] == "keep me"


# --------------------------------------------------------------------- target picking


def test_the_one_executable_is_found_without_being_named(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    result = build(tmp_path, runner)

    assert built(result).path == tmp_path / "build" / APP_ARTIFACT


def test_a_library_is_not_something_to_run(tmp_path: Path) -> None:
    """A STATIC_LIBRARY has an artifact too, so a filter on artifacts alone picks the wrong one."""
    runner = FakeCmake(on_configure=a_reply(a_library(), an_executable()))

    result = build(tmp_path, runner)

    binary = built(result)
    assert binary.path == tmp_path / "build" / APP_ARTIFACT
    assert LIBRARY_ARTIFACT not in str(binary.path)


def test_several_executables_and_no_target_names_them_all(tmp_path: Path) -> None:
    """Choosing for the caller would be a guess, and the wrong guess costs minutes."""
    runner = FakeCmake(
        on_configure=a_reply(an_executable("alpha", "bin/alpha"), an_executable("beta", "bin/beta"))
    )

    result = build(tmp_path, runner)

    failure = failed(result)
    assert failure.stage == "build"
    assert failure.reason is not None
    assert "alpha" in failure.reason
    assert "beta" in failure.reason
    assert failure.suggestion is not None
    assert "target=" in failure.suggestion
    # nothing was built, so cmake was never asked to
    assert len(runner.commands) == 1


def test_a_named_target_settles_the_ambiguity(tmp_path: Path) -> None:
    runner = FakeCmake(
        on_configure=a_reply(an_executable("alpha", "bin/alpha"), an_executable("beta", "bin/beta"))
    )

    result = build(tmp_path, runner, target="beta")

    assert built(result).path == tmp_path / "build" / "bin/beta"
    assert runner.build_command == [
        "cmake",
        "--build",
        str(tmp_path / "build"),
        "--target",
        "beta",
        "--parallel",
    ]


def test_a_target_that_is_not_there_says_what_is(tmp_path: Path) -> None:
    runner = FakeCmake(
        on_configure=a_reply(an_executable("alpha", "bin/alpha"), an_executable("beta", "bin/beta"))
    )

    result = build(tmp_path, runner, target="missing")

    failure = failed(result)
    assert failure.stage == "build"
    assert failure.reason is not None
    assert "missing" in failure.reason
    assert "alpha" in failure.reason
    assert "beta" in failure.reason
    assert failure.suggestion is not None
    assert "alpha" in failure.suggestion


def test_a_project_with_nothing_to_run_says_so(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(a_library()))

    result = build(tmp_path, runner)

    failure = failed(result)
    assert failure.reason is not None
    assert "no executable target" in failure.reason
    assert len(runner.commands) == 1


def test_the_newest_index_is_the_one_that_counts(tmp_path: Path) -> None:
    """A reconfigure leaves its predecessor's index in place, describing a binary that moved."""
    runner = FakeCmake(on_configure=two_replies)

    result = build(tmp_path, runner)

    assert built(result).path == tmp_path / "build" / APP_ARTIFACT


# ----------------------------------------------------------------- unreadable replies


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(None, id="no reply written at all"),
        pytest.param(a_garbled_index, id="an index that is not json"),
        pytest.param(an_index_naming_a_missing_codemodel, id="codemodel file absent"),
        pytest.param(a_codemodel_with_no_configurations, id="codemodel describing nothing"),
        pytest.param(a_codemodel_naming_a_missing_target, id="target file absent"),
    ],
)
def test_an_unreadable_reply_is_a_configure_failure(tmp_path: Path, writer: Writer | None) -> None:
    """cmake exited 0 and still told us nothing, which is not a crash and not a build."""
    runner = FakeCmake(on_configure=writer)

    result = build(tmp_path, runner)

    failure = failed(result)
    assert failure.stage == "configure"
    assert failure.reason is not None
    assert "File API" in failure.reason
    assert failure.suggestion is not None
    # a build was never attempted, so nothing can be reported about one
    assert len(runner.commands) == 1


# --------------------------------------------------------------------------- failures


def test_a_configure_timeout_is_a_configure_failure(tmp_path: Path) -> None:
    runner = FakeCmake(configure_result=TIMED_OUT)

    failure = failed(build(tmp_path, runner))

    assert failure.stage == "configure"
    assert failure.timed_out is True
    assert "[killed after 600s timeout]" in failure.output
    assert failure.reason is None


def test_a_build_timeout_is_a_build_failure(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(an_executable()), build_result=TIMED_OUT)

    failure = failed(build(tmp_path, runner))

    assert failure.stage == "build"
    assert failure.timed_out is True
    assert "[killed after 600s timeout]" in failure.output


def test_a_matching_signature_explains_a_configure_failure(tmp_path: Path) -> None:
    """The platform knows what cmake's complaint means; cmake's own text does not."""
    runner = FakeCmake(configure_result=RunResult(exit_code=1, output=MISSING_COMPILER))

    failure = failed(build(tmp_path, runner))

    assert failure.stage == "configure"
    assert failure.timed_out is False
    assert failure.reason == COMPILER_REASON
    assert failure.suggestion == COMPILER_SUGGESTION
    # the raw text survives alongside the explanation
    assert failure.output == MISSING_COMPILER


def test_a_compile_error_fails_the_build_stage(tmp_path: Path) -> None:
    """Which step died is the first thing a reader needs: this one configured fine."""
    runner = FakeCmake(
        on_configure=a_reply(an_executable()),
        build_result=RunResult(exit_code=1, output=SYNTAX_ERROR),
    )

    failure = failed(build(tmp_path, runner))

    assert failure.stage == "build"
    assert failure.output == SYNTAX_ERROR
    # no signature matched, so the compiler's own words are all there honestly is
    assert failure.reason is None
    assert failure.suggestion is None


# ---------------------------------------------------------------------------- success


def test_a_tsan_build_carries_the_pinned_options(tmp_path: Path) -> None:
    """The half of the decision that makes the run report anything at all."""
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    binary = built(build(tmp_path, runner))

    assert dict(binary.runtime_env) == {"TSAN_OPTIONS": TSAN_OPTIONS}
    assert binary.sanitizer is SanitizerKind.THREAD
    assert binary.build_dir == tmp_path / "build"


def test_an_unsanitized_build_carries_no_options(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    binary = built(build(tmp_path, runner, sanitizer=None))

    assert dict(binary.runtime_env) == {}
    assert binary.sanitizer is None


def test_the_compilation_database_is_reported_when_cmake_wrote_one(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(an_executable(), database=True))

    binary = built(build(tmp_path, runner))

    assert binary.compile_commands == tmp_path / "build" / "compile_commands.json"


def test_a_database_that_is_not_there_is_not_claimed(tmp_path: Path) -> None:
    """Some generators ignore the export flag; a path to nothing sends clang-tidy nowhere."""
    runner = FakeCmake(on_configure=a_reply(an_executable(), database=False))

    binary = built(build(tmp_path, runner))

    assert binary.compile_commands is None


def test_a_quiet_build_has_no_warnings(tmp_path: Path) -> None:
    runner = FakeCmake(on_configure=a_reply(an_executable()))

    assert built(build(tmp_path, runner)).warnings == ()


def test_a_successful_build_still_reports_its_warnings(tmp_path: Path) -> None:
    """-Wthread-safety fires while compiling, so exit 0 does not mean nothing was found."""
    runner = FakeCmake(
        on_configure=a_reply(an_executable()),
        build_result=RunResult(exit_code=0, output=THREAD_SAFETY_WARNING),
    )

    warnings = built(build(tmp_path, runner)).warnings

    assert len(warnings) == 1
    assert warnings[0].tool == "compiler"
    assert warnings[0].severity is Severity.WARNING
    assert warnings[0].category == "thread-safety-analysis"
    assert warnings[0].location is not None
    assert (warnings[0].location.line, warnings[0].location.column) == (21, 5)
