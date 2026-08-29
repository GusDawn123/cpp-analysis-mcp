"""Escalation rules are data (ADR-0003): match static findings, propose dynamic proof.

Rules load from YAML, validate loudly, and only ever *propose* -- nothing here runs a
tool. Cooldown, metrics, and config precedence arrive with project state; executing
an `auto` arrives when sanitizers are plugins, and until then auto proposes.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import yaml

from cpp_analysis_mcp.store.models import Analysis, Finding, Severity

__all__ = [
    "RULES_DIR",
    "Action",
    "Escalation",
    "Proposal",
    "Rule",
    "RuleStatus",
    "Then",
    "When",
    "load_rules",
    "propose",
]

RULES_DIR = Path(__file__).parent / "rules"

# the escalation targets: analyses that witness at runtime what static evidence suspects
_SANITIZERS = frozenset({Analysis.ASAN, Analysis.TSAN, Analysis.LSAN, Analysis.UBSAN})

_SCOPES = frozenset({"translation_unit", "enclosing_target"})

# "at least this severe" needs a ladder, and the enum defines error first
_SEVERITY_RANK = {Severity.NOTE: 0, Severity.WARNING: 1, Severity.ERROR: 2}


class RuleStatus(StrEnum):
    EXPERIMENTAL = "experimental"
    STABLE = "stable"
    DEPRECATED = "deprecated"


class Action(StrEnum):
    PROPOSE = "propose"
    AUTO = "auto"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class When:
    """The match clause: pure matching, never computation (ADR-0003)."""

    tool: str
    rules: frozenset[str]
    min_severity: Severity | None = None
    min_count: int = 1


@dataclass(frozen=True, slots=True)
class Then:
    run: Analysis
    scope: str
    action: Action


@dataclass(frozen=True, slots=True)
class Rule:
    """One escalation rule. Provenance is validated at load and lives in the YAML --
    nothing at runtime consumes it, so the model does not carry it."""

    id: str
    title: str
    description: str
    status: RuleStatus
    when: When
    then: Then


@dataclass(frozen=True, slots=True)
class Proposal:
    """One suggested dynamic verification, with every rule that argued for it."""

    run: Analysis
    unit: str
    where: str
    because: tuple[str, ...]
    titles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Escalation:
    """What the rule table concluded: proposals to make, and notes for the trace."""

    proposals: tuple[Proposal, ...]
    notes: tuple[str, ...]


def load_rules(directory: Path) -> tuple[Rule, ...]:
    """Load and validate every rule in a directory; a bad rule fails loudly by name.

    safe_load only: a rule file is data, and YAML tags that construct objects are
    exactly the computation the schema forbids.
    """
    rules: list[Rule] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise ValueError(f"{path.name}: not readable as a rule: {error}") from error
        rules.append(_rule(path.name, document))
    return tuple(rules)


def propose(findings: Sequence[Finding], rules: Sequence[Rule]) -> Escalation:
    """Match findings against the table and merge identical asks; propose, never run.

    min_count counts within one file -- the unit-of-work stand-in until units split
    beyond whole files -- and identical (run, scope, file) asks from different rules
    become one proposal citing every rule that argued for it.
    """
    notes: list[str] = []
    merged: dict[tuple[Analysis, str, str], tuple[list[str], list[str]]] = {}
    for rule in rules:
        if rule.status is RuleStatus.DEPRECATED or rule.then.action is Action.OFF:
            continue

        counts: dict[str, int] = {}
        for finding in findings:
            if finding.tool != rule.when.tool or finding.category not in rule.when.rules:
                continue
            if rule.when.min_severity is not None and (
                _SEVERITY_RANK[finding.severity] < _SEVERITY_RANK[rule.when.min_severity]
            ):
                continue
            # a locationless finding gives a verifier nothing to point at
            if finding.location is None:
                continue
            counts[finding.location.file] = counts.get(finding.location.file, 0) + 1

        hit_files = [file for file, count in counts.items() if count >= rule.when.min_count]
        if not hit_files:
            continue
        if rule.then.action is Action.AUTO:
            if rule.status is RuleStatus.EXPERIMENTAL:
                notes.append(f"rule {rule.id} is experimental: auto is clamped to propose")
            else:
                notes.append(
                    f"rule {rule.id} asks for auto, which is not built yet; proposed instead"
                )
        for file in hit_files:
            ids, titles = merged.setdefault((rule.then.run, rule.then.scope, file), ([], []))
            ids.append(rule.id)
            titles.append(rule.title)

    proposals = tuple(
        Proposal(run=run, unit=unit, where=where, because=tuple(ids), titles=tuple(titles))
        for (run, unit, where), (ids, titles) in merged.items()
    )
    return Escalation(proposals=proposals, notes=tuple(notes))


def _rule(name: str, document: object) -> Rule:
    body = _section(name, document, "rule")
    for required in ("id", "title", "description", "status", "when", "then", "provenance"):
        if required not in body:
            raise ValueError(f"{name}: missing required field {required!r}")

    rule_id = str(body["id"])
    try:
        uuid.UUID(rule_id)
    except ValueError as error:
        raise ValueError(f"{name}: id must be a UUID, got {rule_id!r}") from error

    provenance = _section(name, body["provenance"], "provenance")
    for required in ("author", "created", "modified", "references"):
        if required not in provenance:
            raise ValueError(f"{name}: provenance is missing {required!r}")

    return Rule(
        id=rule_id,
        title=str(body["title"]),
        description=str(body["description"]),
        status=_member(name, RuleStatus, body["status"], "status"),
        when=_when(name, _section(name, body["when"], "when")),
        then=_then(name, _section(name, body["then"], "then")),
    )


def _when(name: str, body: dict[str, Any]) -> When:
    for required in ("tool", "rules"):
        if required not in body:
            raise ValueError(f"{name}: when is missing {required!r}")
    checks = body["rules"]
    if (
        not isinstance(checks, list)
        or not checks
        or not all(isinstance(check, str) for check in checks)
    ):
        raise ValueError(f"{name}: when.rules must be a non-empty list of check names")
    min_severity = body.get("min_severity")
    min_count = body.get("min_count", 1)
    # bool first: YAML reads `true` as a bool, and Python counts a bool as an int
    if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
        raise ValueError(f"{name}: when.min_count must be a positive integer")
    return When(
        tool=str(body["tool"]),
        rules=frozenset(checks),
        min_severity=(
            _member(name, Severity, min_severity, "when.min_severity")
            if min_severity is not None
            else None
        ),
        min_count=min_count,
    )


def _then(name: str, body: dict[str, Any]) -> Then:
    for required in ("run", "scope", "action"):
        if required not in body:
            raise ValueError(f"{name}: then is missing {required!r}")
    run = _member(name, Analysis, body["run"], "then.run")
    if run not in _SANITIZERS:
        raise ValueError(f"{name}: then.run {run.value!r} is not a dynamic verifier")
    scope = str(body["scope"])
    if scope not in _SCOPES:
        raise ValueError(f"{name}: then.scope must be one of {sorted(_SCOPES)}, got {scope!r}")
    return Then(run=run, scope=scope, action=_member(name, Action, body["action"], "then.action"))


def _section(name: str, value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name}: {label} must be a mapping, got {type(value).__name__}")
    return value


E = TypeVar("E", bound=StrEnum)


def _member(name: str, kind: type[E], value: object, label: str) -> E:
    try:
        return kind(str(value))
    except ValueError as error:
        allowed = [member.value for member in kind]
        raise ValueError(f"{name}: {label} {value!r} is not one of {allowed}") from error
