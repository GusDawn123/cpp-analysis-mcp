"""Drive the six tools over a real MCP session with no compiler and no child process anywhere.

The client here is the SDK's in-memory one, so what these tests exercise is the protocol
surface an assistant actually meets: the tool names it can call, the descriptions it chooses
from, the schemas it validates against, and the JSON that comes back. Underneath, the only
thing faked is the subprocess boundary and the startup that would have probed this host --
the pipelines, the capability gate and the parsers are all the real code.

resolve() is never called. A unit test that resolved the context would compile and run six
probes on whatever machine it happened to be on, which is minutes, needs a toolchain, and
would bind the tools to that host's answers rather than to the ones written down here.

The fakes are copied from tests/unit/test_context.py and the pipeline tests rather than
shared: hoisting them into tests/helpers.py is a refactor of its own, and one shared fake
grown to serve four suites stops resembling the boundary any of them is testing.
"""

from __future__ import annotations

import inspect
import json
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from helpers import GOLDEN_DIR
from mcp import Client
from mcp.server import MCPServer

from cpp_analysis_mcp.context import Context
from cpp_analysis_mcp.models import Analysis, CapabilityStatus
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.server import Lifespan, build_server, live
from cpp_analysis_mcp.toolchains.base import Toolchain

# ---------------------------------------------------------------- pinned expectations

# the whole surface: an assistant can only call what is named here, and a rename is a
# breaking change for every client config that already lists these
TOOL_NAMES = frozenset(
    {
        "capabilities",
        "sanitize_file",
        "sanitize_project",
        "sanitize_snippet",
        "static_check_file",
        "static_check_snippet",
        "profile_file",
        "profile_project",
        "benchmark_variants",
        "full_check_file",
    }
)

# the two tools that publish no CapabilityStatus outcome: capabilities returns the statuses
# themselves, and the race gates on nothing -- a plain compile-and-run cannot break silently
UNGATED_TOOLS = ("capabilities", "benchmark_variants", "full_check_file")

SANITIZER_TOOLS = ("sanitize_file", "sanitize_project", "sanitize_snippet")
CHECK_TOOLS = ("static_check_file", "static_check_snippet")
PROFILE_TOOLS = ("profile_file", "profile_project")

# the descriptions are the only thing an assistant reads before choosing, so the ladder has
# to be in them: what this rung costs, that it executes the code, and which cheaper rung to
# have tried first. A tool that stops saying one of these gets chosen wrongly forever.
SANITIZER_PHRASES = ("minutes", "and runs", "static_check")

# the same three things from the other end of the ladder
CHECK_PHRASES = ("seconds", "nothing is executed", "no main()", "sanitize_")

# what makes each tool the right one to reach for, beyond its rung
SHAPE_PHRASES: Mapping[str, tuple[str, ...]] = {
    "sanitize_project": ("CMake",),
    "sanitize_snippet": ("no file",),
    "static_check_snippet": ("no file",),
    # the probe is why an unavailable answer is worth anything: a version number can claim
    # a sanitizer the runtime library is missing
    "capabilities": ("probe", "unavailable"),
    # the race's contract: whole programs, the first one defines the right answer, and a
    # fast variant with a different answer must be told from a win
    "benchmark_variants": ("whole programs", "baseline", "same output", "fixed seed"),
    # the battery is the one-call road; its description must say what its report sections
    # mean, or an empty ran list reads as a clean file
    "full_check_file": ("single call", "in parallel", "unavailable", "failed_builds"),
}

# the two outcomes every pipeline can hand back whatever it was asked. Both have to be in
# the published schema or a client validates one of them as an error
UNION_MEMBERS = frozenset({"BuildFailure", "CapabilityStatus"})

# the third member varies, because the profiler answers a different question and so returns
# a different shape: a ranking with the sample count that decides whether to believe it,
# rather than a list of findings
REPORT_MEMBER: Mapping[str, str] = {name: "ProfileReport" for name in PROFILE_TOOLS}
DEFAULT_REPORT = "AnalysisReport"

# spelled through Path so the strings compare equal to str(Path(...)) on Windows too
CLANG_PATH = str(Path("/usr/bin/clang++"))
TIDY_PATH = str(Path("/usr/bin/clang-tidy"))
CLANG_WARNING_FLAGS = ("-Wthread-safety",)
DARWIN_COMPILE_EXTRAS = ("-fcolor-diagnostics",)

# what a TSan binary must be run with; written out here, never read from PINNED_RUNTIME_ENV.
# A run that loses this reports nothing and the report reads as clean code.
TSAN_OPTIONS = "history_size=7 second_deadlock_stack=1 exitcode=66 detect_deadlocks=1"

# TSAN_OPTIONS pins exitcode=66, so this is what a reporting TSan run exits with
TSAN_EXIT_CODE = 66

