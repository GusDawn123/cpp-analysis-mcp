"""The rented Linux: run analyses inside a Docker container carrying the whole toolchain,
for machines that have none of it installed. Twin of wsl.py -- discover() hands back a
bridge whose runner respells paths both ways, so nothing above ever learns of a container.
"""

from __future__ import annotations

import re
import shutil
import string
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cpp_analysis_mcp import process
from cpp_analysis_mcp.capabilities import first_line
from cpp_analysis_mcp.platforms import linux
from cpp_analysis_mcp.platforms.base import Denial, Platform
from cpp_analysis_mcp.process import Runner, RunResult
from cpp_analysis_mcp.store.models import Analysis
from cpp_analysis_mcp.toolchains import clang
from cpp_analysis_mcp.toolchains.base import Toolchain

NAME = "container"

DOCKER = "docker"

# the toolbox image, pinned by tag; its digest joins the capability-cache fingerprint,
# so a rebuilt image retires every cached answer the old one produced
IMAGE = "ghcr.io/gusdawn123/cpp-analysis-toolbox:0.1"

# the compiler as the container's own PATH resolves it
COMPILER = "clang++"

DAEMON_TIMEOUT_S = 30
# a cold container start pays image extraction inside this answer
ASK_TIMEOUT_S = 120

# base image ships no make; the documented toolbox installs ninja
CMAKE_EXTRAS = ("-G", "Ninja")

# where the host appears inside the container. Distinctive prefixes on purpose: the
# reverse translation rewrites them in tool output, and "/usr" would match everything.
INSIDE_HOST = "/mnt/host"
INSIDE_WS = "/mnt/ws"
INSIDE_TMP = "/mnt/tmp"

DIGEST_FACT = "image"

# kernel views mean "wherever this command runs" -- translating them through a mounted
# host root would read the wrong machine's kernel
NEVER_TRANSLATED = ("/proc/", "/sys/", "/dev/")

# everything the toolbox carries; perf stays out because a Docker VM has neither the
# host kernel's counters nor a perf built for it
CARRIED = frozenset(Analysis) - {Analysis.PROFILE}

DENIED = {
    Analysis.PROFILE: Denial(
        reason="a container cannot profile: perf needs the host kernel's own counters",
        suggestion="profile natively on Linux, or through WSL on Windows",
    )
}

NO_DOCKER = "Docker is not installed (no `docker` on PATH)"
NO_DAEMON = "Docker is installed but its daemon is not answering"

INSTALL_DOCKER = (
    "install Docker Desktop (Windows/macOS) or Docker Engine (Linux), then restart this server"
)
START_DOCKER = (
    "start Docker Desktop (or `systemctl start docker` on Linux), then restart this server"
)


@dataclass(frozen=True, slots=True)
class Mount:
    """One host directory the container sees: how to say it to docker, match it, respell it."""

    host: str  # the -v spelling, the host's own
    key: str  # the same path normalized for prefix matching (posix slashes, / terminated)
    inside: str  # the container path, / terminated
    writable: bool


@dataclass(frozen=True, slots=True)
class Bridge:
    """A Linux rented from Docker, bound to the analyses it carries. Same shape as
    wsl.Bridge on purpose, and deliberately not shared: two self-contained engines are
    easier to read than one abstraction serving both.
    """

    analyses: frozenset[Analysis]
    toolchain: Toolchain
    platform: Platform
    runner: Runner


@dataclass(frozen=True, slots=True)
class Absence:
    """Why the container engine is not available here, and the command that changes that."""

    reason: str
    suggestion: str


