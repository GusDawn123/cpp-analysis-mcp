"""The container engine with no Docker anywhere: the only fake is the subprocess boundary.

Discovery, the mount table, path respelling both directions, and the wrapping runner are
real code; the fakes script only what the docker CLI would answer.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp import capabilities, container
from cpp_analysis_mcp.platforms import linux
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store.models import Analysis

DOCKER_PATH = str(Path("/usr/bin/docker"))

CLANG_VERSION = "Ubuntu clang version 18.1.3 (1ubuntu1)\nTarget: x86_64-pc-linux-gnu\n"

DIGEST = "sha256:aaaa1111\n"

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
        self.calls.append(list(cmd))
        self.envs.append(env)
        self.cwds.append(cwd)
        return self.reply(list(cmd))


def healthy_docker(argv: list[str]) -> RunResult:
    """What a machine with a running daemon and the toolbox image answers."""
    if "version" in argv:
        return RunResult(exit_code=0, output="29.3.1\n")
    if "inspect" in argv:
        return RunResult(exit_code=0, output=DIGEST)
    if container.COMPILER in argv:
        return RunResult(exit_code=0, output=CLANG_VERSION)
    if "cat" in argv:
        return RunResult(exit_code=0, output="32\n")
    return RunResult(exit_code=0, output="")


def docker_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: DOCKER_PATH if name == container.DOCKER else None
    )


# ------------------------------------------------------------------------------- discovery


def test_no_docker_names_the_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    absence = container.discover(workspace=tmp_path, runner=FakeRunner(reply=healthy_docker))
    assert isinstance(absence, container.Absence)
    assert "not installed" in absence.reason
    assert "install Docker" in absence.suggestion


def test_sleeping_daemon_says_start_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    docker_on_path(monkeypatch)

    def daemon_down(argv: list[str]) -> RunResult:
        if "version" in argv:
            return RunResult(exit_code=1, output="cannot connect to the Docker daemon")
        return healthy_docker(argv)

    absence = container.discover(workspace=tmp_path, runner=FakeRunner(reply=daemon_down))
    assert isinstance(absence, container.Absence)
    assert "daemon" in absence.reason
    assert "start Docker" in absence.suggestion


def test_missing_image_carries_the_pull_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_on_path(monkeypatch)

    def no_image(argv: list[str]) -> RunResult:
        if "inspect" in argv:
            return RunResult(exit_code=1, output="Error: No such image")
        return healthy_docker(argv)

    absence = container.discover(workspace=tmp_path, runner=FakeRunner(reply=no_image))
    assert isinstance(absence, container.Absence)
    assert container.IMAGE in absence.reason
    assert f"docker pull {container.IMAGE}" in absence.suggestion
    assert "docker build" in absence.suggestion


def test_discovery_binds_the_toolbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    docker_on_path(monkeypatch)
    bridge = container.discover(workspace=tmp_path, runner=FakeRunner(reply=healthy_docker))

    assert isinstance(bridge, container.Bridge)
    assert bridge.analyses == frozenset(Analysis) - {Analysis.PROFILE}
    assert bridge.toolchain.family == "clang"
    assert "clang version 18" in bridge.toolchain.version
    assert bridge.platform.name == container.NAME
    assert bridge.platform.engine == container.NAME
    assert bridge.platform.env_facts[container.DIGEST_FACT] == "sha256:aaaa1111"
    assert bridge.platform.env_facts[linux.MMAP_RND_BITS_FACT] == "32"
    assert Analysis.PROFILE in bridge.platform.denied


def test_a_broken_image_is_reported_not_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_on_path(monkeypatch)

    def mute_compiler(argv: list[str]) -> RunResult:
        if container.COMPILER in argv:
            return RunResult(exit_code=127, output="exec: clang++: not found")
        return healthy_docker(argv)

    absence = container.discover(workspace=tmp_path, runner=FakeRunner(reply=mute_compiler))
    assert isinstance(absence, container.Absence)
    assert "did not answer" in absence.reason


# ----------------------------------------------------------------------------- translation


def windows_shaped_mounts() -> tuple[container.Mount, ...]:
    """A hand-built table in Windows spellings, so these tests run identically on any OS."""
    return (
        container.Mount(
            host="C:/Users/dev/ws",
            key="C:/Users/dev/ws/",
            native="C:\\Users\\dev\\ws\\",
            inside="/mnt/ws/",
            writable=True,
        ),
        container.Mount(
            host="C:/", key="C:/", native="C:\\", inside="/mnt/host/c/", writable=False
        ),
    )


def test_translation_prefers_the_most_specific_mount() -> None:
    mounts = windows_shaped_mounts()
    assert container.to_container("C:\\Users\\dev\\ws\\a.cpp", mounts) == "/mnt/ws/a.cpp"
    assert container.to_container("C:\\proj\\b.cpp", mounts) == "/mnt/host/c/proj/b.cpp"


def test_relative_arguments_and_flags_pass_through() -> None:
    mounts = windows_shaped_mounts()
    for arg in ("clang++", "--checks=-*,bugprone-*", "src/a.cpp", "-std=c++20"):
        assert container.to_container(arg, mounts) == arg


def test_kernel_views_are_never_translated() -> None:
    posix_root = (
        container.Mount(host="/", key="/", native="/", inside="/mnt/host/", writable=False),
    )
    assert container.to_container("/proc/sys/vm/mmap_rnd_bits", posix_root) == (
        "/proc/sys/vm/mmap_rnd_bits"
    )
    assert container.to_container("/home/dev/a.cpp", posix_root) == "/mnt/host/home/dev/a.cpp"


def test_output_comes_back_in_host_spelling() -> None:
    mounts = windows_shaped_mounts()
    reported = (
        "/mnt/ws/scratch/a.cpp:3:5: warning: leak\n"
        "allocated at /mnt/host/c/proj/pool.cpp:41\n"
        "build dir was /mnt/ws"
    )
    translated = container.host_spelling(reported, mounts)
    # native spelling on purpose: parsers expect what a local tool would have printed,
    # and on Windows that is backslashes with the drive letter intact
    assert "C:\\Users\\dev\\ws\\scratch/a.cpp:3:5" in translated
    assert "C:\\proj/pool.cpp:41" in translated
    assert translated.endswith("build dir was C:\\Users\\dev\\ws")
    assert "/mnt/" not in translated


def test_a_lookalike_prefix_is_left_alone() -> None:
    """/mnt/ws-cache is not the workspace: bare rewrites stop at a name boundary."""
    text = container.host_spelling("saw /mnt/ws-cache and /mnt/ws today", windows_shaped_mounts())
    assert text == "saw /mnt/ws-cache and C:\\Users\\dev\\ws today"


def test_the_workspace_mount_is_the_only_writable_project_path(tmp_path: Path) -> None:
    table = container.mount_table(tmp_path / "ws", temp=tmp_path / "tmp", drives=(tmp_path,))
    writable = {mount.inside for mount in table if mount.writable}
    assert writable == {container.INSIDE_WS + "/", container.INSIDE_TMP + "/"}
    (drive,) = (mount for mount in table if not mount.writable)
    assert drive.key == tmp_path.as_posix() + "/"


# ---------------------------------------------------------------------------- the runner


def test_commands_run_inside_the_image(tmp_path: Path) -> None:
    mounts = windows_shaped_mounts()
    fake = FakeRunner(reply=healthy_docker)
    run = container.contained(fake, DOCKER_PATH, mounts)

    run(
        ["clang++", "C:\\Users\\dev\\ws\\a.cpp", "-o", "C:\\Users\\dev\\ws\\a.out"],
        timeout_s=60,
        cwd=Path("C:\\Users\\dev\\ws"),
    )

    (argv,) = fake.calls
    assert argv[:6] == [
        DOCKER_PATH,
        "run",
        "--rm",
        "--init",
        "--security-opt",
        "seccomp=unconfined",
    ]
    name = argv[argv.index("--name") + 1]
    assert name.startswith("cpp-analysis-")
    assert "type=bind,source=C:/Users/dev/ws,target=/mnt/ws" in argv
    assert "type=bind,source=C:/,target=/mnt/host/c,readonly" in argv
    assert argv[argv.index("-w") + 1] == "/mnt/ws"
    image_at = argv.index(container.IMAGE)
    assert argv[image_at + 1 :] == ["clang++", "/mnt/ws/a.cpp", "-o", "/mnt/ws/a.out"]


def test_sanitizer_options_cross_and_nothing_else(tmp_path: Path) -> None:
    fake = FakeRunner(reply=healthy_docker)
    run = container.contained(fake, DOCKER_PATH, windows_shaped_mounts())

    run(["ls"], timeout_s=5, env={"ASAN_OPTIONS": "detect_leaks=1", "PATH": "C:/secret"})

    (argv,) = fake.calls
    assert argv[argv.index("-e") + 1] == "ASAN_OPTIONS=detect_leaks=1"
    assert argv.count("-e") == 1


def test_a_timed_out_container_is_killed_by_name() -> None:
    def hang_then_confirm(argv: list[str]) -> RunResult:
        if "kill" in argv:
            return RunResult(exit_code=0, output="")
        return RunResult(exit_code=None, output="[killed after 5s timeout]")

    fake = FakeRunner(reply=hang_then_confirm)
    run = container.contained(fake, DOCKER_PATH, windows_shaped_mounts())

    result = run(["sleep", "999"], timeout_s=5)

    assert result.timed_out
    launch, kill = fake.calls
    assert kill == [DOCKER_PATH, "kill", launch[launch.index("--name") + 1]]


def test_the_runner_translates_its_own_output() -> None:
    def reports_inside_paths(argv: list[str]) -> RunResult:
        return RunResult(exit_code=0, output="warning at /mnt/host/c/proj/a.cpp:7")

    run = container.contained(
        FakeRunner(reply=reports_inside_paths), DOCKER_PATH, windows_shaped_mounts()
    )
    assert run(["true"], timeout_s=5).output == "warning at C:\\proj/a.cpp:7"


# ------------------------------------------------------------------- the capability seam


def test_bridged_platforms_resolve_tools_on_their_own_path() -> None:
    platform = container.container_platform({container.DIGEST_FACT: "sha256:x"})
    assert capabilities.find_clang_tidy(platform) == Path(capabilities.CLANG_TIDY)


# --------------------------------------------------------------------------- the toolbox


def test_the_packaged_dockerfile_is_where_discovery_says() -> None:
    """The build-it-yourself suggestion points here, so the file has to ship with the code."""
    dockerfile = container.dockerfile_dir() / "Dockerfile"
    assert dockerfile.is_file()
    text = dockerfile.read_text(encoding="utf-8")
    assert "FROM ubuntu" in text
    assert "clang-tidy" in text


def test_the_publish_workflow_ships_the_image_discovery_asks_for() -> None:
    """One string in two places: the workflow's tag and container.IMAGE must never drift."""
    workflow = Path(__file__).parents[2] / ".github" / "workflows" / "publish-toolbox.yml"
    assert container.IMAGE in workflow.read_text(encoding="utf-8")
