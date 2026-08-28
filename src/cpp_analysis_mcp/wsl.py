"""Windows's borrowed Linux: find a WSL distro with clang and run through it what Windows
cannot -- no compiler ships TSan or LSan for Windows, but WSL's clang carries both.
Spawns only through the Runner it is handed; everything above this file stays ignorant
of the bridge, composing the same commands as always and getting Linux answers back.
"""

from __future__ import annotations

import os
import re
import shutil
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

NAME = "wsl"

WSL = "wsl"

# the analyses Windows structurally lacks -- the only ones worth carrying across
BRIDGED = frozenset({Analysis.TSAN, Analysis.LSAN, Analysis.PROFILE})

# the compiler as the wrapped runner will resolve it, on the distro's own PATH
COMPILER = "clang++"

LIST_TIMEOUT_S = 30
# a distro that has not booted yet pays its whole VM start inside this answer
ASK_TIMEOUT_S = 60

# management commands (-l and friends) answer in UTF-16 by default, which decodes to
# NUL-riddled text through the runner; this makes them answer in UTF-8. Measured.
UTF8_FLAG = "WSL_UTF8"

# base Ubuntu ships no make, so cmake's default generator would configure and then have
# nothing to build with; the documented setup installs ninja instead
CMAKE_EXTRAS = ("-G", "Ninja")

DRIVE_PATH = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.DOTALL)

_NATIVE = Denial(
    reason="handled natively on Windows; the WSL bridge only carries what Windows cannot run"
)

# lets probe_all answer these four without spawning: the bridge platform's structural nos
# are everything the native Windows platform already does better
DENIED = {
    Analysis.ASAN: _NATIVE,
    Analysis.UBSAN: _NATIVE,
    Analysis.THREAD_SAFETY: _NATIVE,
    Analysis.CLANG_TIDY: _NATIVE,
}

DISTRO_FACT = "distro"


@dataclass(frozen=True, slots=True)
class Bridge:
    """A Linux inside this Windows host, bound to the analyses it carries.

    The three fields after `analyses` are exactly what a pipeline call varies by, so a
    resolved context can point an analysis here instead of at the native host and nothing
    downstream can tell the difference.
    """

    analyses: frozenset[Analysis]
    toolchain: Toolchain
    platform: Platform
    runner: Runner


def discover(*, runner: Runner = process.run) -> Bridge | None:
    """Find a WSL distro that answers for clang, or None on the many machines without one.

    Each distro is asked `clang++ --version` through the same wrapped spawn shape the
    bridge itself uses, so what passes here works later. Utility distros (docker-desktop)
    fail that question and are skipped by measurement, not a name denylist. Every miss --
    no wsl.exe, no distro, no clang -- is an ordinary machine: None, and the native
    denials already say what to do.
    """
    wsl_exe = shutil.which(WSL)
    if wsl_exe is None:
        return None
    listed = runner(
        [wsl_exe, "-l", "-q"],
        timeout_s=LIST_TIMEOUT_S,
        env={**os.environ, UTF8_FLAG: "1"},
    )
    if listed.exit_code != 0:
        return None

    for distro in _distros(listed.output):
        answer = runner(
            _wrapped(wsl_exe, distro, None, (), (COMPILER, "--version")),
            timeout_s=ASK_TIMEOUT_S,
        )
        if answer.exit_code != 0:
            continue
        version = first_line(answer.output)
        if "clang" not in version.lower():
            continue
        return Bridge(
            analyses=BRIDGED,
            toolchain=clang.toolchain(Path(COMPILER), version),
            platform=bridge_platform(distro, _env_facts(wsl_exe, distro, runner)),
            runner=bridged(runner, wsl_exe, distro),
        )
    return None