# the goldens each round-trip replays, and what was read out of each by eye
TSAN_RACE_GOLDEN = "tsan_data_race.darwin-clang.txt"
ASAN_GOLDEN = "asan_heap_overflow.darwin-clang.txt"
THREAD_SAFETY_GOLDEN = "thread_safety_unguarded_write.darwin-clang.txt"

RACE_CATEGORY = "data-race"
HEAP_OVERFLOW_CATEGORY = "heap-buffer-overflow"
THREAD_SAFETY_CATEGORY = "thread-safety-analysis"
THREAD_SAFETY_LINE = 21

# the capability probe's own words, as capabilities.py phrases them for a working TSan
VERIFIED_BY = "compiled and ran a planted data race; ThreadSanitizer reported it"

DENIED_REASON = "LeakSanitizer is Linux-only; it does not run on macOS arm64"
DENIED_SUGGESTION = "run the leak check on Linux, or on the roadmap's Linux-container mode"

# the snippet a sanitizer round-trip builds, and what the build calls the binary it makes
SNIPPET_SOURCE = "int main() { return 0; }\n"
TSAN_BINARY = "snippet.thread"

# the loose file sanitize_file is pointed at, and the name its sanitized build takes
FILE_STEM = "widget"
FILE_TSAN_BINARY = "widget.thread"

# what -fsyntax-only means at the front of a compile-time check's argv: nothing is linked,
# so a snippet with no main() is still checkable
SYNTAX_ONLY = "-fsyntax-only"

SUCCESS = RunResult(exit_code=0, output="")
COMPILE_FAILED = RunResult(
    exit_code=1, output="snippet.cpp:3:9: error: expected ';'\n1 error generated.\n"
)

COMPILE_STAGE = "compile"

# the phrases the server's own instructions have to carry. They are read once at initialize,
# before any tool description is, so this is where an assistant learns there is an order to
# try things in at all -- a client showing only the tool list still gets the ladder.
# Each phrase is short enough to sit inside one line of the prose, since a pin spanning a
# wrap would fail on a reflow that changed nothing an assistant reads.
INSTRUCTION_PHRASES = (
    "Cheapest rung first",
    "seconds",
    "minutes",
    "capabilities",
    "trusting an empty result",
    # the profiler is off the ladder rather than on top of it, and an assistant that reads
    # only the rungs will climb them looking for an answer to slowness that is not there
    "why is this code slow?",
    "profile_project",
    "Only measurement ranks anything",
    # the race sits beside the profiler, and the instructions are where an assistant
    # learns a speedup claim needs one behind it
    "benchmark_variants",
    "races",
    "full_check_file",
)


@pytest.fixture
def anyio_backend() -> str:
    """One event loop is enough: nothing here is loop-specific, and trio doubles the runtime."""
    return "asyncio"


def golden(name: str) -> str:
    """Read a captured sanitizer run; the pipeline meets it as a fake process's output."""
    path: Path = GOLDEN_DIR / name
    assert path.is_file(), f"missing golden {path}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the fakes


@dataclass(frozen=True, slots=True)
class Spawn:
    """One call that reached the subprocess boundary, recorded whole."""

    cmd: list[str]
    timeout_s: int
    env: dict[str, str] | None
    cwd: Path | None


@dataclass
class ScriptedRunner:
    """Answer the scripted results in call order and record every call it was handed."""

    script: list[RunResult]
    spawns: list[Spawn] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        assert len(self.spawns) < len(self.script), (
            f"spawn {len(self.spawns) + 1} was never scripted: {list(cmd)}"
        )
        self.spawns.append(
            Spawn(
                cmd=list(cmd),
                timeout_s=timeout_s,
                env=dict(env) if env is not None else None,
                cwd=cwd,
            )
        )
        return self.script[len(self.spawns) - 1]

    @property
    def only(self) -> Spawn:
        assert len(self.spawns) == 1, f"expected exactly one spawn, got {self.spawns}"
        return self.spawns[0]

    @property
    def ran(self) -> Spawn:
        """The run's spawn: always the last, since parsing spawns nothing."""
        assert len(self.spawns) > 1, f"the run never happened: {self.spawns}"
        return self.spawns[-1]


@dataclass
class RefusingRunner:
    """A runner that fails the test rather than spawn anything."""

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        raise AssertionError(f"nothing may be spawned once the gate says no, but got {list(cmd)}")


# ------------------------------------------------------- the reply a real cmake writes

REPLY_DIR = Path(".cmake") / "api" / "v1" / "reply"
STAMP = "2026-06-07T08-09-10-0000"

# two executables, so the target the caller named is a choice rather than the only option
APP_NAME = "overflow_app"
APP_ARTIFACT = "bin/overflow_app"
OTHER_NAME = "helper_tool"
OTHER_ARTIFACT = "bin/helper_tool"

