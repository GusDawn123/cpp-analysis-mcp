"""Drive the whole sanitize chain with no compiler and no child process anywhere.

Only the subprocess boundary is faked -- capability gating, build composition, and parsing
are real code, and run-stage replies are committed goldens, never invented strings, so a
hand-written approximation can't keep passing after the real tool's output changes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from helpers import GOLDEN_DIR, bug_line

from cpp_analysis_mcp.pipelines.sanitize import analyze_file, analyze_project, analyze_snippet
from cpp_analysis_mcp.platforms.base import Platform
from cpp_analysis_mcp.process import RunResult
from cpp_analysis_mcp.store.models import Analysis, AnalysisReport, BuildFailure, CapabilityStatus
from cpp_analysis_mcp.toolchains.base import Toolchain

# ---------------------------------------------------------------- pinned expectations

# spelled through Path so the string compares equal to str(Path(...)) on Windows too
CLANG_PATH = str(Path("/usr/bin/clang++"))
CLANG_WARNING_FLAGS = ("-Wthread-safety",)
LINUX_COMPILE_EXTRAS = ("-pthread",)

# what a TSan binary must be run with; written out here, never read from PINNED_RUNTIME_ENV.
# A run that loses this reports nothing and the report says "clean".
TSAN_OPTIONS = "history_size=7 second_deadlock_stack=1 exitcode=66 detect_deadlocks=1"

# none of these may reach a sanitized run from the developer's shell
EVERY_SANITIZER_VAR = ("ASAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "UBSAN_OPTIONS")

# what a poisoned shell would have set them to: enough to change what a run reports
POISON = "verbosity=1:halt_on_error=0:detect_leaks=0"

RUN_TIMEOUT_S = 60

SOURCE_STEM = "data_race"

# the sanitized binary's name, pinned: a build that dropped the suffix would run whichever
# variant was compiled last, bound to the wrong runtime environment
TSAN_BINARY = "data_race.thread"
ASAN_BINARY = "data_race.address"

# the capability probe's own words, as capabilities.py phrases them for a working TSan
VERIFIED_BY = "compiled and ran a planted data race; ThreadSanitizer reported it"
# macOS's caveat: TSan runs, its deadlock detector does not
LIMITATION = (
    "Apple clang's TSan runtime cannot detect deadlocks or lock-order inversions; "
    "detect_deadlocks=1 is inert, so deadlock findings require Linux"
)

DENIED_REASON = "LeakSanitizer is Linux-only; it does not run on macOS arm64"
DENIED_SUGGESTION = "run the leak check on Linux, or on the roadmap's Linux-container mode"

# the goldens the fake replays, and what was read out of each by eye
TSAN_RACE_GOLDEN = "tsan_data_race.darwin-clang.txt"
TSAN_CLEAN_GOLDEN = "tsan_clean.darwin-clang.txt"
UBSAN_GOLDEN = "ubsan_signed_overflow.darwin-clang.txt"
ASAN_GOLDEN = "asan_heap_overflow.darwin-clang.txt"

RACE_CATEGORY = "data-race"
OVERFLOW_CATEGORY = "signed-integer-overflow"
HEAP_OVERFLOW_CATEGORY = "heap-buffer-overflow"

# TSAN_OPTIONS pins exitcode=66, so this is what a reporting TSan run exits with
TSAN_EXIT_CODE = 66
# SIGSEGV as a shell reports it: the program died and no sanitizer said a word
CRASH_EXIT_CODE = 139

THREAD_SAFETY_CATEGORY = "thread-safety-analysis"
THREAD_SAFETY_WARNING = (
    "data_race.cpp:21:5: warning: writing variable 'counter' requires holding "
    "mutex 'counter_mutex' exclusively [-Wthread-safety-analysis]\n"
)

SUCCESS = RunResult(exit_code=0, output="")
COMPILE_FAILED = RunResult(
    exit_code=1, output="data_race.cpp:12:9: error: expected ';'\n1 error generated.\n"
)


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
    def compiled(self) -> Spawn:
        """The build's spawn: always the first thing a pipeline reaches for."""
        assert self.spawns, "nothing was spawned at all"
        return self.spawns[0]

    @property
    def ran(self) -> Spawn:
        """The run's spawn: always the last, since parsing spawns nothing."""
        assert len(self.spawns) > 1, f"the run never happened: {self.spawns}"
        return self.spawns[-1]

    def env_of(self, spawn: Spawn) -> dict[str, str]:
        env = spawn.env
        assert env is not None, "a run must be given an environment, not the inherited one"
        return env


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

