"""The MCP tool surface: six tools, their schemas, and one line each into a pipeline.

Protocol only. Every handler reads the resolved context off the request, hands it to one
pipeline call and returns what came back -- there is no control flow in this file at all, and
a test walks it with ast to keep it that way (rule 2). Everything that needed a decision at
startup was decided in context.py, and everything a call needs to decide is decided in the
pipeline it delegates to; a handler that grew an `if` would be workflow logic in the layer
that has no business holding any.

The descriptions below are the product, not documentation of it. MCP hands the assistant a
menu of names and descriptions and nothing else, so these are the only information it has
when choosing what to run: they say what each rung of the escalation ladder costs, what it
can see that the cheaper one cannot, and what it needs to be handed. A vague description
produces wrong tool choices no matter how good the pipeline underneath is.

Nothing is serialized by hand. The union return annotations are the schema: the SDK reads
them off the signatures, publishes the JSON shape of every outcome a pipeline can produce,
and converts the dataclasses on the way out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Literal

from anyio import to_thread
from mcp.server import MCPServer

# our own Context is the app state these handlers read, and the SDK's is the request handle
# they read it off; one of the two names has to move
from mcp.server.mcpserver import Context as ServerContext

from cpp_analysis_mcp import context
from cpp_analysis_mcp.context import Context
from cpp_analysis_mcp.models import Analysis, AnalysisReport, BuildFailure, CapabilityStatus
from cpp_analysis_mcp.pipelines import sanitize, static_check

SERVER_NAME = "cpp-analysis"

# which analyses each pipeline can actually perform, as the schema rather than as a check.
# Asking the sanitize pipeline for clang-tidy is refused by the client before anything is
# spawned, instead of arriving here as an argument no handler is allowed to branch on.
Sanitizer = Literal["tsan", "asan", "lsan", "ubsan"]
CompileTimeCheck = Literal["thread-safety", "clang-tidy"]

Lifespan = Callable[[MCPServer[Context]], AbstractAsyncContextManager[Context]]

INSTRUCTIONS = """\
C++ analysis: compile-time checks, runtime sanitizers, and what this host can actually run.

Cheapest rung first. Each rung sees bugs the one above it structurally cannot, and pays for
them in time:

  static_check_file, static_check_snippet   seconds   nothing is linked, nothing runs
  sanitize_file, sanitize_project,          minutes   the code is built and executed
  sanitize_snippet

Reaching for a sanitizer first is like calling forensics before checking whether the door was
locked. Escalating is the point, though: a clean compile-time result is not an all-clear,
because a data race, a leak and a use-after-free exist only while the program is running.

capabilities says which analyses this machine proved it can do, and is worth reading before
trusting an empty result from any of them.
"""

CAPABILITIES_DOC = """\
Report which analyses work on this machine, and why any of them do not.

Spawns nothing: every answer was settled at server startup by a probe that compiled and ran a
program with a planted bug. So an available analysis is one that demonstrably caught its bug
on this host, not one a version number claimed -- a compiler can report a version supporting
ThreadSanitizer while the runtime library is missing.

An analysis reported unavailable returns that status instead of findings when you call it,
and an available one can still carry limitations: ThreadSanitizer runs on macOS but its
deadlock detector does not, so deadlock findings there require Linux.

Read this when a result surprises you, particularly an empty one.
"""

SANITIZE_FILE_DOC = """\
Compiles one C++ file under a sanitizer and runs it, then reports what the sanitizer saw.

Second rung of the escalation ladder, costing minutes rather than seconds: the file is
compiled, linked and executed. Try static_check_file first -- it is the cheap rung and often
settles the question. Come here when it does not, because these bugs exist only while the
program is running and no compile-time check can see them:

  tsan   data races, lock-order inversions
  asan   heap and stack overflows, use-after-free
  lsan   memory still allocated at exit
  ubsan  signed overflow, bad shifts, null dereference, the rest of undefined behaviour

