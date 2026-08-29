# Design patterns — the shapes this code is written in

The rules that keep the code clean, readable, and maintainable. Each one is a
pattern already practiced in this codebase and cites a live example, so none of
this is aspiration. Layering rules live in `architecture-v2.md`; comment and
workflow rules in `CLAUDE.md`. This file is about the shape of the code itself.

Everything below sits on one standing gate: the whole codebase — `src`, `tests`,
and `scripts` — type-checks under `mypy --strict`, and `make all` runs that
check before every commit. No untyped function exists anywhere.

## 1. Data is immutable

Every model is a frozen dataclass with slots. A mapping shared across callers
is wrapped in `MappingProxyType` before anyone sees it, so a value handed to N
requests cannot drift under any of them. Immutability is also the concurrency
plan: Phase 2 dispatches analyzers in parallel, and frozen data shared between
workers needs no locks.
Live example: `Context.__post_init__` in `src/cpp_analysis_mcp/context.py`.

## 2. Expected failures are return values, never exceptions

A pipeline returns what happened: `AnalysisReport | BuildFailure |
CapabilityStatus`. The union in the signature is the complete story, and mypy
forces every caller to handle all of it. Exceptions are reserved for unusable
configuration and programmer error — situations no caller can act on.

This is deliberately not idiomatic Python; do not "fix" it back to raises,
because the union is what makes forgetting an outcome a type error instead of
a 2 a.m. traceback. Keep unions small: one growing past three outcomes is a
design conversation, not a bigger union.
Live example: `profile_file` in `src/cpp_analysis_mcp/pipelines/profile.py`;
the one sanctioned raise, `NO_COMPILER` in `context.resolve()`.

## 3. Dependencies are injected; one composition root

`Runner`, `Platform`, and `Toolchain` arrive as arguments everywhere. Exactly
one place may look the world up — `context.resolve()` — and an architecture
test keeps it that way. Tests swap the subprocess boundary for a fake and
touch nothing else.
Live example: every pipeline signature; `tests/unit/test_architecture.py`.

## 4. Variation is a new row or a new module — never a new `if`

Value differences live in tables (`SEVERITIES`, `_RUNNERS`); behavior
differences live in modules behind a shared seam (`platforms/`,
`toolchains/`). Scattered `if platform == ...` branches are the failure mode
this rule exists to prevent — and a dict of lambdas is branches with worse
stack traces, not a table.
Live example: `SEVERITIES` in `parsers/clang_tidy.py`; the `platforms/` and
`toolchains/` packages; `_RUNNERS` in `pipelines/static_check.py`.

## 5. Parsers are pure functions

Text in, findings out — no I/O, no subprocess, no clock. Purity is what makes
golden-file testing possible: tool output captured once, replayed forever.
Live example: `parse` in `src/cpp_analysis_mcp/parsers/clang_tidy.py`.

## 6. New capability is a plugin behind the one contract

Analyzers implement `applicable(scope, ctx)` and `run(...)`; the registry
knows names, tiers, and gates — never tools. Adding an analyzer touches its
own module and nothing above it.
Live example: `analyzers/clang_tidy.py` and `analyzers/warnings.py` behind
one `Registry`.

## 7. Refusals explain themselves; no false all-clear

Every gate that says no says why, in words a person can act on — enforced in
the type itself, not by convention. A detector that was not watching reports
unavailable rather than clean.

Saying no has three distinct moments, on purpose: a gate refusal ("I will not
try"), an unavailable capability ("this machine cannot"), and a `BuildFailure`
("the attempt died"). They stay distinct where they happen and converge where
they are consumed — the analyzer layer turns the last two into ERROR findings,
so nothing silently vanishes.
Live example: `Applicability.__post_init__` in `analyzers/base.py`; the
outcome conversions in `analyzers/_adapter.py`.

## 8. A killed run reports itself

A run that hits its timeout returns `timed_out=True` and keeps every finding
it printed before dying: killed output is stamped `[killed after Ns timeout]`
and partial output is salvaged, never discarded. A timeout must never read as
clean — that would be rule 7's false all-clear in its most invisible form,
since an empty result and a clean one look identical.
Live example: `_drained` in `src/cpp_analysis_mcp/process.py`;
`test_a_run_killed_at_its_timeout_still_reports_what_it_printed` in
`tests/unit/pipelines/test_sanitize.py`.

## 9. Validate at the boundary; trust the inside

Inputs are checked once, where they enter — the server surface today, the
scope resolver from Phase 2 — and turned into a richer type there, so the type
system remembers the check happened. Everything deeper assumes valid data:
defensive re-validation in every layer is how codebases bloat.
Live example: `_workspace` in `src/cpp_analysis_mcp/context.py` settles the
workspace path once at resolve time; nothing downstream re-checks it.

## 10. Tests use recording fakes and are named as behavior

A fake records what crossed the boundary and serves scripted outcomes.
Assertions are on what was recorded and what came back — never on how many
times something was called. When a count *is* the behavior (a cache probes
once, a screen checks each file exactly once), read it off the fake's recorded
list: `check.checked == [a, b]` proves count, order, and arguments at once, as
data. Test names read as sentences, so a failing run is a readable bug report.
Live example: `RecordingCheck` in
`tests/unit/analyzers/test_clang_tidy_analyzer.py`.

## 11. One module, one decision

A file answers one question and its directory answers "what layer is this."
Product copy lives in `server.py` alone. A module that needs a paragraph to
explain what it is, is two modules. This is the one rule with no mechanical
enforcement — it is refereed by a human at review, on purpose: length caps and
import counts get gamed, judgment does not.

---

Changing one of these rules is a CLAUDE.md or ADR conversation, not a
refactor slipped into a feature branch.