# the fixture project's one executable and where cmake says its binary lands
APP_NAME = "overflow_app"
APP_ARTIFACT = "bin/overflow_app"

# a second executable, so a test can prove the caller's chosen target is the one obeyed
OTHER_NAME = "helper_tool"
OTHER_ARTIFACT = "bin/helper_tool"

ONE_EXECUTABLE = ((APP_NAME, APP_ARTIFACT),)
TWO_EXECUTABLES = ((APP_NAME, APP_ARTIFACT), (OTHER_NAME, OTHER_ARTIFACT))


def write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def write_reply(build_dir: Path, executables: tuple[tuple[str, str], ...]) -> None:
    """Leave the index, codemodel and target files a real configure would have written.

    Reading these is how the build learns where the artifact lands, so the run's command
    can only be right if the whole chain read them.
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
    """A ScriptedRunner that also writes the File API reply when the configure goes by."""

    executables: tuple[tuple[str, str], ...] = ONE_EXECUTABLE

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
    """A clang nobody had to find: fields written out, detect() never called."""
    return Toolchain(
        family="clang",
        compiler=Path(CLANG_PATH),
        version="Apple clang version 21.0.0 (clang-2100.1.1.101)",
        warning_flags=CLANG_WARNING_FLAGS,
    )


def a_linux() -> Platform:
    """Linux's compile flag, exercised from macOS because the platform is an argument."""
    return Platform(name="linux", compile_extras=LINUX_COMPILE_EXTRAS)


def a_working_status() -> CapabilityStatus:
    """A probe that caught its planted bug, and the caveat that rides along on macOS."""
    return CapabilityStatus(available=True, verified_by=VERIFIED_BY, limitations=(LIMITATION,))


def a_denied_status() -> CapabilityStatus:
    return CapabilityStatus(available=False, reason=DENIED_REASON, suggestion=DENIED_SUGGESTION)


def statuses(status: CapabilityStatus) -> dict[Analysis, CapabilityStatus]:
    """The same status under every analysis, the compile-time ones included.

    THREAD_SAFETY and CLANG_TIDY are in here on purpose: asking this pipeline for one must
    fail on the sanitizer lookup, not on a capability the test forgot to write down.
    """
    return dict.fromkeys(Analysis, status)


def a_source(tmp_path: Path) -> Path:
    source = tmp_path / f"{SOURCE_STEM}.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")
    return source


def build_dir(tmp_path: Path) -> Path:
    return tmp_path / "build"


def analyze(
    tmp_path: Path,
    runner: ScriptedRunner | RefusingRunner,
    *,
    analysis: Analysis = Analysis.TSAN,
    capabilities: Mapping[Analysis, CapabilityStatus] | None = None,
) -> AnalysisReport | BuildFailure | CapabilityStatus:
    """Run the pipeline against the fake, with the arguments the tests vary."""
    return analyze_file(
        a_source(tmp_path),
        analysis,
        toolchain=a_clang(),
        platform=a_linux(),
        capabilities=statuses(a_working_status()) if capabilities is None else capabilities,
        build_dir=build_dir(tmp_path),
        runner=runner,
    )


def reported(result: AnalysisReport | BuildFailure | CapabilityStatus) -> AnalysisReport:
    assert isinstance(result, AnalysisReport), f"expected an AnalysisReport, got {result}"
    return result


# ------------------------------------------------------------------------- the whole chain


def test_a_tsan_run_becomes_the_finding_its_golden_holds(tmp_path: Path) -> None:
    """Build, run, parse: what comes back is what ThreadSanitizer actually printed."""
    runner = ScriptedRunner(
        [SUCCESS, RunResult(exit_code=TSAN_EXIT_CODE, output=golden(TSAN_RACE_GOLDEN))]
    )

    report = reported(analyze(tmp_path, runner))

    assert report.analysis is Analysis.TSAN
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.tool == "tsan"
    assert finding.category == RACE_CATEGORY
    assert finding.location is not None
    # the golden blames the line the fixture marks as its planted bug
    assert finding.location.line == bug_line(SOURCE_STEM)
    assert report.exit_code == TSAN_EXIT_CODE
    assert report.timed_out is False


