"""Enforce the layer rules from docs/architecture.md by reading the source tree.

Each of the four rules is checked by parsing the real files under src/cpp_analysis_mcp
with ast, so a violation fails a test instead of relying on review discipline. One
narrower rule rides along: subprocess belongs to process.py alone. Most of the package
is still docstring stubs; these tests are the ratchet for what gets added.

Rule 3 is checked from its exception inwards. Something has to call platforms.detect(),
and the rule is only worth anything while that something stays singular, so the tests
below name the one sanctioned caller and refuse every other -- including the second-hand
version, a pipeline importing the composition root to resolve a context of its own.
"""

from __future__ import annotations

import ast
import fnmatch
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpers import PACKAGE_DIR, SRC_DIR

PACKAGE_NAME = PACKAGE_DIR.name
PIPELINES = f"{PACKAGE_NAME}.pipelines"

# the layers below pipelines: they may be imported by a pipeline, never the reverse
PRIMITIVE_PACKAGES = ("build", "parsers", "platforms", "toolchains")
# primitives that are single modules rather than packages, so rglob does not reach them
TOP_LEVEL_PRIMITIVES = (
    PACKAGE_DIR / "capabilities.py",
    PACKAGE_DIR / "context.py",
    PACKAGE_DIR / "fingerprints.py",
    PACKAGE_DIR / "process.py",
    PACKAGE_DIR / "wsl.py",
)

SERVER = PACKAGE_DIR / "server.py"
PROCESS = PACKAGE_DIR / "process.py"
# the composition root: the one module allowed to ask this machine what it is
CONTEXT = PACKAGE_DIR / "context.py"
PLATFORMS_INIT = PACKAGE_DIR / "platforms" / "__init__.py"

CONTEXT_MODULE = f"{PACKAGE_NAME}.context"
DETECT = f"{PACKAGE_NAME}.platforms.detect"

CONTROL_FLOW_NODES = (
    ast.If,
    ast.IfExp,
    # `or` and `and` branch as surely as an `if`, and read as plumbing while doing it:
    # `checks=checks or "bugprone-*"` is a handler deciding a default the caller left out
    ast.BoolOp,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.TryStar,
    ast.With,
    ast.AsyncWith,
    ast.Match,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)

IMPURE_ATTRIBUTES = ("os.system", "os.popen")


def modules_in(*package_names: str) -> list[Path]:
    """Return every .py file under the named subpackages, nested ones included."""
    found: list[Path] = []
    for name in package_names:
        found.extend(sorted((PACKAGE_DIR / name).rglob("*.py")))
    return found


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def package_of(path: Path) -> str:
    """Return the package a module's relative imports resolve against."""
    parts = path.relative_to(SRC_DIR).parts
    return ".".join(parts[:-1])


def import_targets(node: ast.stmt, package: str) -> list[str]:
    """Return the dotted names one import statement pulls in, relative forms resolved.

    `from ..pipelines import sanitize` inside cpp_analysis_mcp.build yields both
    cpp_analysis_mcp.pipelines and cpp_analysis_mcp.pipelines.sanitize, since the
    imported name may be either a submodule or an attribute of the package.
    """
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []

    base = node.module or ""
    if node.level:
        parts = package.split(".")
        prefix = parts[: len(parts) - node.level + 1]
        base = ".".join([*prefix, base]) if base else ".".join(prefix)
    if not base:
        return [alias.name for alias in node.names]
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def imports_of(tree: ast.Module, package: str) -> Iterator[tuple[ast.stmt, list[str]]]:
    """Yield each import statement in a module with the dotted names it reaches."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield node, import_targets(node, package)


def reaches(target: str, package: str) -> bool:
    """Report whether a dotted name is that package or something inside it."""
    return target == package or target.startswith(f"{package}.")


def platform_lookups(tree: ast.Module) -> Iterator[ast.Attribute]:
    """Yield every reference to platforms.detect, spelled bare or fully qualified.

    An aliased import (`import ...platforms as p`) could still slip past; the ratchet is
    aimed at honest drift in the spellings this package actually uses, not at evasion.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "detect"
            and _is_platforms(_dotted(node.value))
        ):
            yield node