`source` is a path to a file with a main(): the binary is run with no arguments, so a bug is
only found if the code that actually executed reached it. What comes back is facts -- which
threads raced, which locks each of them held -- never a suggested fix.
"""

SANITIZE_PROJECT_DOC = """\
Builds a CMake project under a sanitizer and runs its executable, then reports what the
sanitizer saw.

Second rung of the escalation ladder, costing minutes rather than seconds: the project is
configured, built and executed. Try static_check_file on the file you suspect first -- it is
the cheap rung. Come here when it does not settle the question, because a data race, a leak
and a use-after-free exist only while the program is running.

`source` is a directory holding a CMakeLists.txt; this is the tool for a real project rather
than a loose file. With no `target`, a project containing exactly one executable builds it,
and anything else comes back as a build failure naming the targets there are. The binary is
run with no arguments, so a bug is only found if the default path reaches it.
"""

SANITIZE_SNIPPET_DOC = """\
Compiles a piece of C++ under a sanitizer and runs it, then reports what the sanitizer saw.

Takes the code as text -- no file on disk, no project -- which makes this the tool for code
you are holding rather than code the repository already has. Second rung of the escalation
ladder, costing minutes rather than seconds, so try static_check_snippet first: it is the
cheap rung and it does not even need the code to be runnable. Come here when it does not
settle the question, because a data race, a leak and a use-after-free exist only while the
program is running.

`text` has to be a complete program with a main(): it is compiled, linked and executed. The
file written for it stays behind on purpose, since every frame in the report names it.
"""

STATIC_CHECK_FILE_DOC = """\
Checks one C++ file at compile time and reports what the compiler or clang-tidy said about it.

The cheapest rung of the escalation ladder: seconds, and nothing is executed. Nothing is
linked either, so no main() is needed and one file out of a larger program is still checkable
on its own.

  thread-safety  clang's -Wthread-safety, which reads the lock annotations in the source
                 (GUARDED_BY, REQUIRES) and catches lock bugs without running anything
  clang-tidy     the broader lint: bug-prone patterns, modernization, readability

Start here. A clean result is not an all-clear -- neither check can see a bug that only
happens at runtime -- so escalate to sanitize_file when this comes back inconclusive.

`source` is a path. `checks` is clang-tidy's --checks argument; leave it unset and the
project's own .clang-tidy file decides, which is usually what you want.
"""

STATIC_CHECK_SNIPPET_DOC = """\
Checks a piece of C++ at compile time and reports what the compiler or clang-tidy said.

Takes the code as text -- no file on disk -- which makes this the tool for code you are
holding rather than code the repository already has. The cheapest rung of the escalation
ladder: seconds, and nothing is executed. Nothing is linked either, so no main() is needed:
a bare function or class is a valid thing to hand this.

  thread-safety  clang's -Wthread-safety, which reads the lock annotations in the source
                 (GUARDED_BY, REQUIRES) and catches lock bugs without running anything
  clang-tidy     the broader lint: bug-prone patterns, modernization, readability

Start here. A clean result is not an all-clear -- neither check can see a bug that only
happens at runtime -- so escalate to sanitize_snippet when this comes back inconclusive.

`checks` is clang-tidy's --checks argument; leave it unset and the project's own .clang-tidy
file decides. The file written for the snippet stays behind, since every finding names it.
"""


@asynccontextmanager
async def live(server: MCPServer[Context]) -> AsyncIterator[Context]:
    """Read this host once at startup and hand the result to every request the process serves.

    Off the event loop, because a cold cache means compiling and running six probes -- minutes
    on a first start -- and resolve() is ordinary blocking code. Run inline it would stall the
    loop for all of that, and a host waiting on the initialize response would give up on a
    server that is working exactly as intended.
    """
    yield await to_thread.run_sync(context.resolve)


def capabilities(ctx: ServerContext[Context]) -> Mapping[Analysis, CapabilityStatus]:
    """Read back what the startup probes wrote down; nothing is spawned to answer this."""
    return ctx.request_context.lifespan_context.capabilities


def sanitize_file(
    source: str,
    analysis: Sanitizer,
    ctx: ServerContext[Context],
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Delegate to the sanitize pipeline, on the engine this analysis was resolved onto."""
    app = ctx.request_context.lifespan_context
    engine = app.engines[Analysis(analysis)]
    return sanitize.analyze_file(
        Path(source),
        Analysis(analysis),
        toolchain=engine.toolchain,
        platform=engine.platform,
        capabilities=app.capabilities,
        build_dir=context.scratch(app.workspace),
        runner=engine.runner,
    )