def discover(*, workspace: Path, runner: Runner = process.run) -> Bridge | Absence:
    """Find a Docker that answers and the toolbox image inside it, or say what is missing.
    Never pulls and never builds -- both cost minutes startup does not have, so an absent
    image comes back as an Absence carrying the one-time command instead.
    """
    docker = shutil.which(DOCKER)
    if docker is None:
        return Absence(reason=NO_DOCKER, suggestion=INSTALL_DOCKER)

    version = runner(
        [docker, "version", "--format", "{{.Server.Version}}"], timeout_s=DAEMON_TIMEOUT_S
    )
    if version.exit_code != 0:
        return Absence(reason=NO_DAEMON, suggestion=START_DOCKER)

    inspected = runner(
        [docker, "image", "inspect", "--format", "{{.Id}}", IMAGE], timeout_s=DAEMON_TIMEOUT_S
    )
    if inspected.exit_code != 0:
        return Absence(
            reason=f"the toolbox image {IMAGE} is not present in Docker",
            suggestion=(
                f"one-time: `docker pull {IMAGE}` -- or build it from a checkout: "
                f"`docker build -t {IMAGE} {dockerfile_dir()}`; then restart this server"
            ),
        )

    mounts = mount_table(workspace)
    run = contained(runner, docker, mounts)
    answer = run([COMPILER, "--version"], timeout_s=ASK_TIMEOUT_S)
    version_line = first_line(answer.output)
    if answer.exit_code != 0 or "clang" not in version_line.lower():
        return Absence(
            reason=f"the toolbox image {IMAGE} is present but its compiler did not answer",
            suggestion=f"rebuild or re-pull it, then restart this server: docker pull {IMAGE}",
        )

    facts = {DIGEST_FACT: first_line(inspected.output).strip(), **_env_facts(run)}
    return Bridge(
        analyses=CARRIED,
        toolchain=clang.toolchain(Path(COMPILER), version_line),
        platform=container_platform(facts),
        runner=run,
    )


def dockerfile_dir() -> Path:
    """Where the packaged Dockerfile lives, so an offline machine can build the image."""
    return Path(__file__).parent / "toolbox"


def container_platform(env_facts: Mapping[str, str]) -> Platform:
    """Describe the Linux inside the image, in the same data every real OS is described in.
    The failure signatures are linux.py's own table, keyed by kernel facts read inside --
    they are the VM's, not this host's.
    """
    inside = (
        "runs inside a Docker container; the host is mounted read-only, builds stay in "
        "the server's own scratch space, and nothing in your project is written to"
    )
    fixes_stay = (
        "suggested fixes are not relayed from the container engine: the paths inside the "
        "tool's fix export do not resolve on the host. Findings are unaffected."
    )
    db_may_miss = (
        "a compile_commands.json written by a host build may not resolve inside the "
        "container; without one, the default flags apply"
    )
    return Platform(
        name=NAME,
        engine=NAME,
        compile_extras=linux.COMPILE_EXTRAS,
        cmake_extras=CMAKE_EXTRAS,
        denied=DENIED,
        limitations={
            Analysis.TSAN: (inside,),
            Analysis.ASAN: (inside,),
            Analysis.LSAN: (inside,),
            Analysis.UBSAN: (inside,),
            Analysis.THREAD_SAFETY: (inside,),
            Analysis.CLANG_TIDY: (inside, fixes_stay, db_may_miss),
        },
        failure_signatures=linux.failure_signatures(env_facts.get(linux.MMAP_RND_BITS_FACT)),
        env_facts=env_facts,
    )


