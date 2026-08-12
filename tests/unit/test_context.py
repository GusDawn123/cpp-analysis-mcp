"""Resolve the composition root with no compiler anywhere: the only fake is the subprocess.

resolve() reads the real host on purpose -- it is the one sanctioned caller of
platforms.detect() -- so what these tests replace is the boundary underneath it: what PATH
appears to hold, and what each spawn prints. Discovery, the clang preference, the probe gate
and the cache are all the real code running.

What the code under test decides is written down here rather than read back out of it: the
compiler paths the fake PATH hands out, where the probe cache lives by default, the length of
the name a cache file carries, and the phrase the no-compiler error names both compilers in.

Three names are imported on purpose -- Analysis, SANITIZER_FOR and PROBE_STEM. Those are the
vocabulary the fakes have to answer in, not expectations about behaviour: a test carrying its
own list of analyses would quietly stop covering a seventh the day one is added.
"""

from __future__ import annotations

import inspect
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cpp_analysis_mcp.capabilities import PROBE_STEM
from cpp_analysis_mcp.context import prefer, resolve, scratch
from cpp_analysis_mcp.models import SANITIZER_FOR, Analysis, CapabilityStatus
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.toolchains import clang, gcc
from cpp_analysis_mcp.toolchains.base import Toolchain

# where the fake PATH puts everything discovery and the probes go looking for. Spelled
# through Path so the strings compare equal to str(Path(...)) on Windows too, where the
# separator flips
BIN_DIR = Path("/usr/bin")
CLANG_PATH = str(BIN_DIR / "clang++")
GCC_PATH = str(BIN_DIR / "g++")

# first lines of --version, copied from the real thing
APPLE_CLANG = "Apple clang version 17.0.0 (clang-1700.0.13.3)\nTarget: arm64-apple-darwin24.6.0\n"
UBUNTU_GCC = "g++ (Ubuntu 13.2.0-23ubuntu4) 13.2.0\nCopyright (C) 2023 Free Software Foundation\n"

VERSIONS = {"clang++": APPLE_CLANG, "g++": UBUNTU_GCC}

# the same two texts, each printed by the other binary. Not a contrivance: macOS ships
# /usr/bin/g++ as a clang driver, so discovery decides the family from the version text and
# a machine really can hand back its gcc-family toolchain first
SWAPPED_VERSIONS = {"clang++": UBUNTU_GCC, "g++": APPLE_CLANG}

# the supported operating systems: resolve() reads whichever one this machine is
SUPPORTED_PLATFORMS = ("darwin", "linux", "windows")

# a cache file is named after capabilities.fingerprint, which is sha256 written as hex
FINGERPRINT_LENGTH = 64

# what each detector prints when it catches the bug its probe planted, and the status it
# exits with -- a detector that reports is not obliged to exit 0: TSan exits 66 under the
# options capabilities.py pins
CAUGHT: Mapping[Analysis, tuple[int, str]] = {
    Analysis.TSAN: (66, "WARNING: ThreadSanitizer: data race (pid=1234)"),
    Analysis.ASAN: (1, "==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000"),
    Analysis.LSAN: (23, "==1==ERROR: LeakSanitizer: detected memory leaks"),
    Analysis.UBSAN: (0, "probe_ubsan.cpp:5:11: runtime error: signed integer overflow"),
    Analysis.THREAD_SAFETY: (
        0,
        "probe_thread-safety.cpp:14:5: warning: writing variable 'guarded' requires holding "
        "mutex 'm' exclusively [-Wthread-safety-analysis]",
    ),
    Analysis.CLANG_TIDY: (
        0,
        "probe_clang-tidy.cpp:2:14: warning: use nullptr [modernize-use-nullptr]",
    ),
}

Reply = Callable[[list[str]], RunResult]


# ------------------------------------------------------------------------------- the fakes


@dataclass
class FakeRunner:
    """Answer scripted RunResults and record every command, so nothing has to be compiled."""

    reply: Reply
    calls: list[list[str]] = field(default_factory=list)

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
        raise AssertionError(f"a startup that cannot succeed spawned {list(cmd)}")


def probe_analysis(cmd: Sequence[str]) -> Analysis | None:
    """Read which probe a command belongs to off the scratch file it names.

    .exe comes off as well as .cpp: on a real Windows host the probes name their
    binaries with the platform's executable suffix.
    """
    for arg in cmd:
        stem = Path(arg).name.removesuffix(".cpp").removesuffix(".exe")
        if stem.startswith(PROBE_STEM):
            return Analysis(stem.removeprefix(PROBE_STEM))
    return None