TWO_EXECUTABLES = ((APP_NAME, APP_ARTIFACT), (OTHER_NAME, OTHER_ARTIFACT))


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def write_reply(build_dir: Path, executables: tuple[tuple[str, str], ...]) -> None:
    """Leave the index, codemodel and target files a real configure would have written.

    Reading these is how the build learns which targets exist and where each artifact lands,
    so the target that gets built and the binary that gets run are only right if the whole
    chain read them.
    """
    reply_dir = build_dir / REPLY_DIR
    entries = []
    for name, artifact in executables:
        target_file = f"target-{name}-{STAMP}.json"
        write_json(
            reply_dir / target_file,
            {"name": name, "type": "EXECUTABLE", "artifacts": [{"path": artifact}]},
        )
        entries.append({"name": name, "jsonFile": target_file})
    codemodel = f"codemodel-v2-{STAMP}.json"
    write_json(reply_dir / codemodel, {"configurations": [{"name": "", "targets": entries}]})
    write_json(
        reply_dir / f"index-{STAMP}.json", {"reply": {"codemodel-v2": {"jsonFile": codemodel}}}
    )


@dataclass
class ScriptedCmake(ScriptedRunner):
    """A ScriptedRunner that also writes the File API reply when the configure goes by.

    The build directory is read off the configure's own argv, because the handler makes one
    per call under the workspace and no test can know its name in advance.
    """

    executables: tuple[tuple[str, str], ...] = TWO_EXECUTABLES

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        result = super().__call__(cmd, timeout_s=timeout_s, env=env, cwd=cwd)
        if cmd[0] == "cmake" and "-B" in cmd and "--build" not in cmd:
            write_reply(Path(cmd[cmd.index("-B") + 1]), self.executables)
        return result


# ------------------------------------------------------------------------- the inputs


def a_clang() -> Toolchain:
    """A clang nobody had to find: fields written out, discovery never called."""
    return Toolchain(
        family="clang",
        compiler=Path(CLANG_PATH),
        version="Apple clang version 21.0.0 (clang-2100.1.1.101)",
        warning_flags=CLANG_WARNING_FLAGS,
    )


def a_darwin() -> Platform:
    return Platform(name="darwin", compile_extras=DARWIN_COMPILE_EXTRAS)


def a_working_status() -> CapabilityStatus:
    """A probe that caught its planted bug, with nothing this OS has to caveat."""
    return CapabilityStatus(available=True, verified_by=VERIFIED_BY)


def a_denied_status() -> CapabilityStatus:
    """What the probe wrote down for an analysis this OS cannot run at all."""
    return CapabilityStatus(available=False, reason=DENIED_REASON, suggestion=DENIED_SUGGESTION)


def every_analysis_works() -> dict[Analysis, CapabilityStatus]:
    """A host where all six probes caught their planted bugs."""
    return dict.fromkeys(Analysis, a_working_status())


def a_context(
    workspace: Path,
    runner: Any,
    capabilities: Mapping[Analysis, CapabilityStatus] | None = None,
) -> Context:
    """The value resolve() would have produced, built by hand instead of probed for."""
    return Context(
        platform=a_darwin(),
        toolchain=a_clang(),
        capabilities=every_analysis_works() if capabilities is None else capabilities,
        workspace=workspace,
        runner=runner,
    )


def a_lifespan(app: Context) -> Lifespan:
    """Hand the server a context that was written down rather than read off this machine."""

    @asynccontextmanager
    async def lifespan(server: MCPServer[Context]) -> AsyncIterator[Context]:
        yield app

    return lifespan


def a_server(app: Context) -> MCPServer[Context]:
    return build_server(lifespan=a_lifespan(app))


def result_of(structured: dict[str, Any] | None) -> dict[str, Any]:
    """Unwrap the {"result": ...} envelope the SDK puts a union return inside."""
    assert structured is not None, "the tool returned nothing structured at all"
    payload = structured["result"]
    assert isinstance(payload, dict), f"expected an object, got {payload!r}"
    return payload


# ------------------------------------------------------------------------ the surface


@pytest.mark.anyio
async def test_the_ten_tools_the_ladder_needs_are_the_ones_offered(tmp_path: Path) -> None:
    """The names are the API. A client config lists them, so a rename breaks every caller,
    and an extra one is a rung nobody documented."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    assert {tool.name for tool in listed.tools} == TOOL_NAMES


@pytest.mark.anyio
async def test_each_sanitizer_tool_says_what_it_costs_and_which_rung_comes_first(
    tmp_path: Path,
) -> None:
    """Descriptions are the only thing an assistant reads before choosing, so a sanitizer has
    to place itself: minutes rather than seconds, it executes the code, and the compile-time
    check is the rung to have tried first. Vague here and the profiler gets called before
    anyone checked whether the door was locked."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    described = {tool.name: tool.description or "" for tool in listed.tools}
    missing = [
        f"{name}: {phrase!r}"
        for name in SANITIZER_TOOLS
        for phrase in SANITIZER_PHRASES
        if phrase not in described[name]
    ]

    assert not missing, "a sanitizer tool stopped placing itself on the ladder:\n" + "\n".join(
        missing
    )


