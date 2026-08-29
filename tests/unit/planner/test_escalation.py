"""Escalation rules are data: they match findings and propose, never run anything.

The shipped pack is held to its own fixture contract here -- every rule must carry
triggers that fire it and near-misses that must not -- the same plant-a-known-bug
discipline the golden files use.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cpp_analysis_mcp.planner.escalation import (
    RULES_DIR,
    Action,
    Rule,
    RuleStatus,
    Then,
    When,
    load_rules,
    propose,
)
from cpp_analysis_mcp.store.models import Analysis, Finding, Location, Severity

USE_AFTER_MOVE = "bugprone-use-after-move"


def a_finding(
    category: str = USE_AFTER_MOVE,
    tool: str = "clang-tidy",
    severity: Severity = Severity.WARNING,
    file: str | None = "src/order_book.cpp",
    line: int = 40,
) -> Finding:
    return Finding(
        id="tidy-0001",
        tool=tool,
        severity=severity,
        category=category,
        message="'order' used after it was moved",
        location=Location(file=file, line=line) if file is not None else None,
    )


def a_rule(
    rule_id: str = "7c9e4a2d-1f3b-4d8e-9a6c-2b5f8e1d4a7c",
    status: RuleStatus = RuleStatus.STABLE,
    action: Action = Action.PROPOSE,
    checks: frozenset[str] = frozenset({USE_AFTER_MOVE}),
    min_severity: Severity | None = None,
    min_count: int = 1,
    run: Analysis = Analysis.ASAN,
) -> Rule:
    return Rule(
        id=rule_id,
        title="use-after-move -> verify with ASan",
        description="low false-positive rate; exactly what ASan witnesses at runtime",
        status=status,
        when=When(tool="clang-tidy", rules=checks, min_severity=min_severity, min_count=min_count),
        then=Then(run=run, scope="translation_unit", action=action),
    )


# ---------------------------------------------------------------------------- matching


def test_a_matching_finding_yields_a_proposal_citing_the_rule() -> None:
    escalated = propose((a_finding(),), (a_rule(),))

    (proposal,) = escalated.proposals
    assert proposal.run is Analysis.ASAN
    assert proposal.where == "src/order_book.cpp"
    assert proposal.because == ("7c9e4a2d-1f3b-4d8e-9a6c-2b5f8e1d4a7c",)
    assert escalated.notes == ()


def test_two_rules_wanting_the_same_run_merge_into_one_proposal() -> None:
    first = a_rule(rule_id="7c9e4a2d-1f3b-4d8e-9a6c-2b5f8e1d4a7c")
    second = a_rule(
        rule_id="2d8b6e1f-5a4c-4b9d-8e3a-7f2c5d9b1e6a",
        checks=frozenset({USE_AFTER_MOVE, "bugprone-dangling-handle"}),
    )

    escalated = propose((a_finding(),), (first, second))

    (proposal,) = escalated.proposals
    assert proposal.because == (first.id, second.id)


def test_min_count_counts_within_one_file_not_across_the_repo() -> None:
    spread = (a_finding(file="src/a.cpp"), a_finding(file="src/b.cpp"))
    packed = (a_finding(file="src/a.cpp", line=10), a_finding(file="src/a.cpp", line=90))
    rule = a_rule(min_count=2)

    assert propose(spread, (rule,)).proposals == ()
    assert len(propose(packed, (rule,)).proposals) == 1


def test_a_finding_below_min_severity_does_not_count() -> None:
    escalated = propose(
        (a_finding(severity=Severity.NOTE),), (a_rule(min_severity=Severity.WARNING),)
    )

    assert escalated.proposals == ()


def test_the_wrong_tool_does_not_match_even_on_the_right_check() -> None:
    escalated = propose((a_finding(tool="compiler"),), (a_rule(),))

    assert escalated.proposals == ()


def test_a_locationless_finding_cannot_anchor_an_escalation() -> None:
    escalated = propose((a_finding(file=None),), (a_rule(),))

    assert escalated.proposals == ()


# ------------------------------------------------------------------- lifecycle and clamps


def test_an_experimental_auto_is_clamped_to_propose_with_a_note() -> None:
    rule = a_rule(status=RuleStatus.EXPERIMENTAL, action=Action.AUTO)

    escalated = propose((a_finding(),), (rule,))

    assert len(escalated.proposals) == 1
    assert any("experimental" in note and rule.id in note for note in escalated.notes)


def test_a_stable_auto_still_proposes_until_execution_exists() -> None:
    rule = a_rule(action=Action.AUTO)

    escalated = propose((a_finding(),), (rule,))

    assert len(escalated.proposals) == 1
    assert any("proposed instead" in note and rule.id in note for note in escalated.notes)


def test_a_deprecated_rule_never_fires() -> None:
    escalated = propose((a_finding(),), (a_rule(status=RuleStatus.DEPRECATED),))

    assert escalated.proposals == ()


def test_an_off_rule_never_fires() -> None:
    escalated = propose((a_finding(),), (a_rule(action=Action.OFF),))

    assert escalated.proposals == ()


# ------------------------------------------------------------------------ loading rules


def write_rule(directory: Path, text: str, name: str = "rule.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory


VALID_RULE_YAML = """\
id: 9b1d3f5a-7c2e-4a8b-9d4f-1e6a8c3b5d7f
title: unguarded write -> verify with TSan
description: a write the analysis says needs a lock is what TSan witnesses live
status: experimental
when:
  tool: compiler
  rules: [thread-safety-analysis]