def probe_calls(runner: FakeRunner) -> list[list[str]]:
    """Every command a probe spawned; a cache hit adds none of these, only version queries."""
    return [cmd for cmd in runner.calls if probe_analysis(cmd) is not None]


def is_detection(cmd: Sequence[str], analysis: Analysis) -> bool:
    """Say whether this is the step whose output has to carry the marker.

    A sanitizer probe detects when its binary runs, which is a bare one-element command;
    the compile-time checks detect in the only step they have.
    """
    return len(cmd) == 1 or analysis not in SANITIZER_FOR


def a_host_where_every_detector_works(cmd: list[str]) -> RunResult:
    """Answer by command shape: a version query, a build that succeeds, a detector that reports."""
    if cmd[-1] == "--version":
        return RunResult(exit_code=0, output=VERSIONS[Path(cmd[0]).name])
    analysis = probe_analysis(cmd)
    if analysis is None or not is_detection(cmd, analysis):
        return RunResult(exit_code=0, output="")
    exit_code, report = CAUGHT[analysis]
    return RunResult(exit_code=exit_code, output=report)


def a_host_where_each_binary_reports_the_other_family(cmd: list[str]) -> RunResult:
    """Answer every --version with the other compiler's text; probe as the happy path does."""
    if cmd[-1] == "--version":
        return RunResult(exit_code=0, output=SWAPPED_VERSIONS[Path(cmd[0]).name])
    return a_host_where_every_detector_works(cmd)


def both_compilers_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put clang++, g++ and clang-tidy where discovery and the probes look for them."""
    monkeypatch.setattr(shutil, "which", lambda name: str(BIN_DIR / name))


def a_clang() -> Toolchain:
    return clang.toolchain(Path(CLANG_PATH), "Apple clang version 17.0.0 (clang-1700.0.13.3)")


def a_gcc() -> Toolchain:
    return gcc.toolchain(Path(GCC_PATH), "g++ (Ubuntu 13.2.0-23ubuntu4) 13.2.0")


# ------------------------------------------------------------------ which compiler gets used


def test_the_only_compiler_on_the_machine_is_the_one_used() -> None:
    """clang is a preference, never a requirement: plenty of projects only build under gcc."""
    found = (a_gcc(),)

    assert prefer(found).family == "gcc"


def test_clang_wins_even_when_gcc_was_discovered_first() -> None:
    """gcc is listed first here on purpose: the preference has to be a choice, not an
    accident of the order discovery happens to walk PATH in. -Wthread-safety exists on no
    other compiler, so which one is picked decides whether an analysis exists at all."""
    found = (a_gcc(), a_clang())

    chosen = prefer(found)

    assert chosen.family == "clang"
    assert str(chosen.compiler) == CLANG_PATH


# ------------------------------------------------------------------------ the whole startup


def test_resolve_binds_the_host_the_compiler_and_the_probes_into_one_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call at startup, and everything a request needs afterwards is in what came back."""
    both_compilers_on_path(monkeypatch)
    runner = FakeRunner(a_host_where_every_detector_works)

    # no cache: the default is the developer's home directory, which no test may write to
    context = resolve(cache_dir=None, runner=runner)

    assert context.platform.name in SUPPORTED_PLATFORMS
    # both compilers answered, and the preference picked between them
    assert str(context.toolchain.compiler) == CLANG_PATH
    assert set(context.capabilities) == set(Analysis)
    # ASan rather than TSan: it is the one analysis no supported OS denies, so the fake
    # detector's catch reads as available on every machine this test runs on
    assert context.capabilities[Analysis.ASAN].available
    assert context.workspace.is_dir()
    assert context.runner is runner
    shutil.rmtree(context.workspace)


