"""The WSL bridge with no WSL anywhere: the only fake is the subprocess boundary.

Discovery, the platform table, the path respelling and the wrapping runner are all the real
code running; what the fakes script is what wsl.exe and the distros inside it would have
answered. The measured shapes these tests pin -- the UTF-16 listing, the --exec env spawn,
the --cd Windows path -- come from the 2026-08-12 spec's measurements, so a refactor that
drifts from what the real wsl.exe accepts fails here first.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp import wsl
from cpp_analysis_mcp.platforms import linux
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store.models import Analysis

# where the fake PATH puts wsl.exe; spelled through Path so comparisons hold on any OS
WSL_PATH = str(Path("/windows/system32/wsl.exe"))

# first line copied from the real machine the bridge was measured on
UBUNTU_CLANG = "Ubuntu clang version 21.1.8 (6ubuntu1)\nTarget: x86_64-pc-linux-gnu\n"

# what `wsl -l -q` printed on the measured machine: the utility distro listed first,
# exactly the order that would fool a discovery that took the first name it saw
DISTROS = "docker-desktop\nUbuntu\n"

# the same listing as a wsl.exe that ignores WSL_UTF8 answers it: UTF-16 through a
# UTF-8 decode, NUL after every character. Measured shape.
DISTROS_UTF16 = "".join(f"{ch}\x00" for ch in DISTROS)

# what the distro answers when asked for each kernel setting the bridge fingerprints on.
# Keyed by the posix spelling on purpose, so a discovery that asks in Windows spelling falls
# through to the assertion instead of being answered: these are Linux paths carried in a Path,
# and a Path renders as "\proc\sys\..." on the only OS that has a bridge, which cat cannot
# open. Answering both spellings here is what let that go unnoticed once already.
HOST_SETTINGS = {
    linux.MMAP_RND_BITS.as_posix(): "32\n",
    linux.PERF_PARANOID.as_posix(): "2\n",
}

Reply = Callable[[list[str]], RunResult]


@dataclass
class FakeRunner:
    """Answer scripted RunResults and record every spawn, environment and cwd included."""

    reply: Reply
    calls: list[list[str]] = field(default_factory=list)
    envs: list[Mapping[str, str] | None] = field(default_factory=list)
    cwds: list[Path | None] = field(default_factory=list)

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        timeout_s: int,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        recorded = list(cmd)
        self.calls.append(recorded)
        self.envs.append(env)
        self.cwds.append(cwd)
        return self.reply(recorded)


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
        raise AssertionError(f"a discovery with nothing to find spawned {list(cmd)}")


def wsl_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: WSL_PATH if name == wsl.WSL else None)


def asked_distro(cmd: list[str]) -> str:
    """Read which distro a wrapped command was aimed at."""
    return cmd[cmd.index("-d") + 1]


def a_machine_where_ubuntu_has_clang(listing: str = DISTROS) -> Reply:
    """Script the measured machine: a utility distro that cannot compile, then an Ubuntu."""

    def reply(cmd: list[str]) -> RunResult:
        if cmd[1:3] == ["-l", "-q"]:
            return RunResult(exit_code=0, output=listing)
        if cmd[-1] == "--version":
            if asked_distro(cmd) == "Ubuntu":
                return RunResult(exit_code=0, output=UBUNTU_CLANG)
            # docker-desktop: a busybox userland with no compiler anywhere
            return RunResult(exit_code=1, output="env: 'clang++': No such file or directory")
        if cmd[-1] in HOST_SETTINGS:
            return RunResult(exit_code=0, output=HOST_SETTINGS[cmd[-1]])
        raise AssertionError(f"discovery asked something unexpected: {cmd}")

    return reply


# ---------------------------------------------------------------------------------- discovery


def test_a_machine_without_wsl_has_no_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most machines. The answer must cost nothing: no wsl.exe means nothing to ask."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert wsl.discover(runner=RefusingRunner()) is None


def test_a_wsl_that_cannot_list_distros_has_no_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """wsl.exe with the feature off answers -l with an error, not an empty list."""
    wsl_on_path(monkeypatch)
    runner = FakeRunner(lambda cmd: RunResult(exit_code=1, output="WSL is not installed."))

    assert wsl.discover(runner=runner) is None


def test_distros_without_clang_leave_no_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A distro is skipped because it failed the clang question, never because of its name."""
    wsl_on_path(monkeypatch)

    def reply(cmd: list[str]) -> RunResult:
        if cmd[1:3] == ["-l", "-q"]:
            return RunResult(exit_code=0, output="docker-desktop\n")
        return RunResult(exit_code=1, output="env: 'clang++': No such file or directory")

    assert wsl.discover(runner=FakeRunner(reply)) is None