def bridge_platform(distro: str, env_facts: Mapping[str, str]) -> Platform:
    """Describe the Linux behind the bridge, as the same data every real OS is described in.

    The failure signatures are linux.py's own table: the bridge is a Linux, and those
    crashes (ASLR width, split runtime packages) were measured there. The env facts carry
    the distro name and its vm.mmap_rnd_bits into the capability-cache fingerprint, so a
    swapped distro or a changed kernel setting retires the cached answers.
    """
    inside = (
        f"runs inside WSL distro {distro!r}; file paths in its reports appear "
        "in WSL form (/mnt/c/... is C:\\...)"
    )
    measures_linux_build = (
        "profiles a Linux build made by the distro's clang against its libstdc++, not the "
        "binary Windows would produce; portable code ranks the same either way, but "
        "standard-library and system-call costs are that platform's"
    )
    return Platform(
        name=NAME,
        compile_extras=linux.COMPILE_EXTRAS,
        cmake_extras=CMAKE_EXTRAS,
        denied=DENIED,
        limitations={
            Analysis.TSAN: (inside,),
            Analysis.LSAN: (inside,),
            Analysis.PROFILE: (inside, measures_linux_build),
        },
        failure_signatures=linux.failure_signatures(env_facts.get(linux.MMAP_RND_BITS_FACT)),
        env_facts=env_facts,
    )


def bridged(runner: Runner, wsl_exe: str, distro: str) -> Runner:
    """Wrap a runner so every command it is handed runs inside the distro instead.

    Windows drive paths in the argv are respelled /mnt/<drive>; a cwd rides in on --cd,
    which takes the Windows spelling as it stands; and the sanitizer option variables --
    the one part of the environment that must cross, since WSL forwards nothing else --
    are pinned onto the Linux side through the `env` prefix. The outer wsl.exe process
    keeps the caller's environment untouched.
    """

    def run(
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        return runner(
            _wrapped(wsl_exe, distro, cwd, _pins(env), tuple(to_wsl(arg) for arg in cmd)),
            timeout_s=timeout_s,
            env=env,
            cwd=None,
        )

    return run


def to_wsl(arg: str) -> str:
    """Respell one whole-argument Windows drive path for Linux; anything else passes through.

    Whole arguments only, on purpose: every command composed for the bridge's platform was
    checked, and none embeds a drive path inside a larger argument -- a substring rewrite
    would be guessing at shapes that do not occur.
    """
    matched = DRIVE_PATH.match(arg)
    if matched is None:
        return arg
    drive, rest = matched.groups()
    return f"/mnt/{drive.lower()}/{rest}".replace("\\", "/")


def _distros(output: str) -> list[str]:
    """Read distro names out of `wsl -l -q` output.

    The NUL strip covers a wsl.exe old enough to ignore WSL_UTF8 and answer in UTF-16
    anyway, which the runner's decode leaves as NUL-riddled text. Measured shape.
    """
    return [line.strip() for line in output.replace("\x00", "").splitlines() if line.strip()]


def _env_facts(wsl_exe: str, distro: str, runner: Runner) -> dict[str, str]:
    """Read the volatile facts a bridged capability depends on, off the distro itself.

    The same settings linux.env_facts() reads on a real Linux, asked one at a time through
    the bridge: these live in the distro's kernel, not this Windows one, so reading them
    here would answer about the wrong machine.
    """
    facts = {DISTRO_FACT: distro}
    for name, path in linux.HOST_SETTINGS.items():
        asked = runner(
            # as_posix(), not str(): a Path renders in the host's spelling, so on Windows
            # str() would hand the distro "\proc\sys\..." -- which cat reports as missing,
            # silently leaving the fact unread and the cache fingerprint blind to a change.
            _wrapped(wsl_exe, distro, None, (), ("cat", path.as_posix())),
            timeout_s=ASK_TIMEOUT_S,
        )
        value = asked.output.strip()
        if asked.exit_code == 0 and value:
            facts[name] = value
    return facts


def _pins(env: Mapping[str, str] | None) -> tuple[str, ...]:
    """The K=V assignments the `env` prefix carries across: sanitizer options and only those.

    process.py owns the list. The rest of the caller's environment stays on the Windows
    side, which is where hygienic_env already scrubbed it.
    """
    if env is None:
        return ()
    return tuple(f"{name}={env[name]}" for name in process.SANITIZER_ENV_VARS if name in env)


def _wrapped(
    wsl_exe: str,
    distro: str,
    cwd: Path | None,
    pins: tuple[str, ...],
    argv: Sequence[str],
) -> list[str]:
    """Compose the one spawn shape everything here uses: --exec env, measured safe for spaces."""
    command = [wsl_exe, "-d", distro]
    if cwd is not None:
        command += ["--cd", str(cwd)]
    return [*command, "--exec", "env", *pins, *argv]