@pytest.mark.anyio
async def test_each_compile_time_tool_says_it_is_the_cheap_rung_and_where_to_escalate(
    tmp_path: Path,
) -> None:
    """The other end of the same ladder. A compile-time check that does not say it runs
    nothing gets trusted as an all-clear on a data race it structurally cannot see, and one
    that does not name the sanitizer leaves the assistant with nowhere to escalate."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    described = {tool.name: tool.description or "" for tool in listed.tools}
    missing = [
        f"{name}: {phrase!r}"
        for name in CHECK_TOOLS
        for phrase in CHECK_PHRASES
        if phrase not in described[name]
    ]

    assert not missing, "a compile-time tool stopped placing itself on the ladder:\n" + "\n".join(
        missing
    )


@pytest.mark.anyio
async def test_each_tool_says_what_it_needs_to_be_handed(tmp_path: Path) -> None:
    """Same rung, different input, and nothing in the schema says which. An assistant holding
    code it has not written to disk needs to know a snippet tool exists, and one pointed at a
    CMake tree needs to know sanitize_file is the wrong door."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    described = {tool.name: tool.description or "" for tool in listed.tools}
    missing = [
        f"{name}: {phrase!r}"
        for name, phrases in SHAPE_PHRASES.items()
        for phrase in phrases
        if phrase not in described[name]
    ]

    assert not missing, "a tool stopped saying what it needs:\n" + "\n".join(missing)


@pytest.mark.anyio
async def test_every_analysis_outcome_is_in_the_published_schema(tmp_path: Path) -> None:
    """All three outcomes are ordinary returns, not errors. A schema naming only the report
    leaves a client validating a build failure and a capability status as protocol faults,
    which is exactly the shape that gets retried instead of read."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    analysis_tools = [tool for tool in listed.tools if tool.name not in UNGATED_TOOLS]
    incomplete = [
        f"{tool.name}: {sorted((tool.output_schema or {}).get('$defs', {}))}"
        for tool in analysis_tools
        if not set((tool.output_schema or {}).get("$defs", {}))
        >= UNION_MEMBERS | {REPORT_MEMBER.get(tool.name, DEFAULT_REPORT)}
    ]

    assert len(analysis_tools) == len(TOOL_NAMES) - len(UNGATED_TOOLS)
    assert not incomplete, "an outcome is missing from a published schema:\n" + "\n".join(
        incomplete
    )


@pytest.mark.anyio
async def test_the_races_own_outcomes_are_in_its_published_schema(tmp_path: Path) -> None:
    """The race's union has no CapabilityStatus arm on purpose -- nothing gates it -- but
    its two real outcomes and the shapes inside them still have to be published, or a
    client validates a build failure as a protocol fault."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    tool = next(entry for entry in listed.tools if entry.name == "benchmark_variants")
    published = set((tool.output_schema or {}).get("$defs", {}))
    assert published >= {"BenchmarkReport", "BuildFailure", "VariantResult"}
    assert "Variant" in set(tool.input_schema.get("$defs", {}))

    # the limits live in the schema too, so a client refuses a six-variant race before
    # anything is spawned instead of learning the bounds from a failed call
    properties = tool.input_schema["properties"]
    assert properties["variants"]["minItems"] == 2
    assert properties["variants"]["maxItems"] == 5
    assert properties["repeats"]["minimum"] == 2
    assert properties["repeats"]["maximum"] == 20


@pytest.mark.anyio
async def test_the_analyses_a_sanitizer_tool_accepts_are_the_four_that_run(
    tmp_path: Path,
) -> None:
    """The compile-time checks are not sanitizers and the sanitize pipeline has no step for
    them. Left out of the schema the assistant learns that from a call that fails; in it, the
    choice is refused before anything is spawned."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    accepted = {
        tool.name: set(tool.input_schema["properties"]["analysis"]["enum"])
        for tool in listed.tools
        if tool.name in SANITIZER_TOOLS
    }

    assert accepted == {name: {"tsan", "asan", "lsan", "ubsan"} for name in SANITIZER_TOOLS}


@pytest.mark.anyio
async def test_the_analyses_a_compile_time_tool_accepts_are_the_two_that_do_not_run(
    tmp_path: Path,
) -> None:
    """The mirror of the sanitizer schema: asking this pipeline for tsan would look like a
    clean run of a detector that was never watching."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    accepted = {
        tool.name: set(tool.input_schema["properties"]["analysis"]["enum"])
        for tool in listed.tools
        if tool.name in CHECK_TOOLS
    }

    assert accepted == {name: {"thread-safety", "clang-tidy"} for name in CHECK_TOOLS}


# ------------------------------------------------------------------- what this host can do