def test_the_report_carries_what_made_the_detector_trustworthy(tmp_path: Path) -> None:
    """The probe's proof and caveat travel with the findings; looked up later they get lost."""
    runner = ScriptedRunner(
        [SUCCESS, RunResult(exit_code=TSAN_EXIT_CODE, output=golden(TSAN_RACE_GOLDEN))]
    )

    report = reported(analyze(tmp_path, runner))

    assert report.verified_by == VERIFIED_BY
    assert report.limitations == (LIMITATION,)


# --------------------------------------------------------------- the run's environment


def test_the_run_gets_the_options_its_build_chose_and_nothing_from_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false all-clear in one test: a TSan run without TSAN_OPTIONS reports nothing.

    All four variables are poisoned in this process first, so this proves the pipeline
    replaced them rather than that they happened to be absent.
    """
    for name in EVERY_SANITIZER_VAR:
        monkeypatch.setenv(name, POISON)
    runner = ScriptedRunner([SUCCESS, RunResult(exit_code=TSAN_EXIT_CODE, output="")])

    analyze(tmp_path, runner)

    env = runner.env_of(runner.ran)
    assert env["TSAN_OPTIONS"] == TSAN_OPTIONS
    leaked = [name for name in EVERY_SANITIZER_VAR if name != "TSAN_OPTIONS" and name in env]
    assert leaked == [], f"the shell's own options survived into the run: {leaked}"


def test_the_run_keeps_the_rest_of_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hygiene means replacing the sanitizer variables, not starting from an empty world.

    The run still needs PATH and friends: on Linux the TSan runtime finds llvm-symbolizer
    through them, and without it every frame in a report is a raw address.
    """
    monkeypatch.setenv("CPP_ANALYSIS_TEST_CANARY", "still-here")
    runner = ScriptedRunner([SUCCESS, RunResult(exit_code=TSAN_EXIT_CODE, output="")])

    analyze(tmp_path, runner)

    assert runner.env_of(runner.ran).get("CPP_ANALYSIS_TEST_CANARY") == "still-here"