then:
  run: tsan
  scope: translation_unit
  action: propose
provenance:
  author: gustavo
  created: 2026-08-29
  modified: 2026-08-29
  references:
    - https://clang.llvm.org/docs/ThreadSafetyAnalysis.html
"""


def test_a_valid_rule_file_loads(tmp_path: Path) -> None:
    (rule,) = load_rules(write_rule(tmp_path, VALID_RULE_YAML))

    assert rule.then.run is Analysis.TSAN
    assert rule.when.min_count == 1  # the default
    assert rule.when.min_severity is None


def test_a_rule_missing_a_required_field_refuses_naming_it(tmp_path: Path) -> None:
    broken = VALID_RULE_YAML.replace("title: unguarded write -> verify with TSan\n", "")

    with pytest.raises(ValueError, match="title"):
        load_rules(write_rule(tmp_path, broken))


def test_a_rule_naming_a_non_sanitizer_run_is_refused(tmp_path: Path) -> None:
    broken = VALID_RULE_YAML.replace("run: tsan", "run: profile")

    with pytest.raises(ValueError, match="profile"):
        load_rules(write_rule(tmp_path, broken))


def test_a_yaml_python_tag_is_refused(tmp_path: Path) -> None:
    hostile = 'id: !!python/object/apply:os.system ["echo owned"]\n'

    with pytest.raises(ValueError, match=r"rule\.yaml"):
        load_rules(write_rule(tmp_path, hostile))


# ------------------------------------------------------------------- the shipped pack


def load_fixture(path: Path) -> tuple[Finding, ...]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Finding(
            id=f"fx-{index:04d}",
            tool=row["tool"],
            severity=Severity(row["severity"]),
            category=row["category"],
            message=row["message"],
            location=Location(file=row["file"], line=row["line"]),
        )
        for index, row in enumerate(rows, start=1)
    )


def test_the_shipped_pack_loads_and_every_rule_honors_its_fixtures() -> None:
    """The fixture contract from the schema spec: a rule without fixtures does not
    merge, its triggers must fire it, and its near-misses must not."""
    rules = load_rules(RULES_DIR)
    assert rules, f"no rules shipped under {RULES_DIR}"

    for rule in rules:
        fixtures = RULES_DIR / "fixtures" / rule.id
        triggers = sorted((fixtures / "triggers").glob("*.json"))
        near_misses = sorted((fixtures / "near_misses").glob("*.json"))
        assert triggers, f"rule {rule.id} ships no trigger fixtures"
        assert near_misses, f"rule {rule.id} ships no near-miss fixtures"

        for trigger in triggers:
            fired = propose(load_fixture(trigger), rules)
            assert any(rule.id in proposal.because for proposal in fired.proposals), (
                f"{trigger.name} did not fire rule {rule.id}"
            )
        for miss in near_misses:
            fired = propose(load_fixture(miss), rules)
            assert not any(rule.id in proposal.because for proposal in fired.proposals), (
                f"{miss.name} fired rule {rule.id} and must not"
            )