@pytest.mark.anyio
async def test_capabilities_reports_the_statuses_the_startup_probes_produced(
    tmp_path: Path,
) -> None:
    """The one tool that spawns nothing: the probes already ran, and this reads their answers
    back. An assistant that cannot see them cannot tell an empty finding list produced by a
    working detector from one produced by a detector this machine never had."""
    denied = {**every_analysis_works(), Analysis.LSAN: a_denied_status()}
    app = a_context(tmp_path, RefusingRunner(), capabilities=denied)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool("capabilities", {})

    reported = result_of(result.structured_content)
    assert set(reported) == {analysis.value for analysis in Analysis}
    assert reported["tsan"] == {
        "available": True,
        "reason": None,
        "suggestion": None,
        "verified_by": VERIFIED_BY,
        "limitations": [],
    }
    assert reported["lsan"]["available"] is False
    assert reported["lsan"]["reason"] == DENIED_REASON
    assert reported["lsan"]["suggestion"] == DENIED_SUGGESTION


# ------------------------------------------------------------- through the real pipelines


@pytest.mark.anyio
async def test_a_snippet_is_built_run_and_parsed_without_a_file_anywhere(
    tmp_path: Path,
) -> None:
    """The whole chain over the protocol: text in, a sanitized build, a run under the options
    that build chose, and a parsed report out as JSON. The reply is a committed golden -- a
    chain that parsed a hand-written approximation would keep passing on the day it stopped
    understanding what TSan really prints."""
    runner = ScriptedRunner(
        [SUCCESS, RunResult(exit_code=TSAN_EXIT_CODE, output=golden(TSAN_RACE_GOLDEN))]
    )
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "sanitize_snippet", {"text": SNIPPET_SOURCE, "analysis": "tsan"}
        )

    report = result_of(result.structured_content)
    assert report["analysis"] == "tsan"
    assert report["exit_code"] == TSAN_EXIT_CODE
    assert report["timed_out"] is False
    assert report["verified_by"] == VERIFIED_BY
    assert [finding["category"] for finding in report["findings"]] == [RACE_CATEGORY]
    # the diagnosis a race report exists to deliver: one thread held the lock, one did not
    assert [thread["op"] for thread in report["findings"][0]["threads"]] == ["write", "write"]

    # the snippet reached a real build, under the real runtime environment
    assert Path(runner.ran.cmd[0]).name == TSAN_BINARY
    assert (runner.ran.env or {})["TSAN_OPTIONS"] == TSAN_OPTIONS


@pytest.mark.anyio
async def test_a_file_on_disk_is_built_run_and_parsed_through_the_contexts_own_runner(
    tmp_path: Path,
) -> None:
    """The same chain as the snippet, entered by path instead of by text.

    The runner is the assertion that matters here. Dropped on the way down, the pipeline
    falls back to its own default and the tool really does compile and execute whatever the
    caller pointed at -- silently, and green, because the fake simply never hears about it."""
    source = tmp_path / f"{FILE_STEM}.cpp"
    source.write_text(SNIPPET_SOURCE, encoding="utf-8")
    runner = ScriptedRunner(
        [SUCCESS, RunResult(exit_code=TSAN_EXIT_CODE, output=golden(TSAN_RACE_GOLDEN))]
    )
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "sanitize_file", {"source": str(source), "analysis": "tsan"}
        )

    report = result_of(result.structured_content)
    assert report["analysis"] == "tsan"
    assert [finding["category"] for finding in report["findings"]] == [RACE_CATEGORY]
    # both spawns went through the runner the context carries, not the pipeline's default
    assert len(runner.spawns) == 2, f"expected a compile and a run: {runner.spawns}"
    assert str(source) in runner.spawns[0].cmd
    assert Path(runner.ran.cmd[0]).name == FILE_TSAN_BINARY
    assert (runner.ran.env or {})["TSAN_OPTIONS"] == TSAN_OPTIONS