def test_an_asan_run_pins_no_options_and_still_strips_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASan's goldens were captured on its defaults, so its pinned table is empty.

    Empty is not "leave the shell alone": the developer's ASAN_OPTIONS could turn off the
    very check being asked for, so all four still have to go.
    """
    for name in EVERY_SANITIZER_VAR:
        monkeypatch.setenv(name, POISON)
    runner = ScriptedRunner([SUCCESS, RunResult(exit_code=1, output=golden(ASAN_GOLDEN))])

    analyze(tmp_path, runner, analysis=Analysis.ASAN)

    env = runner.env_of(runner.ran)
    leaked = [name for name in EVERY_SANITIZER_VAR if name in env]
    assert leaked == [], f"the shell's own options survived into the run: {leaked}"


# ------------------------------------------------------------------- what stops the chain


def test_an_unavailable_capability_comes_back_untouched_and_spawns_nothing(
    tmp_path: Path,
) -> None:
    """Running a detector the probe could not verify would report silence as cleanliness."""
    status = a_denied_status()

    result = analyze(
        tmp_path,
        RefusingRunner(),
        analysis=Analysis.LSAN,
        capabilities=statuses(status),
    )

    # the same object, so the reason and suggestion cannot be reworded on the way through
    assert result is status


def test_a_build_that_failed_is_the_answer_and_the_run_never_happens(tmp_path: Path) -> None:
    """There is no binary to run, and a report built from nothing would say "clean"."""
    runner = ScriptedRunner([COMPILE_FAILED])

    result = analyze(tmp_path, runner)

    assert isinstance(result, BuildFailure), f"expected a BuildFailure, got {result}"
    assert result.stage == "compile"
    assert len(runner.spawns) == 1, f"the run happened anyway: {runner.spawns}"


def test_a_non_sanitizer_analysis_is_a_caller_bug(tmp_path: Path) -> None:
    """-Wthread-safety reports while compiling; there is no sanitized binary to run."""
    with pytest.raises(KeyError):
        analyze(tmp_path, RefusingRunner(), analysis=Analysis.THREAD_SAFETY)


# ------------------------------------------------------------------ what a run can report


def test_a_clean_run_is_an_honest_empty_report(tmp_path: Path) -> None:
    """The golden is an empty file: a verified TSan ran and had nothing to say."""
    runner = ScriptedRunner([SUCCESS, RunResult(exit_code=0, output=golden(TSAN_CLEAN_GOLDEN))])

    report = reported(analyze(tmp_path, runner))

    assert report.findings == ()
    assert report.exit_code == 0
    assert report.timed_out is False
    # what makes the emptiness worth anything
    assert report.verified_by == VERIFIED_BY


def test_a_crash_with_no_report_is_still_a_fact(tmp_path: Path) -> None:
    """The sanitizer said nothing and the program died; the exit status is the finding."""
    runner = ScriptedRunner([SUCCESS, RunResult(exit_code=CRASH_EXIT_CODE, output="")])

    report = reported(analyze(tmp_path, runner))

    assert report.findings == ()
    assert report.exit_code == CRASH_EXIT_CODE
    assert report.timed_out is False


def test_ubsan_reports_while_exiting_zero(tmp_path: Path) -> None:
    """Exit 0 does not mean there was nothing to read: UBSan prints and carries on."""
    runner = ScriptedRunner([SUCCESS, RunResult(exit_code=0, output=golden(UBSAN_GOLDEN))])

    report = reported(analyze(tmp_path, runner, analysis=Analysis.UBSAN))

    assert report.exit_code == 0
    assert len(report.findings) == 1
    assert report.findings[0].tool == "ubsan"
    assert report.findings[0].category == OVERFLOW_CATEGORY


def test_a_run_killed_at_its_timeout_still_reports_what_it_printed(tmp_path: Path) -> None:
    """A hang after the race was already reported must not throw the report away."""
    killed = RunResult(
        exit_code=None,
        output=golden(TSAN_RACE_GOLDEN) + f"\n[killed after {RUN_TIMEOUT_S}s timeout]\n",
    )
    runner = ScriptedRunner([SUCCESS, killed])

    report = reported(analyze(tmp_path, runner))

    assert len(report.findings) == 1
    assert report.findings[0].category == RACE_CATEGORY
    assert report.timed_out is True
    assert report.exit_code is None


# ----------------------------------------------------------------------- what gets spawned


def test_the_run_invokes_the_sanitized_binary_from_the_build_directory(tmp_path: Path) -> None:
    """The suffixed name and the working directory are both what the build handed back."""
    runner = ScriptedRunner([SUCCESS, SUCCESS])

    analyze(tmp_path, runner)

    assert runner.ran.cmd == [str(build_dir(tmp_path) / TSAN_BINARY)]
    assert runner.ran.cwd == build_dir(tmp_path)
    assert runner.ran.timeout_s == RUN_TIMEOUT_S


def test_an_asan_run_invokes_its_own_variant_of_the_binary(tmp_path: Path) -> None:
    """Two sanitized builds of one source coexist, so the run must name the right one."""
    runner = ScriptedRunner([SUCCESS, SUCCESS])

    analyze(tmp_path, runner, analysis=Analysis.ASAN)

    assert runner.ran.cmd == [str(build_dir(tmp_path) / ASAN_BINARY)]


def test_the_build_step_still_composes_a_real_compile(tmp_path: Path) -> None:
    """Nothing is stubbed between here and the compiler: the argv is the primitive's own."""
    runner = ScriptedRunner([SUCCESS, SUCCESS])

    analyze(tmp_path, runner)

    assert runner.compiled.cmd == [
        CLANG_PATH,
        "-std=c++20",
        "-g",
        "-O1",
        "-fno-omit-frame-pointer",
        "-fsanitize=thread",
        *CLANG_WARNING_FLAGS,
        *LINUX_COMPILE_EXTRAS,
        str(tmp_path / f"{SOURCE_STEM}.cpp"),
        "-o",
        str(build_dir(tmp_path) / TSAN_BINARY),
    ]


# ------------------------------------------------------------------------ build warnings