def test_discovery_asked_the_callers_runner_what_the_compilers_are(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery is a spawn too -- `<compiler> --version` on everything PATH offered -- and it
    has to go through the injected runner like every other one. A resolve() that discovered
    with its own runner would run the developer's real compilers behind a test's back, and
    then bind the context to whatever those said rather than to what the test scripted."""
    both_compilers_on_path(monkeypatch)
    runner = FakeRunner(a_host_where_every_detector_works)

    context = resolve(cache_dir=None, runner=runner)

    assert [CLANG_PATH, "--version"] in runner.calls
    assert [GCC_PATH, "--version"] in runner.calls
    shutil.rmtree(context.workspace)


def test_the_probes_ran_through_the_callers_runner_on_the_chosen_compiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capability table must describe the toolchain the context binds, and its probes must
    be visible to whoever injected the runner. A resolve() that probed with its own runner
    would compile for real behind a test's back, and one that probed the other compiler would
    hand every request a table about a machine the pipelines never build on: -Wthread-safety
    probed on gcc reads unavailable while the clang builds quietly have it."""
    both_compilers_on_path(monkeypatch)
    runner = FakeRunner(a_host_where_every_detector_works)

    context = resolve(cache_dir=None, runner=runner)

    compiled_with = {
        cmd[0]
        for cmd in probe_calls(runner)
        if len(cmd) > 1 and probe_analysis(cmd) in SANITIZER_FOR
    }
    # every sanitizer probe compile the fake saw, and all of them on the preferred compiler;
    # empty would mean the probes never came through the runner at all
    assert compiled_with == {CLANG_PATH}
    shutil.rmtree(context.workspace)


def test_startup_still_prefers_clang_when_discovery_hands_back_gcc_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prefer() is tested on its own above; this pins that resolve() is the one asking it.

    The two version texts are swapped, which inverts discovery's order the way a real host
    can -- family comes off the version text, so the clang++ binary here is a gcc and the g++
    binary is a clang. Taking whatever discovery found first would cost every -Wthread-safety
    analysis on a machine that has clang, and nothing in the suite would notice."""
    both_compilers_on_path(monkeypatch)
    runner = FakeRunner(a_host_where_each_binary_reports_the_other_family)

    context = resolve(cache_dir=None, runner=runner)

    assert context.toolchain.family == "clang"
    # the clang on this host is the binary named g++, and that is the one that must be bound
    assert str(context.toolchain.compiler) == GCC_PATH
    shutil.rmtree(context.workspace)


# ------------------------------------------------------------------------------- the cache


def test_asking_for_no_cache_probes_again_and_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cache_dir=None is the explicit "don't remember this", which is what a test asking not
    to touch the developer's home directory needs, and what probe_all already understands.

    What proves nothing was remembered is the second start paying for every probe again.
    Watching the real ~/.cache would prove less and flake more: any other process on the
    machine writing there mid-test fails a run of perfectly correct code."""
    both_compilers_on_path(monkeypatch)
    runner = FakeRunner(a_host_where_every_detector_works)

    first = resolve(cache_dir=None, runner=runner)
    spawned = len(runner.calls)
    second = resolve(cache_dir=None, runner=runner)

    # every probe ran a second time, so nothing was read back from anywhere
    assert len(runner.calls) == 2 * spawned
    assert list(first.workspace.iterdir()) == []
    shutil.rmtree(first.workspace)
    shutil.rmtree(second.workspace)


def test_the_cache_directory_the_caller_named_is_the_one_the_probes_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Probing costs six compiles and six runs. A cache_dir that stopped reaching probe_all
    would pay that on every start, with nothing on disk to show for it."""
    both_compilers_on_path(monkeypatch)
    cache_dir = tmp_path / "cache"
    runner = FakeRunner(a_host_where_every_detector_works)

    first = resolve(cache_dir=cache_dir, runner=runner)
    probed = probe_calls(runner)
    second = resolve(cache_dir=cache_dir, runner=runner)

    written = list(cache_dir.glob("*.json"))
    assert len(written) == 1
    # named after the machine fingerprint, which is a sha256 digest written as hex
    assert len(written[0].stem) == FINGERPRINT_LENGTH
    assert set(json.loads(written[0].read_text(encoding="utf-8"))) == {a.value for a in Analysis}
    # and the second start read it rather than compiling anything again
    assert probe_calls(runner) == probed
    shutil.rmtree(first.workspace)
    shutil.rmtree(second.workspace)


def test_a_server_that_asks_for_nothing_gets_the_probe_cache() -> None:
    """The default is the whole point of CACHE_DIR: a live start that forgot to ask must pay
    the six probes once per machine, not once per start. No test may exercise the default
    behaviorally -- it writes into the developer's real home directory -- so the promise is
    pinned on the signature, where a default quietly flipped to None is still visible."""
    default = inspect.signature(resolve).parameters["cache_dir"].default

    # spelled out rather than imported from context.py: a pin that reads CACHE_DIR back
    # from the code under test would follow the cache wherever a later edit moved it
    assert default == Path.home() / ".cache" / "cpp-analysis-mcp"


# ---------------------------------------------------------------------------- the workspace


def test_a_workspace_that_already_exists_is_used_as_it_stands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Restarting the server against a directory somebody chose must not empty it first."""
    both_compilers_on_path(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kept = workspace / "yesterdays-build.txt"
    kept.write_text("still here", encoding="utf-8")

    context = resolve(
        workspace=workspace,
        cache_dir=None,
        runner=FakeRunner(a_host_where_every_detector_works),
    )

    assert context.workspace == workspace
    assert kept.read_text(encoding="utf-8") == "still here"


def test_a_workspace_that_does_not_exist_yet_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured path is a promise, not a precondition. Left uncreated it fails later,
    inside a build, as a compiler error about an output directory nobody can place."""
    both_compilers_on_path(monkeypatch)
    workspace = tmp_path / "nested" / "workspace"

    context = resolve(
        workspace=workspace,
        cache_dir=None,
        runner=FakeRunner(a_host_where_every_detector_works),
    )

    assert context.workspace == workspace
    assert workspace.is_dir()


def test_a_relative_workspace_is_pinned_down_before_anything_can_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative path kept as it stands means "wherever this process happens to be", and the
    host that starts an MCP server decides that -- one os.chdir later, scratch() either fails
    or quietly builds somewhere nobody configured. Resolved once at startup, it cannot move."""
    both_compilers_on_path(monkeypatch)
    monkeypatch.chdir(tmp_path)

    context = resolve(
        workspace=Path("workspace"),
        cache_dir=None,
        runner=FakeRunner(a_host_where_every_detector_works),
    )

    assert context.workspace.is_absolute()
    assert context.workspace == tmp_path.resolve() / "workspace"


def test_a_workspace_that_is_a_file_says_which_path_is_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Configuration typos land here: a path that names a file cannot become a directory, and
    the bare FileExistsError mkdir raises for it names no setting and suggests nothing."""
    both_compilers_on_path(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.write_text("a regular file, not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError) as raised:
        resolve(
            workspace=workspace,
            cache_dir=None,
            runner=FakeRunner(a_host_where_every_detector_works),
        )

    assert str(workspace.resolve()) in str(raised.value)


def test_a_doomed_workspace_is_answered_before_anything_is_spawned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Startup's whole cost is spawns: two version queries and then six compiles and six runs.
    Whether the workspace can exist is knowable without any of them, so a start that cannot
    succeed has to end in milliseconds -- the runner here fails the test rather than spawn."""
    both_compilers_on_path(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.write_text("a regular file, not a directory", encoding="utf-8")

    with pytest.raises(RuntimeError):
        resolve(workspace=workspace, cache_dir=None, runner=RefusingRunner())


# --------------------------------------------------------------------- nothing to build with


def test_a_machine_with_no_compiler_says_which_ones_were_looked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup cannot continue without a compiler, and "no toolchain found" would leave the
    reader guessing what to install. The runner refuses to spawn: there is nothing to run."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError) as raised:
        resolve(cache_dir=None, runner=RefusingRunner())

    message = str(raised.value)
    # one literal, not one substring each: "g++" alone can never fail while "clang++" is
    # in the message, so the two names have to be pinned as the phrase they are written as
    assert "clang++ and g++" in message
    assert "install" in message
    # discovery also drops a compiler that is on PATH but will not answer --version, which is
    # exactly what a macOS without Command Line Tools has. Saying only "none found" there
    # calls the developer's own working `which clang++` a hallucination
    assert "version" in message


# ------------------------------------------------------------------ one build dir per call


def test_two_scratch_directories_never_collide(tmp_path: Path) -> None:
    """Concurrent requests each get their own place to build in. Sharing one would mean two
    calls overwriting each other's binaries, and each reporting on what the other compiled."""
    first = scratch(tmp_path)
    second = scratch(tmp_path)

    assert first != second
    assert first.is_dir()
    assert second.is_dir()
    assert first.parent == tmp_path
    assert second.parent == tmp_path


# --------------------------------------------------------------------- resolved once, shared


def test_the_capability_table_cannot_be_edited_through_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One context serves every request for the life of the process, so a table a caller
    could write to would let one request change what a later one is told this machine can do
    -- without anything having been probed."""
    both_compilers_on_path(monkeypatch)
    context = resolve(cache_dir=None, runner=FakeRunner(a_host_where_every_detector_works))
    denied = CapabilityStatus(available=False, reason="not probed, just asserted")

    with pytest.raises(TypeError):
        context.capabilities[Analysis.TSAN] = denied  # type: ignore[index]

    shutil.rmtree(context.workspace)