@pytest.mark.anyio
async def test_the_target_the_caller_named_is_the_one_cmake_is_told_to_build(
    tmp_path: Path,
) -> None:
    """A project with two executables cannot choose for itself, which is what makes `target`
    worth passing. Dropped between the tool and the pipeline, the caller gets a build failure
    listing the very targets they already picked from -- and a project with one executable
    quietly builds the wrong thing with no sign anything was ignored."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    runner = ScriptedCmake([SUCCESS, SUCCESS, RunResult(exit_code=1, output=golden(ASAN_GOLDEN))])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "sanitize_project",
            {"source": str(project_dir), "analysis": "asan", "target": OTHER_NAME},
        )

    report = result_of(result.structured_content)
    assert [finding["category"] for finding in report["findings"]] == [HEAP_OVERFLOW_CATEGORY]
    assert len(runner.spawns) == 3, f"expected configure, build, run: {runner.spawns}"
    assert runner.spawns[0].cmd[:2] == ["cmake", "-S"]
    # the caller's word, all the way down to the argv cmake is handed
    assert runner.spawns[1].cmd[:2] == ["cmake", "--build"]
    assert "--target" in runner.spawns[1].cmd
    assert runner.spawns[1].cmd[runner.spawns[1].cmd.index("--target") + 1] == OTHER_NAME
    # and the binary that ran is the one that target names, not the other one. Through
    # Path: cmake's File API reports the artifact with forward slashes, the run command
    # prints the platform's own separator
    assert runner.ran.cmd[0].endswith(str(Path(OTHER_ARTIFACT)))


@pytest.mark.anyio
async def test_a_snippet_is_checked_at_compile_time_without_being_linked(
    tmp_path: Path,
) -> None:
    """The cheap rung entered as text. One spawn, no link step and no run: -fsyntax-only is
    what lets a snippet with no main() be checkable at all, and the snippet still has to be
    written down somewhere for the finding to have a file to name."""
    runner = ScriptedRunner([RunResult(exit_code=0, output=golden(THREAD_SAFETY_GOLDEN))])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "static_check_snippet", {"text": SNIPPET_SOURCE, "analysis": "thread-safety"}
        )

    report = result_of(result.structured_content)
    assert report["analysis"] == "thread-safety"
    assert [finding["category"] for finding in report["findings"]] == [THREAD_SAFETY_CATEGORY]
    assert report["findings"][0]["location"]["line"] == THREAD_SAFETY_LINE
    assert len(runner.spawns) == 1, f"a compile-time check links and runs nothing: {runner.spawns}"
    assert runner.only.cmd[0] == CLANG_PATH
    assert SYNTAX_ONLY in runner.only.cmd
    # written into a build directory of this call's own, under the workspace
    written = Path(runner.only.cmd[-1])
    assert written.name == "snippet.cpp"
    assert tmp_path in written.parents
    assert written.read_text(encoding="utf-8") == SNIPPET_SOURCE


@pytest.mark.anyio
async def test_a_race_runs_whole_over_the_protocol_and_ranks_what_matched(
    tmp_path: Path,
) -> None:
    """The full race as an assistant meets it: two variants in as JSON, release builds, the
    baseline's warmup defining the answer, interleaved timed runs, and a ranking out. The
    spawn order is the methodology, so it is pinned call by call."""
    answer = RunResult(exit_code=0, output="trades=1200\n")
    runner = ScriptedRunner([SUCCESS, SUCCESS, answer, answer, answer, answer, answer, answer])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "benchmark_variants",
            {
                "variants": [
                    {"name": "baseline", "code": SNIPPET_SOURCE},
                    {"name": "flat", "code": SNIPPET_SOURCE},
                ],
                "repeats": 2,
            },
        )

    report = result_of(result.structured_content)
    assert report["baseline"] == "baseline"
    assert report["repeats"] == 2
    assert {row["name"] for row in report["variants"]} == {"baseline", "flat"}
    assert all(row["matches_baseline"] for row in report["variants"])
    assert all(row["rejected"] is None for row in report["variants"])
    assert all(row["runs"] == 2 for row in report["variants"])
    assert report["next_step"] is not None

    # both compiles at release optimization, neither instrumented
    for spawn in runner.spawns[:2]:
        assert "-O2" in spawn.cmd
        assert not [arg for arg in spawn.cmd if arg.startswith("-fsanitize")]
    # warmups first, then the rounds interleave: nobody gets the machine twice in a row
    ran = [Path(spawn.cmd[0]).stem for spawn in runner.spawns[2:]]
    assert ran == ["baseline", "flat", "baseline", "flat", "baseline", "flat"]


@pytest.mark.anyio
async def test_a_variant_that_answers_differently_is_rejected_over_the_protocol(
    tmp_path: Path,
) -> None:
    """The same-answer rule surviving serialization: the lying variant comes back rejected
    with no numbers, and not one timed run was spent on it."""
    right = RunResult(exit_code=0, output="trades=1200\n")
    wrong = RunResult(exit_code=0, output="trades=999\n")
    runner = ScriptedRunner([SUCCESS, SUCCESS, right, wrong, right, right])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "benchmark_variants",
            {
                "variants": [
                    {"name": "baseline", "code": SNIPPET_SOURCE},
                    {"name": "liar", "code": SNIPPET_SOURCE},
                ],
                "repeats": 2,
            },
        )

    report = result_of(result.structured_content)
    liar = next(row for row in report["variants"] if row["name"] == "liar")
    assert liar["rejected"] == "output differs from the baseline"
    assert liar["mean_ms"] is None
    assert [Path(spawn.cmd[0]).stem for spawn in runner.spawns[2:]] == [
        "baseline",
        "liar",  # its warmup, where the answer check caught it
        "baseline",
        "baseline",
    ]


@pytest.mark.anyio
async def test_the_batterys_own_outcomes_are_in_its_published_schema(tmp_path: Path) -> None:
    """The battery folds failure modes into report sections instead of union arms, so the
    one shape it returns has to publish whole."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_tools()

    tool = next(entry for entry in listed.tools if entry.name == "full_check_file")
    schema = tool.output_schema or {}
    # a bare dataclass return publishes itself as the schema root, nested shapes in $defs
    assert schema.get("title") == "FullCheckReport"
    assert set(schema.get("$defs", {})) >= {"Finding", "Location"}
    assert set(schema.get("properties", {})) >= {"findings", "ran", "unavailable", "failed_builds"}


