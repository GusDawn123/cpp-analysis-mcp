"""Analyzers: one contract, N plugins (layer 3 of architecture v2). The contract and
registry live in `base`; each tool gets one module here as the phase advances.
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
from cpp_analysis_mcp.analyzers.clang_tidy import ClangTidyAnalyzer
from cpp_analysis_mcp.analyzers.warnings import WarningsAnalyzer

__all__ = [
    "Analyzer",
    "AnalyzerContext",
    "Applicability",
    "ClangTidyAnalyzer",
    "CostTier",
    "Registry",
    "Resolution",
    "Scope",
    "UnitOfWork",
    "WarningsAnalyzer",
]