def test_a_distro_whose_clang_answers_as_something_else_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exit 0 alone proves nothing: a clang++ that is really a gcc shim answers politely
    and must still fail the question -- the version text is the evidence, not the exit."""
    wsl_on_path(monkeypatch)

    def reply(cmd: list[str]) -> RunResult:
        if cmd[1:3] == ["-l", "-q"]:
            return RunResult(exit_code=0, output="Legacy\n")
        return RunResult(exit_code=0, output="g++ (Ubuntu 13.2.0-23ubuntu4) 13.2.0\n")

    assert wsl.discover(runner=FakeRunner(reply)) is None


def test_the_first_distro_that_answers_for_clang_becomes_the_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole discovery: list, ask each distro the clang question, bind the winner."""
    wsl_on_path(monkeypatch)
    runner = FakeRunner(a_machine_where_ubuntu_has_clang())

    bridge = wsl.discover(runner=runner)

    assert bridge is not None
    assert bridge.analyses == frozenset({Analysis.TSAN, Analysis.LSAN, Analysis.PROFILE})
    assert bridge.toolchain.family == "clang"
    assert bridge.toolchain.version == "Ubuntu clang version 21.1.8 (6ubuntu1)"
    assert bridge.platform.name == "wsl"
    assert bridge.platform.env_facts["distro"] == "Ubuntu"
    # both kernel settings were read off the distro rather than off this Windows, and both
    # reached it in a spelling it could open
    assert bridge.platform.env_facts["vm.mmap_rnd_bits"] == "32"
    assert bridge.platform.env_facts["kernel.perf_event_paranoid"] == "2"
    # docker-desktop was asked and failed, which is how it was skipped
    asked = [asked_distro(cmd) for cmd in runner.calls if "-d" in cmd and cmd[-1] == "--version"]
    assert asked == ["docker-desktop", "Ubuntu"]


def test_the_listing_is_asked_in_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Management output is UTF-16 unless WSL_UTF8=1 says otherwise (measured); the flag
    must ride on the listing's environment or every distro name comes back NUL-riddled."""
    wsl_on_path(monkeypatch)
    runner = FakeRunner(a_machine_where_ubuntu_has_clang())

    wsl.discover(runner=runner)

    listing_env = runner.envs[0]
    assert listing_env is not None
    assert listing_env["WSL_UTF8"] == "1"


def test_a_utf16_listing_is_still_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wsl.exe old enough to ignore WSL_UTF8 answers in UTF-16 anyway, which the runner's
    decode leaves as NUL-riddled text. The names must survive that, or old machines would
    look distro-less while `wsl -l` shows plenty."""
    wsl_on_path(monkeypatch)
    runner = FakeRunner(a_machine_where_ubuntu_has_clang(listing=DISTROS_UTF16))

    bridge = wsl.discover(runner=runner)

    assert bridge is not None
    assert bridge.platform.env_facts["distro"] == "Ubuntu"


# ------------------------------------------------------------------------------- path respell


def test_windows_drive_paths_are_respelled_for_linux() -> None:
    assert wsl.to_wsl(r"C:\Users\g\code\app.cpp") == "/mnt/c/Users/g/code/app.cpp"
    assert wsl.to_wsl("D:/scratch/out") == "/mnt/d/scratch/out"


def test_everything_that_is_not_a_drive_path_passes_through() -> None:
    """Whole arguments only: flags, bare command names and relative paths are not paths to
    respell, and a substring rewrite would be guessing at shapes no composed command has."""
    for arg in ("clang++", "-fsanitize=thread", "-o", r"relative\path.cpp", "env", "C:"):
        assert wsl.to_wsl(arg) == arg


# --------------------------------------------------------------------------- wrapping runner


def test_the_wrapped_command_is_the_measured_spawn_shape() -> None:
    """Prefix, --cd with the Windows spelling, --exec env, pins, then the respelled argv --
    the exact shape the spec measured as safe for spaces and PATH lookup."""
    inner = FakeRunner(lambda cmd: RunResult(exit_code=66, output="race"))
    run = wsl.bridged(inner, WSL_PATH, "Ubuntu")
    cwd = Path(r"C:\work\build")

    result = run(
        [r"C:\work\build\probe_tsan"],
        timeout_s=30,
        env={"PATH": r"C:\x", "TSAN_OPTIONS": "exitcode=66"},
        cwd=cwd,
    )

    assert inner.calls == [
        [
            WSL_PATH,
            "-d",
            "Ubuntu",
            "--cd",
            str(cwd),
            "--exec",
            "env",
            "TSAN_OPTIONS=exitcode=66",
            "/mnt/c/work/build/probe_tsan",
        ]
    ]
    # what came back is what the inner runner produced, untouched
    assert result.exit_code == 66
    assert result.output == "race"


def test_only_sanitizer_options_cross_into_linux() -> None:
    """WSL forwards no environment on its own, and nothing else should cross: the rest of
    the caller's environment is Windows configuration, meaningless or harmful inside."""
    inner = FakeRunner(lambda cmd: RunResult(exit_code=0, output=""))
    run = wsl.bridged(inner, WSL_PATH, "Ubuntu")
    env = {"PATH": r"C:\x", "TMP": r"C:\t", "LSAN_OPTIONS": "verbosity=0"}

    run(["binary"], timeout_s=5, env=env)

    pinned = [arg for arg in inner.calls[0] if "=" in arg]
    assert pinned == ["LSAN_OPTIONS=verbosity=0"]
    # the outer wsl.exe process keeps the caller's environment, and runs from the
    # caller's own directory -- the --cd flag is what carries a cwd inside
    assert inner.envs == [env]
    assert inner.cwds == [None]