@pytest.mark.anyio
async def test_the_battery_runs_whole_over_the_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One call, six analyses in parallel, one merged JSON answer. The runner dispatches
    by command content because parallel order is not promised, and the reply for TSan is
    a committed golden so the merge is proved against real sanitizer output."""
    monkeypatch.setattr(shutil, "which", lambda _name: TIDY_PATH)
    source = tmp_path / f"{FILE_STEM}.cpp"
    source.write_text(SNIPPET_SOURCE, encoding="utf-8")

    def dispatch(
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        listed = list(cmd)
        if Path(listed[0]).name == "clang-tidy" or "-fsyntax-only" in listed:
            return RunResult(exit_code=0, output="")
        if any(arg.startswith("-fsanitize=") for arg in listed):
            return RunResult(exit_code=0, output="")
        if Path(listed[0]).name.endswith(".thread"):
            return RunResult(exit_code=TSAN_EXIT_CODE, output=golden(TSAN_RACE_GOLDEN))
        return RunResult(exit_code=0, output="")

    app = a_context(tmp_path, dispatch)
    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool("full_check_file", {"source": str(source)})

    # a bare dataclass return arrives unwrapped, no {"result": ...} envelope
    report = result.structured_content
    assert report is not None
    assert sorted(report["ran"]) == sorted(
        ["thread-safety", "clang-tidy", "tsan", "asan", "lsan", "ubsan"]
    )
    assert [finding["category"] for finding in report["findings"]] == [RACE_CATEGORY]
    assert report["unavailable"] == {}
    assert report["failed_builds"] == {}
    assert report["next_step"] is not None


@pytest.mark.anyio
async def test_the_two_recipes_are_published_as_prompts(tmp_path: Path) -> None:
    """Prompts are how a client surfaces whole workflows as slash commands, and the
    argument must be declared required or a client renders the recipe with a hole in it."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        listed = await client.list_prompts()

    prompts_by_name = {prompt.name: prompt for prompt in listed.prompts}
    assert set(prompts_by_name) == {"checkup", "make-it-faster"}
    for prompt in prompts_by_name.values():
        assert prompt.description
        assert [argument.name for argument in prompt.arguments or []] == ["source"]
        assert all(argument.required for argument in prompt.arguments or [])


@pytest.mark.anyio
async def test_the_checkup_recipe_names_its_tools_and_its_rules(tmp_path: Path) -> None:
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        result = await client.get_prompt("checkup", {"source": "book.cpp"})

    text = result.messages[0].content.text  # type: ignore[union-attr]
    assert "book.cpp" in text
    steps = text[text.index("Steps:") :]
    assert steps.index("capabilities") < steps.index("full_check_file")
    assert "never delete or weaken a check" in text


@pytest.mark.anyio
async def test_the_speed_recipe_orders_profile_race_check(tmp_path: Path) -> None:
    """The order is the method: measure, then rewrite, then race, then prove correctness,
    and a recipe that stopped saying so would let a speedup claim skip its race."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        result = await client.get_prompt("make-it-faster", {"source": "book.cpp"})

    text = result.messages[0].content.text  # type: ignore[union-attr]
    assert "book.cpp" in text
    steps = text[text.index("Steps:") :]
    assert (
        steps.index("profile_file")
        < steps.index("benchmark_variants")
        < steps.index("full_check_file")
    )
    assert "never claim a speedup without a race" in text


@pytest.mark.anyio
async def test_the_servers_instructions_teach_the_ladder_before_any_tool_is_read(
    tmp_path: Path,
) -> None:
    """Instructions arrive with the initialize response, ahead of every tool description, and
    some clients surface them as the whole of what this server is. A tool description can only
    argue for its own rung; the order to try rungs in, and the reason an empty result is worth
    checking capabilities over, exist nowhere else."""
    async with Client(a_server(a_context(tmp_path, RefusingRunner())), raise_exceptions=True) as (
        client
    ):
        instructions = client.instructions or ""

    missing = [phrase for phrase in INSTRUCTION_PHRASES if phrase not in instructions]

    assert not missing, f"the instructions stopped teaching the ladder: {missing}"


@pytest.mark.anyio
async def test_each_call_builds_somewhere_of_its_own_inside_the_workspace(
    tmp_path: Path,
) -> None:
    """Two calls sharing a build directory overwrite each other's binaries between the compile
    and the run, and each report then describes whatever the other one compiled."""
    runner = ScriptedRunner([SUCCESS, RunResult(exit_code=0, output=""), SUCCESS, SUCCESS])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        await client.call_tool("sanitize_snippet", {"text": SNIPPET_SOURCE, "analysis": "tsan"})
        await client.call_tool("sanitize_snippet", {"text": SNIPPET_SOURCE, "analysis": "tsan"})

    built_in = [spawn.cmd[-1] for spawn in runner.spawns[::2]]
    assert len(set(built_in)) == 2, f"both calls built in the same place: {built_in}"
    assert all(tmp_path in Path(path).parents for path in built_in)


@pytest.mark.anyio
async def test_asking_for_no_checks_leaves_the_projects_own_config_in_charge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --checks the caller never asked for overrides whatever .clang-tidy the project
    committed, so a repository with a curated check list silently gets a different one. The
    absence has to survive all the way to argv, not be filled in with a default on the way."""
    monkeypatch.setattr(shutil, "which", lambda _name: TIDY_PATH)
    source = tmp_path / "widget.cpp"
    source.write_text(SNIPPET_SOURCE, encoding="utf-8")
    (tmp_path / ".clang-tidy").write_text("Checks: 'readability-*'\n", encoding="utf-8")
    runner = ScriptedRunner([SUCCESS])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        await client.call_tool(
            "static_check_file", {"source": str(source), "analysis": "clang-tidy"}
        )

    assert runner.only.cmd[0] == TIDY_PATH
    assert not [arg for arg in runner.only.cmd if arg.startswith("--checks")]


