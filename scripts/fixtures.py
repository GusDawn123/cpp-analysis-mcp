#!/usr/bin/env python3
"""Compile and run the C++ bug fixtures under sanitizers.

    validate    check every fixture still produces the output it is supposed to
    capture     save raw sanitizer output into tests/fixtures/golden/

`validate` doubles as the CI integration job. Stdlib only and no imports from
src/ -- this script predates the package and runs bare in CI.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import platform
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CPP_DIR = REPO_ROOT / "tests" / "fixtures" / "cpp"
BUILD_DIR = REPO_ROOT / "tests" / "fixtures" / "build"
DEFAULT_OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "golden"

RUN_TIMEOUT_S = 30

# how long the cleanup after a timeout may itself take: the tree kill and the final read
# of the pipe are each bound by this, because neither is guaranteed to finish on its own
KILL_GRACE_S = 10

# Where LLVM on Windows keeps the sanitizer runtimes, one directory per clang version.
# Two measured quirks live there: UBSan's libraries must be linked by full path (the
# linker otherwise takes MSVC's incompatible copy, searched first), and ASan's runtime
# is a DLL the loader only finds beside the binary.
LLVM_ROOT = Path(r"C:\Program Files\LLVM")
UBSAN_LIBS = (
    "clang_rt.ubsan_standalone-x86_64.lib",
    "clang_rt.ubsan_standalone_cxx-x86_64.lib",
)
ASAN_DLL = "clang_rt.asan_dynamic-x86_64.dll"

# Pin the sanitizer runtime options so goldens match what validation saw.
# ASAN_OPTIONS is deliberately absent -- ASan runs on its defaults.
PINNED_ENV = {
    "TSAN_OPTIONS": ("history_size=7 second_deadlock_stack=1 exitcode=66 detect_deadlocks=1"),
    "UBSAN_OPTIONS": "print_stacktrace=1",
}

# Any of these appearing in clean.cpp's output means something is wrong.
SANITIZER_MARKERS = (
    "WARNING: ThreadSanitizer",
    "ERROR: AddressSanitizer",
    "runtime error:",
)


@dataclass(frozen=True)
class Case:
    """One fixture compiled and run under one sanitizer."""

    fixture: str  # source stem in tests/fixtures/cpp/
    sanitizer: str  # value for -fsanitize=
    tool: str  # short name used in golden filenames
    expect: tuple[str, ...] = ()  # substrings that must appear in the output
    forbid: tuple[str, ...] = ()  # substrings that must not appear
    platforms: frozenset[str] = frozenset({"darwin", "linux"})
    skip_reason: str = ""
    # Controls must actually run: a crashed program prints no forbidden
    # markers, and that silence must not count as a pass.
    require_exit_zero: bool = False


# Ground truth: which fixture proves which tool still works, and where.
SUPPORT: list[Case] = [
    Case(
        fixture="data_race",
        sanitizer="thread",
        tool="tsan",
        expect=("WARNING: ThreadSanitizer: data race",),
        skip_reason="no compiler ships a ThreadSanitizer runtime for Windows",
    ),
    Case(
        fixture="race_with_lock",
        sanitizer="thread",
        tool="tsan",
        # The second marker guarantees goldens carry mutex annotations, which
        # the tsan parser's locks_held extraction is tested against.
        expect=("WARNING: ThreadSanitizer: data race", "(mutexes:"),
        skip_reason="no compiler ships a ThreadSanitizer runtime for Windows",
    ),
    Case(
        fixture="deadlock",
        sanitizer="thread",
        tool="tsan",
        expect=("WARNING: ThreadSanitizer: lock-order-inversion",),
        platforms=frozenset({"linux"}),
        skip_reason=(
            "TSan's deadlock detector is inert in Apple clang's macOS runtime; "
            "detect_deadlocks=1 has no effect and even a plain double-lock "
            "hangs unreported"
        ),
    ),
    Case(
        fixture="heap_overflow",
        sanitizer="address",
        tool="asan",
        expect=("ERROR: AddressSanitizer: heap-buffer-overflow",),
        platforms=frozenset({"darwin", "linux", "windows"}),
    ),
    Case(
        fixture="use_after_free",
        sanitizer="address",
        tool="asan",
        expect=("ERROR: AddressSanitizer: heap-use-after-free",),
        platforms=frozenset({"darwin", "linux", "windows"}),
    ),
    Case(
        fixture="leak",
        sanitizer="leak",
        tool="lsan",
        expect=("detected memory leaks",),
        platforms=frozenset({"linux"}),
        skip_reason="LeakSanitizer is Linux-only; it runs on neither macOS arm64 nor Windows",
    ),
    Case(
        fixture="signed_overflow",
        sanitizer="undefined",
        tool="ubsan",
        expect=("runtime error: signed integer overflow",),
        platforms=frozenset({"darwin", "linux", "windows"}),
    ),
    # The control: three runs that must all come back silent AND exit 0.
    Case(
        fixture="clean",
        sanitizer="thread",
        tool="tsan",
        forbid=SANITIZER_MARKERS,
        require_exit_zero=True,
        skip_reason="no compiler ships a ThreadSanitizer runtime for Windows",
    ),
    Case(
        fixture="clean",
        sanitizer="address",
        tool="asan",
        forbid=SANITIZER_MARKERS,
        require_exit_zero=True,
        platforms=frozenset({"darwin", "linux", "windows"}),
    ),
    Case(
        fixture="clean",
        sanitizer="undefined",
        tool="ubsan",
        forbid=SANITIZER_MARKERS,
        require_exit_zero=True,
        platforms=frozenset({"darwin", "linux", "windows"}),
    ),
]


def current_os() -> str:
    """Return darwin, linux or windows."""
    return platform.system().lower()


def compiler_family(compiler: str) -> str:
    """Map a compiler command to gcc or clang; anything not g++ counts as clang."""
    name = Path(compiler).name
    # "g++" is a substring of "clang++", so rule clang out first.
    is_gcc = "clang" not in name and ("g++" in name or "gcc" in name)
    return "gcc" if is_gcc else "clang"


def run_env() -> dict[str, str]:
    """Build the child environment: inherited, plus pinned sanitizer options."""
    env = dict(os.environ)
    # Drop developer shell settings that would change sanitizer output.
    env.pop("ASAN_OPTIONS", None)
    env.pop("LSAN_OPTIONS", None)
    env.update(PINNED_ENV)
    return env


def run_command(
    cmd: list[str], env: dict[str, str], timeout: int = RUN_TIMEOUT_S
) -> tuple[int | None, str]:
    """Run a command with stderr merged into stdout.

    Returns (exit code, output); the code is None when the run timed out.
    """
    # New session so a hung sanitizer takes its symbolizer child down with it.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # POSIX: start_new_session made the child its own group leader, so its pid is
        # the pgid; it may also exit on its own between the timeout and the kill.
        # Windows has no groups to signal, so taskkill /T walks the tree instead.
        # sys.platform rather than os.name because mypy only narrows the former.
        if sys.platform == "win32":
            # by full path: bare "taskkill" is resolved through a search that can
            # reach the current directory, which this script does not control.
            # Bounded, so the cleanup cannot hang the way its target did.
            taskkill = (
                Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
            )
            with contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run(
                    [str(taskkill), "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                    timeout=KILL_GRACE_S,
                )
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        # The kill is not guaranteed (an elevated child, a grandchild in its own
        # session); a survivor holds the pipe open, so the final read is bound too and
        # the root is killed directly when it expires.
        try:
            output, _ = proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired as undead:
            proc.kill()
            # bytes on POSIX, absent on Windows, str only when run() re-raises it
            leftover = undead.output
            salvaged = (
                leftover.decode(errors="replace")
                if isinstance(leftover, bytes)
                else (leftover if isinstance(leftover, str) else "")
            )
            return None, (
                salvaged + f"\n[killed after {timeout}s timeout; parts of its process tree"
                " may have survived]\n"
            )
        return None, (output or "") + f"\n[killed after {timeout}s timeout]\n"
    return proc.returncode, output


def binary_path(case: Case) -> Path:
    # Windows will only execute a file that ends in .exe
    suffix = ".exe" if current_os() == "windows" else ""
    return BUILD_DIR / f"{case.fixture}_{case.tool}{suffix}"


def windows_runtime_dir() -> Path | None:
    """Return the newest clang runtime directory of a Windows LLVM install, or None."""
    versioned = [
        (int(path.parts[-3]), path)
        for path in LLVM_ROOT.glob("lib/clang/*/lib/windows")
        if path.parts[-3].isdigit()
    ]
    return max(versioned)[1] if versioned else None


def compile_case(case: Case, compiler: str) -> tuple[bool, str]:
    """Compile one fixture under its sanitizer. Returns (ok, compiler output)."""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        compiler,
        "-std=c++20",
        "-g",
        "-O1",
        "-fno-omit-frame-pointer",
        f"-fsanitize={case.sanitizer}",
    ]
    if current_os() == "linux":
        cmd.append("-pthread")
    cmd += [str(CPP_DIR / f"{case.fixture}.cpp"), "-o", str(binary_path(case))]
    runtime = windows_runtime_dir() if current_os() == "windows" else None
    if runtime is not None and case.sanitizer == "undefined":
        # by full path, or the link takes MSVC's incompatible copy of the runtime
        cmd += [str(runtime / lib) for lib in UBSAN_LIBS]
    code, output = run_command(cmd, run_env(), timeout=120)
    if code == 0 and runtime is not None and case.sanitizer == "address":
        # beside the binary, the one place the Windows loader always looks
        shutil.copy2(runtime / ASAN_DLL, BUILD_DIR)
    return code == 0, output


def check_output(case: Case, output: str) -> str:
    """Return an empty string if the output matches expectations, else why not."""
    for marker in case.expect:
        if marker not in output:
            return f"missing expected marker {marker!r}"
    for marker in case.forbid:
        if marker in output:
            return f"unexpected sanitizer output {marker!r}"
    return ""


def build_and_run(case: Case, compiler: str) -> tuple[str, str]:
    """Compile then run one case. Returns (program output, failure reason)."""
    ok, compile_output = compile_case(case, compiler)
    if not ok:
        return "", "compile failed: " + excerpt(compile_output)
    code, output = run_command([str(binary_path(case))], run_env())
    # A marker printed before a hang must not count as a pass.
    if code is None:
        return output, "run timed out: " + excerpt(output)
    if case.require_exit_zero and code != 0:
        return output, f"expected exit 0, got {code}: " + excerpt(output)
    return output, ""


def excerpt(text: str, lines: int = 4) -> str:
    """Flatten the first few lines of tool output onto one line."""
    kept = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " | ".join(kept[:lines]) or "(no output)"


def supported(case: Case) -> bool:
    return current_os() in case.platforms


def label(case: Case) -> str:
    return f"{case.fixture:<16} {case.sanitizer:<10}"


def cmd_validate(args: argparse.Namespace) -> int:
    """Check every case still behaves as documented. Nonzero exit on any FAIL."""
    passed = skipped = failed = 0
    print(f"validating {len(SUPPORT)} cases on {current_os()} with {args.compiler}\n")

    for case in SUPPORT:
        if not supported(case):
            skipped += 1
            print(f"SKIP  {label(case)} {case.skip_reason}")
            continue

        output, reason = build_and_run(case, args.compiler)
        if not reason:
            reason = check_output(case, output)

        if reason:
            failed += 1
            print(f"FAIL  {label(case)} {reason}")
        else:
            passed += 1
            print(f"PASS  {label(case)}")

    print(f"\n{passed} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


def golden_name(case: Case, compiler: str) -> str:
    return f"{case.tool}_{case.fixture}.{current_os()}-{compiler_family(compiler)}.txt"


def scrubbed(output: str) -> str:
    """Drop this checkout's own location from output that is about to be committed.

    Sanitizer frames carry absolute paths, and those begin at wherever the capturing
    machine keeps the repo -- which on Windows includes a username. The machine-specific
    head is replaced with the neutral root the Linux goldens already carry (/w, C:\\w),
    keeping the tail that parsers and readers actually use.
    """
    neutral = r"C:\w" if current_os() == "windows" else "/w"
    return output.replace(str(REPO_ROOT), neutral)


def cmd_capture(args: argparse.Namespace) -> int:
    """Write raw sanitizer output to the golden directory."""
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = skipped = failed = 0
    print(f"capturing into {out_dir}\n")

    for case in SUPPORT:
        if not supported(case):
            # Never overwrite a golden captured on a platform that supports it.
            skipped += 1
            print(f"SKIP  {label(case)} {case.skip_reason}")
            continue

        output, reason = build_and_run(case, args.compiler)
        if reason:
            failed += 1
            print(f"FAIL  {label(case)} {reason}")
            continue

        target = out_dir / golden_name(case, args.compiler)
        target.write_text(scrubbed(output))
        written += 1
        print(f"WROTE {label(case)} {target.name} ({len(output)} bytes)")

    print(f"\n{written} written, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", default="clang++", help="C++ compiler to use")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="check fixtures against expected output")

    capture = sub.add_parser("capture", help="write golden output files")
    capture.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))

    args = parser.parse_args(argv)

    if shutil.which(args.compiler) is None:
        print(f"compiler not found: {args.compiler}", file=sys.stderr)
        return 2

    if args.command == "validate":
        return cmd_validate(args)
    return cmd_capture(args)


if __name__ == "__main__":
    sys.exit(main())