def test_a_call_without_environment_gets_no_pins() -> None:
    """A compile: hygienic_env({}) upstream strips every sanitizer option, so nothing is
    there to pin and the spawn is just `env <argv>`."""
    inner = FakeRunner(lambda cmd: RunResult(exit_code=0, output=""))
    run = wsl.bridged(inner, WSL_PATH, "Ubuntu")

    run(["clang++", "--version"], timeout_s=5)

    assert inner.calls == [[WSL_PATH, "-d", "Ubuntu", "--exec", "env", "clang++", "--version"]]


# -------------------------------------------------------------------------- the platform data


def test_the_bridge_platform_only_carries_what_windows_cannot_run() -> None:
    """The four native analyses are structural nos here, so probe_all answers them without
    spawning and the bridge never competes with the platform that does them better."""
    platform = wsl.bridge_platform("Ubuntu", {"distro": "Ubuntu"})

    assert set(platform.denied) == {
        Analysis.ASAN,
        Analysis.UBSAN,
        Analysis.THREAD_SAFETY,
        Analysis.CLANG_TIDY,
    }
    assert Analysis.TSAN not in platform.denied
    assert Analysis.LSAN not in platform.denied


def test_the_bridged_analyses_say_where_they_run_and_how_paths_read() -> None:
    """The one surprise a caller meets is /mnt/c paths in findings; the limitation is where
    that is explained, and it travels on every report the pipeline produces."""
    platform = wsl.bridge_platform("Ubuntu", {"distro": "Ubuntu"})

    for analysis in wsl.BRIDGED:
        notes = platform.limitations[analysis]
        assert any("Ubuntu" in note and "/mnt/c" in note for note in notes), analysis


def test_a_bridged_profile_says_it_ranked_a_different_binary() -> None:
    """The one bridged analysis whose answer is about a build Windows would not have made.

    A race is a race in either build, so TSan's answer carries across unchanged. Where the
    time goes is decided by the compiler and standard library that produced the code, and
    those are the distro's -- a caller told only "runs inside WSL" would read a libstdc++
    hotspot as one they can act on from Windows.
    """
    platform = wsl.bridge_platform("Ubuntu", {"distro": "Ubuntu"})

    notes = platform.limitations[Analysis.PROFILE]

    assert any("libstdc++" in note for note in notes)
    assert len(notes) > len(platform.limitations[Analysis.TSAN])


def test_the_bridge_is_a_linux_and_builds_like_one() -> None:
    """-pthread because glibc needs it for std::thread; Ninja because base distros ship no
    make and the documented setup installs ninja instead."""
    platform = wsl.bridge_platform("Ubuntu", {})

    assert "-pthread" in platform.compile_extras
    assert platform.cmake_extras == ("-G", "Ninja")


def test_the_bridge_reuses_linuxs_measured_failure_signatures() -> None:
    """The bridge is a Linux, so Linux's measured crashes are its crashes: the ASLR width
    signature must quote this distro's own setting, read at discovery."""
    platform = wsl.bridge_platform("Ubuntu", {"vm.mmap_rnd_bits": "32"})

    aslr = platform.diagnose("FATAL: ThreadSanitizer: unexpected memory mapping 0x7f")
    assert aslr is not None
    assert "32" in aslr.reason
    packages = platform.diagnose("/usr/bin/ld: cannot find -ltsan")
    assert packages is not None
    assert packages.suggestion is not None