def _dotted(node: ast.expr) -> str:
    """Flatten a Name/Attribute chain to its dotted text; anything else flattens to ""."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def _is_platforms(dotted: str) -> bool:
    """Match the platforms module as a whole trailing segment, so `myplatforms` cannot."""
    return dotted == "platforms" or dotted.endswith(".platforms")


def test_layer_packages_exist() -> None:
    """Guard the rules below: a renamed directory would make them pass vacuously."""
    expected = (*PRIMITIVE_PACKAGES, "pipelines")
    missing = [name for name in expected if not (PACKAGE_DIR / name).is_dir()]
    assert not missing, f"missing layer packages under {PACKAGE_DIR}: {missing}"
    assert SERVER.is_file(), f"missing {SERVER}"
    assert modules_in(*expected), f"no python modules found under {PACKAGE_DIR}"


def test_every_layer_directory_under_tests_is_collected(pytestconfig: pytest.Config) -> None:
    """pytest skips directories named "build" by default, and tests/unit/build/ is one.

    Left at the default the build tests are never collected: no failures, no count, green.
    A test that cannot run is worse than one that fails, so the exclusion is pinned here.
    """
    excluded = list(pytestconfig.getini("norecursedirs"))
    # the entries are fnmatch patterns, so "build*" would skip the directory as surely
    # as a literal "build" -- match the name against each, don't look for the string
    matching = [pattern for pattern in excluded if fnmatch.fnmatchcase("build", pattern)]

    assert not matching, f"tests/unit/build/ would not be collected: {matching} in {excluded}"


def test_primitives_do_not_import_pipelines() -> None:
    """Rule 1: layers only point downward -- no primitive imports an orchestrator."""
    violations: list[str] = []
    for path in [*modules_in(*PRIMITIVE_PACKAGES), *TOP_LEVEL_PRIMITIVES]:
        package = package_of(path)
        for node, targets in imports_of(parse(path), package):
            offending = [target for target in targets if reaches(target, PIPELINES)]
            if offending:
                violations.append(f"{path}:{node.lineno} imports {offending[0]}")

    assert not violations, "primitives must not import pipelines:\n" + "\n".join(violations)


def test_pipelines_do_not_import_each_other() -> None:
    """Rule 1, second half: a pipeline composes primitives, never another pipeline."""
    violations: list[str] = []
    # __init__.py is exempt: importing a submodule there is re-export, not one
    # pipeline calling another.
    for path in modules_in("pipelines"):
        if path.name == "__init__.py":
            continue
        own_module = f"{PIPELINES}.{path.stem}"
        package = package_of(path)
        for node, targets in imports_of(parse(path), package):
            offending = [
                target
                for target in targets
                if reaches(target, PIPELINES)
                and target != PIPELINES
                and not reaches(target, own_module)
            ]
            if offending:
                violations.append(f"{path}:{node.lineno} imports sibling {offending[0]}")

    assert not violations, "no pipeline may import another pipeline:\n" + "\n".join(violations)


def test_server_has_no_control_flow() -> None:
    """Rule 2: handlers declare inputs and delegate; an `if` means logic leaked up."""
    violations = [
        f"{SERVER}:{node.lineno} contains {type(node).__name__}"
        for node in ast.walk(parse(SERVER))
        if isinstance(node, CONTROL_FLOW_NODES)
    ]

    assert not violations, "server.py must hold no control flow:\n" + "\n".join(violations)


def test_only_the_composition_root_looks_the_platform_up() -> None:
    """Rule 3's sanctioned exception, made structural rather than remembered.

    Something has to call platforms.detect(), and context.resolve() is that something. A
    second caller is how the rule erodes: code that looks the platform up itself always
    finds this machine, so the Linux behaviour it decides can never be tested from here.
    """
    violations: list[str] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        # the module that defines detect(), and the one module sanctioned to call it
        if path in (PLATFORMS_INIT, CONTEXT):
            continue
        tree = parse(path)
        for node, targets in imports_of(tree, package_of(path)):
            if DETECT in targets:
                violations.append(f"{path}:{node.lineno} imports {DETECT}")
        violations.extend(
            f"{path}:{node.lineno} references platforms.detect" for node in platform_lookups(tree)
        )

    assert not violations, "only context.py may look the platform up:\n" + "\n".join(violations)


def test_no_pipeline_resolves_its_own_context() -> None:
    """Rule 3 second-hand: a pipeline that built its own Context would be looking the platform
    up through the composition root, and would need a whole host where it is handed a Platform
    and a Toolchain today -- the same violation, one layer further from the test that names it.
    """
    violations: list[str] = []
    for path in modules_in("pipelines"):
        for node, targets in imports_of(parse(path), package_of(path)):
            if any(reaches(target, CONTEXT_MODULE) for target in targets):
                violations.append(f"{path}:{node.lineno} imports {CONTEXT_MODULE}")

    assert not violations, f"no pipeline may import {CONTEXT_MODULE}:\n" + "\n".join(violations)


def test_parsers_are_pure() -> None:
    """Rule 4: text in, findings out -- no subprocess, no filesystem."""
    violations: list[str] = []
    for path in modules_in("parsers"):
        tree = parse(path)
        package = package_of(path)

        for node, targets in imports_of(tree, package):
            if any(reaches(target, "subprocess") for target in targets):
                violations.append(f"{path}:{node.lineno} imports subprocess")
            for name in IMPURE_ATTRIBUTES:
                if name in targets:
                    violations.append(f"{path}:{node.lineno} imports {name}")

        for child in ast.walk(tree):
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and f"{child.value.id}.{child.attr}" in IMPURE_ATTRIBUTES
            ):
                violations.append(f"{path}:{child.lineno} references {child.value.id}.{child.attr}")
            if isinstance(child, ast.Call) and _callee_name(child.func) == "open":
                violations.append(f"{path}:{child.lineno} calls open()")

    assert not violations, "parsers must stay pure:\n" + "\n".join(violations)


def test_only_process_may_spawn() -> None:
    """process.py owns subprocess, so timeouts and env hygiene cannot be bypassed elsewhere."""
    violations: list[str] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if path == PROCESS:
            continue
        package = package_of(path)
        for node, targets in imports_of(parse(path), package):
            if any(reaches(target, "subprocess") for target in targets):
                violations.append(f"{path}:{node.lineno} imports subprocess")

    assert not violations, "only process.py may import subprocess:\n" + "\n".join(violations)


def _callee_name(func: ast.expr) -> str:
    """Return the last name in a call target: `open`, `p.open` -> open."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""
