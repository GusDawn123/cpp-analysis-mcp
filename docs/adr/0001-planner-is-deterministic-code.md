# ADR-0001: The planner is deterministic code, not an LLM

**Status:** Accepted · 2026-08-27

## Context

Something must decide which analyzers run on which files, in what order, with
what budget, and when a static finding justifies an expensive sanitizer run.
Three designs were considered:

**(a) The connected agent orchestrates.** Expose granular tools; let the MCP
client sequence them. This is what most MCP servers do. Every orchestration
decision burns the caller's tokens, sequencing is nondeterministic, and
multi-step tool choreography is precisely where agents fumble. v1 partially
lives here — the escalation ladder exists only as prose in tool descriptions,
hoping the agent follows it.

**(b) An LLM inside the server.** CodeRabbit's design: their pipeline calls
their own models for filtering and synthesis. Disqualified on this project's
constraints — it puts an API key inside the tool server, adds cost and latency
per call, and makes review results nondeterministic.

**(c) A deterministic planner in-server.** Applicability, ordering,
parallelism, caching, timeouts, and escalation are computed by ordinary code
from declared data. CodeRabbit's own engineering writing supports the split:
they run their static tools as deterministic stages *before* any model sees
anything, and deliberately reject giving the agent open-ended tool autonomy
("more isn't better; better is better").

## Decision

Option (c). The planner is a pure function from
`(scope, compile_commands, config, capabilities, project state)` to a **plan**:
which analyzers run, on what units, in what order, under what limits, and which
escalation rules armed. The plan is emitted as an artifact (the plan trace)
before anything executes.

The division of intelligence: **the planner decides what to run; the calling
agent decides what the results mean.** Escalation defaults to *propose* — the
agent, or the human behind it, approves expensive dynamic runs.

One escape hatch is reserved, not built: MCP **sampling** lets a server request
a completion from the *connected client's* model — no server-side key, billed
to the caller's existing subscription. If a future stage genuinely needs
in-pipeline judgment (e.g. false-positive triage on a noisy rule), it slots
into the store's ranking stage via sampling. Nothing else in the architecture
may assume an LLM exists server-side.

## Consequences

- Same inputs, same plan, every run — testable in the golden-file style this
  repo already uses.
- Planning is instant and free; no tokens spent on orchestration.
- The plan trace makes every run auditable: what ran, what was skipped, and why.
- The planner can never "reason" its way around a gap in the rules; escalation
  coverage grows only by adding rules (see ADR-0003). This is the accepted
  cost of determinism.

## References

- CodeRabbit, "Pipeline AI vs agentic AI for code reviews"
  (coderabbit.ai/blog) — where a production system drew the same line.
- Kudelski Security's CodeRabbit RCE disclosure — what happens when tool
  execution boundaries are informal (see also ADR-0003 on rules-as-data).