def test_the_builds_own_findings_travel_beside_the_runs(tmp_path: Path) -> None:
    """-Wthread-safety fires while compiling, so one analysis can produce both kinds."""
    runner = ScriptedRunner(
        [
            RunResult(exit_code=0, output=THREAD_SAFETY_WARNING),
            RunResult(exit_code=TSAN_EXIT_CODE, output=golden(TSAN_RACE_GOLDEN)),
        ]
    )

    report = reported(analyze(tmp_path, runner))

    assert len(report.build_warnings) == 1
    assert report.build_warnings[0].category == THREAD_SAFETY_CATEGORY
    assert report.build_warnings[0].tool == "compiler"
    # kept apart from what the run saw: the compiler and the sanitizer found different things
    assert [finding.category for finding in report.findings] == [RACE_CATEGORY]


# ------------------------------------------------------------------------------ snippets


def test_a_snippet_is_written_down_before_it_is_analyzed(tmp_path: Path) -> None:
    """The file has to survive the call: every frame in the report names it."""
    text = "int main() { return 0; }\n"
    runner = ScriptedRunner(
        [SUCCESS, RunResult(exit_code=TSAN_EXIT_CODE, output=golden(TSAN_RACE_GOLDEN))]
    )

    report = reported(
        analyze_snippet(
            text,
            Analysis.TSAN,
            toolchain=a_clang(),
            platform=a_linux(),
            capabilities=statuses(a_working_status()),
            build_dir=build_dir(tmp_path),
            runner=runner,
        )
    )

    snippet = build_dir(tmp_path) / "snippet.cpp"
    assert snippet.read_text(encoding="utf-8") == text
    assert str(snippet) in runner.compiled.cmd
    assert runner.ran.cmd == [str(build_dir(tmp_path) / "snippet.thread")]
    assert [finding.category for finding in report.findings] == [RACE_CATEGORY]


# ------------------------------------------------------------------------------ projects


def test_a_project_configures_builds_and_then_runs_what_cmake_named(tmp_path: Path) -> None:
    """Three spawns, and the last one is the path the File API reply pointed at."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    runner = ScriptedCmake([SUCCESS, SUCCESS, RunResult(exit_code=1, output=golden(ASAN_GOLDEN))])

    report = reported(
        analyze_project(
            project_dir,
            Analysis.ASAN,
            toolchain=a_clang(),
            platform=a_linux(),
            capabilities=statuses(a_working_status()),
            build_dir=build_dir(tmp_path),
            runner=runner,
        )
    )

    assert len(runner.spawns) == 3, f"expected configure, build, run: {runner.spawns}"
    assert runner.spawns[0].cmd[:2] == ["cmake", "-S"]
    assert runner.spawns[1].cmd[:2] == ["cmake", "--build"]
    assert runner.ran.cmd == [str(build_dir(tmp_path) / APP_ARTIFACT)]
    assert runner.ran.cwd == build_dir(tmp_path)
    assert [finding.category for finding in report.findings] == [HEAP_OVERFLOW_CATEGORY]
    assert report.verified_by == VERIFIED_BY


def test_the_callers_chosen_target_is_the_one_built_and_run(tmp_path: Path) -> None:
    """With two executables the caller's word decides which one is analyzed.

    A pipeline that dropped the target on the way down would come back asking the caller
    to choose from the very list the caller already chose from.
    """
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    runner = ScriptedCmake(
        [SUCCESS, SUCCESS, RunResult(exit_code=1, output=golden(ASAN_GOLDEN))],
        executables=TWO_EXECUTABLES,
    )

    report = reported(
        analyze_project(
            project_dir,
            Analysis.ASAN,
            toolchain=a_clang(),
            platform=a_linux(),
            capabilities=statuses(a_working_status()),
            build_dir=build_dir(tmp_path),
            target=OTHER_NAME,
            runner=runner,
        )
    )

    built = runner.spawns[1].cmd
    assert built[built.index("--target") + 1] == OTHER_NAME
    assert runner.ran.cmd == [str(build_dir(tmp_path) / OTHER_ARTIFACT)]
    assert [finding.category for finding in report.findings] == [HEAP_OVERFLOW_CATEGORY]


def test_a_project_whose_capability_is_denied_spawns_no_cmake(tmp_path: Path) -> None:
    status = a_denied_status()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = analyze_project(
        project_dir,
        Analysis.LSAN,
        toolchain=a_clang(),
        platform=a_linux(),
        capabilities=statuses(status),
        build_dir=build_dir(tmp_path),
        runner=RefusingRunner(),
    )

    assert result is status
