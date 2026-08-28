"""Everything server.py would otherwise have to decide at startup, decided one layer down.

server.py holds no control flow (rule 2); startup is nothing but decisions -- OS, compilers,
what this machine can do, where work happens -- so each lives here instead. resolve() is
the composition root, the one sanctioned caller of platforms.detect() outside tests (rule
3): a pipeline resolving its own context would be looking the platform up globally.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from cpp_analysis_mcp import capabilities, platforms, process, wsl
from cpp_analysis_mcp.platforms import windows
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import Runner
from cpp_analysis_mcp.store.models import Analysis, CapabilityStatus
from cpp_analysis_mcp.toolchains.base import Toolchain

CLANG_FAMILY = "clang"

TEMP_PREFIX = "cpp-analysis-"

# where the probe results live between runs, so only the first server start on a machine
# pays for compiling and running six smoke tests
CACHE_DIR = Path.home() / ".cache" / "cpp-analysis-mcp"

# names the compilers that were looked for and both ways the search comes back empty: a bare
# "no toolchain found" leaves the reader guessing what to install, and "not on PATH" alone
# would be a lie next to a `which clang++` that answers -- discovery also drops a compiler
# that is there but exits nonzero on --version, which is macOS without Command Line Tools
NO_COMPILER = (
    "no usable C++ compiler found (looked for "
    f"{' and '.join(capabilities.COMPILER_CANDIDATES)}): neither is on PATH, or one is there "
    "but could not report a version when asked. Install or repair one: xcode-select --install "
    "or brew install llvm on macOS, sudo apt install clang or sudo apt install g++ on Debian "
    "and Ubuntu, winget install LLVM.LLVM on Windows (then add its bin directory to PATH; "
    "the installer offers a checkbox for it)."
)


@dataclass(frozen=True, slots=True)
class Engine:
    """What one analysis actually runs on: a compiler, an OS's data, and a way to spawn.

    Almost every analysis runs on the host's own engine. A bridged one -- TSan on a
    Windows with a WSL distro -- runs on the bridge's, and deciding that here at startup
    is what keeps the pipelines free of any idea that a second machine exists.
    """

    toolchain: Toolchain
    platform: Platform
    runner: Runner


@dataclass(frozen=True, slots=True)
class Context:
    """What one server process resolved once at startup and hands down to every call.

    Differs from the Context docs/architecture.md sketches by two additions and one drop.
    Toolchain is here because the OS and the compiler vary independently and every pipeline
    needs both. Runner is here because it is the one testability seam this project allows: a
    fake replaces the subprocess boundary and nothing else. default_timeout_s is gone --
    each pipeline knows its own steps' cost, and one number here would be too tight for a
    sanitized run's minutes and meaningless for a syntax check's seconds.
    """

    platform: Platform  # the OS does not change mid-run
    toolchain: Toolchain
    capabilities: Mapping[Analysis, CapabilityStatus]
    # every tool call builds and runs in here; the startup probes are the exception, and
    # they compile and run in their own scratch directories under the system temp dir
    workspace: Path
    runner: Runner
    # which engine each analysis runs on. Constructed sparse: entries only where an
    # analysis runs somewhere other than the host, and __post_init__ fills the rest in,
    # so "no bridge" is the default shape rather than a special case
    engines: Mapping[Analysis, Engine] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen= stops rebinding but not editing inside the mapping, and this one is shared
        # by every request the process serves: an edit would change what a later call is told
        # this machine can do, with nothing probed to back it
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
        native = Engine(toolchain=self.toolchain, platform=self.platform, runner=self.runner)
        object.__setattr__(
            self,
            "engines",
            MappingProxyType(
                {analysis: self.engines.get(analysis, native) for analysis in Analysis}
            ),
        )


def prefer(toolchains: Sequence[Toolchain]) -> Toolchain:
    """Choose which discovered compiler to build with: clang when the machine has one.

    Clang is the preference for the reasons docs/architecture.md gives under "Why clang is
    still the default"; never a requirement, though -- a machine with only gcc gets gcc, and
    the probes report what that costs. Discovery order must not decide this:
    COMPILER_CANDIDATES happens to list clang++ before g++ today, and relying on that would
    quietly invert if the tuple's order ever changed. Callers guarantee a non-empty sequence.
    """
    return next((chain for chain in toolchains if chain.family == CLANG_FAMILY), toolchains[0])


def resolve(
    *,
    workspace: Path | None = None,
    cache_dir: Path | None = CACHE_DIR,
    runner: Runner = process.run,
) -> Context:
    """Read this host once and bind everything a request will need into one immutable value.

    cache_dir defaults to CACHE_DIR, not None, so a live server pays the probe cost once per
    machine rather than once per start; explicit None means "remember nothing", which is
    what probe_all understands and what a caller barred from writing a home directory needs.

    The workspace is settled first because it is the only question here that costs nothing
    to answer -- everything after it spawns two version queries, then six compiles and six
    runs, and a misconfigured path that can never hold a build should not surface at the end.
    """
    platform = platforms.detect()
    workspace = _workspace(workspace)
    toolchains = capabilities.discover_toolchains(runner=runner)
    # every analysis in the set needs something to compile with, so there is no reduced
    # service to start up with here
    if not toolchains:
        raise RuntimeError(NO_COMPILER)

    toolchain = prefer(toolchains)
    statuses = capabilities.probe_all(toolchain, platform, cache_dir=cache_dir, runner=runner)

    # On Windows, a WSL distro that answers for clang gets probed too (same planted bugs,
    # its own cache fingerprint), and each analysis whose bridged probe passed is rerouted:
    # bridged status replaces the native denial, and the engine points into the distro. A
    # failed bridged probe changes nothing -- no analysis is ever routed where its own
    # probe didn't pass.
    engines: dict[Analysis, Engine] = {}
    bridge = wsl.discover(runner=runner) if platform.name == windows.NAME else None
    if bridge is not None:
        bridged = capabilities.probe_all(
            bridge.toolchain, bridge.platform, cache_dir=cache_dir, runner=bridge.runner
        )
        for analysis in bridge.analyses:
            if bridged[analysis].available:
                statuses[analysis] = bridged[analysis]
                engines[analysis] = Engine(
                    toolchain=bridge.toolchain, platform=bridge.platform, runner=bridge.runner
                )

    return Context(
        platform=platform,
        toolchain=toolchain,
        capabilities=statuses,
        workspace=workspace,
        runner=runner,
        engines=engines,
    )


def scratch(workspace: Path) -> Path:
    """Return a fresh empty directory under the workspace for one call to build in.

    Every tool call gets its own. Two requests sharing a build directory would overwrite each
    other's binaries between the compile and the run, and each report would then describe
    whatever the other one compiled last.
    """
    return Path(tempfile.mkdtemp(dir=workspace))


def _workspace(workspace: Path | None) -> Path:
    """Use the directory the caller named, creating it when absent, or make a temporary one.

    Absolute always. A relative path means "wherever this process happens to be", and the
    host that launches an MCP server owns that: one chdir later, scratch() either fails or
    builds somewhere nobody configured.

    exist_ok, because a configured workspace is meant to survive restarts: whatever a
    previous run left in it is the operator's, not ours to clear.
    """
    if workspace is None:
        return Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    settled = workspace.resolve()
    try:
        settled.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        # exist_ok covers a directory that is already there, not a file sitting where one
        # should be; the FileExistsError that raises names no setting and suggests nothing
        raise RuntimeError(f"workspace {settled} cannot be used as a directory: {error}") from error
    return settled
