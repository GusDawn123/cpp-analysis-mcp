"""Everything server.py would otherwise have to decide at startup, decided one layer down.
server.py holds no control flow (rule 2), so each startup decision lives here. resolve() is
the composition root, the one sanctioned caller of platforms.detect() outside tests (rule 3).
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from cpp_analysis_mcp import capabilities, container, platforms, process, wsl
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

# names the compilers that were looked for and both ways the search comes back empty --
# discovery also drops a compiler that answers `which` but exits nonzero on --version,
# which is macOS without Command Line Tools
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
    A bridged analysis -- TSan on a Windows with a WSL distro -- runs on the bridge's, and
    deciding that at startup keeps the pipelines free of any idea of a second machine.
    """

    toolchain: Toolchain
    platform: Platform
    runner: Runner


@dataclass(frozen=True, slots=True)
class Context:
    """What one server process resolved once at startup and hands down to every call.
    Toolchain rides here because OS and compiler vary independently; Runner because it is
    the one testability seam allowed. No default timeout -- each pipeline knows its costs.
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
    """Choose the build compiler: clang when the machine has one (docs/architecture.md says
    why), never a requirement. Discovery order must not decide this -- relying on
    COMPILER_CANDIDATES' order would quietly invert. Callers pass a non-empty sequence.
    """
    return next((chain for chain in toolchains if chain.family == CLANG_FAMILY), toolchains[0])


def resolve(
    *,
    workspace: Path | None = None,
    cache_dir: Path | None = CACHE_DIR,
    runner: Runner = process.run,
) -> Context:
    """Read this host once and bind everything a request will need into one immutable value.
    cache_dir=None means "remember nothing"; the default pays the probe cost once per machine.
    The workspace settles first: it is the only question here that costs no spawns to answer.
    """
    platform = platforms.detect()
    workspace = _workspace(workspace)
    toolchains = capabilities.discover_toolchains(runner=runner)
    # a machine with no compiler at all can still serve: the toolbox image carries one,
    # and only when Docker cannot stand in either does startup refuse (ADR-0004's floor)
    if not toolchains:
        return _floor(platform, workspace, cache_dir, runner)

    toolchain = prefer(toolchains)
    statuses = capabilities.probe_all(toolchain, platform, cache_dir=cache_dir, runner=runner)

    # On Windows a WSL distro that answers gets probed too (same planted bugs, its own cache
    # fingerprint); each analysis whose bridged probe passed is rerouted into the distro.
    # No analysis is ever routed where its own probe didn't pass.
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

    # the container floor (ADR-0004): whatever is still unavailable gets one more chance
    # inside the toolbox image. A machine already running everything asks Docker nothing.
    needy = [
        analysis
        for analysis in Analysis
        if not statuses[analysis].available and analysis in container.CARRIED
    ]
    if needy:
        settled = container.discover(workspace=workspace, runner=runner)
        if isinstance(settled, container.Bridge):
            contained = capabilities.probe_all(
                settled.toolchain, settled.platform, cache_dir=cache_dir, runner=settled.runner
            )
            for analysis in needy:
                if contained[analysis].available:
                    statuses[analysis] = contained[analysis]
                    engines[analysis] = Engine(
                        toolchain=settled.toolchain,
                        platform=settled.platform,
                        runner=settled.runner,
                    )
        else:
            for analysis in needy:
                statuses[analysis] = _offered_docker(statuses[analysis], settled)

    return Context(
        platform=platform,
        toolchain=toolchain,
        capabilities=statuses,
        workspace=workspace,
        runner=runner,
        engines=engines,
    )


def _floor(platform: Platform, workspace: Path, cache_dir: Path | None, runner: Runner) -> Context:
    """Serve a machine with no compiler through the container engine, or refuse plainly."""
    settled = container.discover(workspace=workspace, runner=runner)
    if isinstance(settled, container.Absence):
        raise RuntimeError(
            f"{NO_COMPILER} Or skip installing compilers: with Docker, every analysis "
            f"can run inside the toolbox image -- {settled.reason}; {settled.suggestion}."
        )
    statuses = capabilities.probe_all(
        settled.toolchain, settled.platform, cache_dir=cache_dir, runner=settled.runner
    )
    engine = Engine(toolchain=settled.toolchain, platform=settled.platform, runner=settled.runner)
    return Context(
        platform=platform,
        toolchain=settled.toolchain,
        capabilities=statuses,
        workspace=workspace,
        runner=runner,
        engines={analysis: engine for analysis in settled.analyses if statuses[analysis].available},
    )


def _offered_docker(status: CapabilityStatus, absence: container.Absence) -> CapabilityStatus:
    """Add the container way out to an unavailable status, keeping its own advice first."""
    hint = f"or run it in the container engine: {absence.reason} -- {absence.suggestion}"
    combined = f"{status.suggestion}; {hint}" if status.suggestion else hint
    return replace(status, suggestion=combined)


def scratch(workspace: Path) -> Path:
    """A fresh empty directory under the workspace for one call to build in. Two requests
    sharing a build directory would overwrite each other's binaries between compile and
    run, each report then describing whatever the other compiled last.
    """
    return Path(tempfile.mkdtemp(dir=workspace))


def _workspace(workspace: Path | None) -> Path:
    """Use the directory the caller named, creating it when absent, or make a temporary one.
    Absolute always -- a relative path means "wherever this process happens to be". exist_ok
    because a configured workspace survives restarts; what it holds is the operator's.
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