def contained(runner: Runner, docker: str, mounts: Sequence[Mount]) -> Runner:
    """Wrap a runner so every command runs inside a fresh toolbox container: argv and cwd
    paths respelled on the way in, mount prefixes respelled back in output on the way out.
    A timed-out container is killed by name -- killing the docker client leaves it running.
    """

    def run(
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        name = f"cpp-analysis-{uuid.uuid4().hex[:12]}"
        # seccomp relaxed per ADR-0004's measured evidence: TSan dies on the default
        # profile's personality() block, and the goldens were captured this way
        argv = [docker, "run", "--rm", "--init", "--security-opt", "seccomp=unconfined"]
        argv += ["--name", name]
        for mount in mounts:
            # --mount, never -v: a drive-letter host path carries its own colon
            flag = f"type=bind,source={mount.host},target={mount.inside.rstrip('/')}"
            argv += ["--mount", flag if mount.writable else flag + ",readonly"]
        if cwd is not None:
            argv += ["-w", to_container(str(cwd), mounts)]
        for pin in _pins(env):
            argv += ["-e", pin]
        argv += [IMAGE, *(to_container(arg, mounts) for arg in cmd)]

        result = runner(argv, timeout_s=timeout_s, env=env, cwd=None)
        if result.timed_out:
            # bounded and best-effort: --rm reaps the container once the kill lands
            runner([docker, "kill", name], timeout_s=process.KILL_GRACE_S)
        return RunResult(exit_code=result.exit_code, output=host_spelling(result.output, mounts))

    return run


def mount_table(
    workspace: Path,
    *,
    temp: Path | None = None,
    drives: Sequence[Path] | None = None,
) -> tuple[Mount, ...]:
    """What the container may see: scratch space writable, everything else read-only --
    deliberately less than the WSL bridge sees. Ordered longest-key-first so translation
    always takes the most specific mount.
    """
    scratch_mounts = [
        _mount(workspace, INSIDE_WS, writable=True),
        _mount(temp or Path(tempfile.gettempdir()), INSIDE_TMP, writable=True),
    ]
    roots = drives if drives is not None else _drive_roots()
    root_mounts = [_mount(root, _inside_root(root), writable=False) for root in roots]
    everything = scratch_mounts + root_mounts
    return tuple(sorted(everything, key=lambda mount: len(mount.key), reverse=True))


def to_container(arg: str, mounts: Sequence[Mount]) -> str:
    """Respell one whole-argument host path for the container; anything else passes through.
    Whole arguments only, the measured rule wsl.py already follows: no command this
    project composes embeds an absolute path inside a larger argument.
    """
    key = _normalized(arg)
    if not _absolute(key) or key.startswith(NEVER_TRANSLATED):
        return arg
    for mount in mounts:
        if key.lower().startswith(mount.key.lower()):
            return mount.inside + key[len(mount.key) :]
        # the mount's own directory, arriving without its trailing slash (a cwd, a -p flag)
        if (key + "/").lower() == mount.key.lower():
            return mount.inside.rstrip("/")
    return arg


def host_spelling(text: str, mounts: Sequence[Mount]) -> str:
    """Rewrite every container path in tool output back to the host's own spelling -- what
    keeps parsers, fingerprints and reports ignorant of the container: by the time output
    leaves the runner, it reads as if the tool ran here.
    """
    ordered = sorted(mounts, key=lambda mount: len(mount.inside), reverse=True)
    for mount in ordered:
        text = text.replace(mount.inside, mount.key)
    for mount in ordered:
        text = text.replace(mount.inside.rstrip("/"), mount.key.rstrip("/"))
    return text


DRIVE = re.compile(r"^[A-Za-z]:/")


def _mount(host: Path, inside: str, *, writable: bool) -> Mount:
    key = host.as_posix()
    if not key.endswith("/"):
        key += "/"
    # forward slashes even on Windows: docker accepts them, and --mount csv needs no escaping
    return Mount(host=host.as_posix(), key=key, inside=inside.rstrip("/") + "/", writable=writable)


def _inside_root(root: Path) -> str:
    # C:\ becomes /mnt/host/c, matching the /mnt/<drive> habit WSL users already know;
    # a posix root becomes /mnt/host itself
    drive = root.drive.rstrip(":").lower()
    return f"{INSIDE_HOST}/{drive}" if drive else INSIDE_HOST


def _drive_roots() -> tuple[Path, ...]:
    if sys.platform == "win32":
        return tuple(
            Path(f"{letter}:\\")
            for letter in string.ascii_uppercase
            if Path(f"{letter}:\\").exists()
        )
    return (Path("/"),)


def _normalized(arg: str) -> str:
    return arg.replace("\\", "/")


def _absolute(key: str) -> bool:
    return key.startswith("/") or DRIVE.match(key) is not None


def _env_facts(run: Runner) -> dict[str, str]:
    """Read the kernel facts a capability depends on, off the container's own kernel:
    they live in Docker's VM, so reading this host's would answer for the wrong machine.
    """
    facts: dict[str, str] = {}
    for name, path in linux.HOST_SETTINGS.items():
        asked = run(["cat", path.as_posix()], timeout_s=DAEMON_TIMEOUT_S)
        value = asked.output.strip()
        if asked.exit_code == 0 and value:
            facts[name] = value
    return facts


def _pins(env: Mapping[str, str] | None) -> tuple[str, ...]:
    """The K=V pairs -e carries inside: sanitizer options only. process.py owns the list."""
    if env is None:
        return ()
    return tuple(f"{name}={env[name]}" for name in process.SANITIZER_ENV_VARS if name in env)
