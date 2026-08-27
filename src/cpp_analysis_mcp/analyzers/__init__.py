"""Analyzers: one contract, N plugins (layer 3 of architecture v2).

The contract and registry live in `base`; each tool gets one module here as the
phase advances -- clang-tidy and compiler warnings first, the rest as plugins.
"""

from cpp_analysis_mcp.analyzers.base import (
    Analyzer,
    AnalyzerContext,
    Applicability,
    CostTier,
    Registry,
    Resolution,
    Scope,
    UnitOfWork,
)

__all__ = [
    "Analyzer",
    "AnalyzerContext",
    "Applicability",
    "CostTier",
    "Registry",
    "Resolution",
    "Scope",
    "UnitOfWork",
]
