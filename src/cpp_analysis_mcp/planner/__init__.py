"""Layer 4: the deterministic planner -- never an LLM, never a model call (ADR-0001).

Scope resolution lives here today; applicability gates, cost-tier scheduling, and the
plan trace arrive with the work that needs them. Same inputs, same plan, every run.
"""