def sanitize_project(
    source: str,
    analysis: Sanitizer,
    ctx: ServerContext[Context],
    target: str | None = None,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Delegate to the sanitize pipeline, on the engine this analysis was resolved onto."""
    app = ctx.request_context.lifespan_context
    engine = app.engines[Analysis(analysis)]
    return sanitize.analyze_project(
        Path(source),
        Analysis(analysis),
        toolchain=engine.toolchain,
        platform=engine.platform,
        capabilities=app.capabilities,
        build_dir=context.scratch(app.workspace),
        target=target,
        runner=engine.runner,
    )


def sanitize_snippet(
    text: str,
    analysis: Sanitizer,
    ctx: ServerContext[Context],
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Delegate to the sanitize pipeline, on the engine this analysis was resolved onto."""
    app = ctx.request_context.lifespan_context
    engine = app.engines[Analysis(analysis)]
    return sanitize.analyze_snippet(
        text,
        Analysis(analysis),
        toolchain=engine.toolchain,
        platform=engine.platform,
        capabilities=app.capabilities,
        build_dir=context.scratch(app.workspace),
        runner=engine.runner,
    )


def static_check_file(
    source: str,
    analysis: CompileTimeCheck,
    ctx: ServerContext[Context],
    checks: str | None = None,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Delegate to the static_check pipeline; nothing is built, so there is no build directory.

    `checks` travels as it arrived, None included: a default filled in here would override
    whatever .clang-tidy the project committed.
    """
    app = ctx.request_context.lifespan_context
    engine = app.engines[Analysis(analysis)]
    return static_check.check_file(
        Path(source),
        Analysis(analysis),
        toolchain=engine.toolchain,
        platform=engine.platform,
        capabilities=app.capabilities,
        checks=checks,
        runner=engine.runner,
    )


def static_check_snippet(
    text: str,
    analysis: CompileTimeCheck,
    ctx: ServerContext[Context],
    checks: str | None = None,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Delegate to the static_check pipeline; the snippet needs somewhere to be written down.

    `checks` travels as it arrived, None included: a default filled in here would override
    whatever .clang-tidy the project committed.
    """
    app = ctx.request_context.lifespan_context
    engine = app.engines[Analysis(analysis)]
    return static_check.check_snippet(
        text,
        Analysis(analysis),
        toolchain=engine.toolchain,
        platform=engine.platform,
        capabilities=app.capabilities,
        build_dir=context.scratch(app.workspace),
        checks=checks,
        runner=engine.runner,
    )


def build_server(*, lifespan: Lifespan = live) -> MCPServer[Context]:
    """Register the six tools against one server; `lifespan` is the seam a test starts through.

    The default is what ships. A test injects a context it wrote down instead, because the
    live one probes the machine the suite happens to be running on.
    """
    server: MCPServer[Context] = MCPServer(
        SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan
    )
    server.add_tool(capabilities, description=CAPABILITIES_DOC)
    server.add_tool(sanitize_file, description=SANITIZE_FILE_DOC)
    server.add_tool(sanitize_project, description=SANITIZE_PROJECT_DOC)
    server.add_tool(sanitize_snippet, description=SANITIZE_SNIPPET_DOC)
    server.add_tool(static_check_file, description=STATIC_CHECK_FILE_DOC)
    server.add_tool(static_check_snippet, description=STATIC_CHECK_SNIPPET_DOC)
    return server


def main() -> None:
    """The console entry point: serve over stdio, which is how an MCP host launches this."""
    build_server().run("stdio")