@pytest.mark.anyio
async def test_a_file_with_no_clang_tidy_anywhere_still_gets_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clang-tidy enables nothing by itself: with no --checks and no .clang-tidy above the
    file it exits 1 printing usage text, which parses to no findings and is indistinguishable
    from clean code. A project that committed no configuration gets a default instead."""
    monkeypatch.setattr(shutil, "which", lambda _name: TIDY_PATH)
    source = tmp_path / "widget.cpp"
    source.write_text(SNIPPET_SOURCE, encoding="utf-8")
    runner = ScriptedRunner([SUCCESS])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        await client.call_tool(
            "static_check_file", {"source": str(source), "analysis": "clang-tidy"}
        )

    assert [arg for arg in runner.only.cmd if arg.startswith("--checks")]


@pytest.mark.anyio
async def test_the_checks_the_caller_did_ask_for_reach_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same promise: passed through means passed through, so a caller
    hunting one check gets that check rather than the project's whole list."""
    monkeypatch.setattr(shutil, "which", lambda _name: TIDY_PATH)
    source = tmp_path / "widget.cpp"
    source.write_text(SNIPPET_SOURCE, encoding="utf-8")
    runner = ScriptedRunner([SUCCESS])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        await client.call_tool(
            "static_check_file",
            {
                "source": str(source),
                "analysis": "clang-tidy",
                "checks": "-*,modernize-use-nullptr",
            },
        )

    assert "--checks=-*,modernize-use-nullptr" in runner.only.cmd


@pytest.mark.anyio
async def test_an_analysis_this_machine_cannot_run_comes_back_as_its_status(
    tmp_path: Path,
) -> None:
    """A gate that reported through an empty findings list would read exactly like clean code,
    which is the false all-clear this project exists to avoid. The runner refuses to spawn:
    nothing may be built once the answer is known."""
    denied = {**every_analysis_works(), Analysis.LSAN: a_denied_status()}
    app = a_context(tmp_path, RefusingRunner(), capabilities=denied)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "sanitize_snippet", {"text": SNIPPET_SOURCE, "analysis": "lsan"}
        )

    status = result_of(result.structured_content)
    assert status["available"] is False
    assert status["reason"] == DENIED_REASON
    assert status["suggestion"] == DENIED_SUGGESTION
    # not a report with nothing in it
    assert "findings" not in status


@pytest.mark.anyio
async def test_code_that_does_not_compile_comes_back_as_the_build_failure(
    tmp_path: Path,
) -> None:
    """User code that does not compile is an expected outcome of asking to build it, not a
    protocol error. Raised instead of returned, the assistant sees a tool malfunction and
    retries rather than reading the compiler diagnostic it was handed."""
    runner = ScriptedRunner([COMPILE_FAILED])
    app = a_context(tmp_path, runner)

    async with Client(a_server(app), raise_exceptions=True) as client:
        result = await client.call_tool(
            "sanitize_snippet", {"text": SNIPPET_SOURCE, "analysis": "asan"}
        )

    assert result.is_error is False
    failure = result_of(result.structured_content)
    assert failure["stage"] == COMPILE_STAGE
    assert "expected ';'" in failure["output"]


# ------------------------------------------------------------------------- what ships


def test_a_server_built_with_no_arguments_resolves_this_host() -> None:
    """The fake lifespan every test above injects must not be what production gets. No test
    may exercise the live one behaviorally -- it compiles and runs six probes on whatever
    machine the suite is on -- so the promise is pinned on the signature, where a default
    quietly left pointing at a stub is still visible."""
    default = inspect.signature(build_server).parameters["lifespan"].default

    assert default is live
