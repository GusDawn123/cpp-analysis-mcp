"""The store: the shared vocabulary, finding identity, and the operations over both.

Layer 2 of architecture v2. Models are the nouns every layer speaks; fingerprints give
findings an identity that survives edits; triage says how dangerous one is; FindingStore
answers what a review gate asks -- what is new, what agrees, what is hidden, what to
show first.
"""

from cpp_analysis_mcp.store.fingerprints import (
    SCHEME_VERSION,
    compute_fingerprint,
    fingerprint,
    fingerprint_batch,
)
from cpp_analysis_mcp.store.models import (
    SANITIZER_FOR,
    AccessOp,
    Analysis,
    AnalysisReport,
    BenchmarkReport,
    BuildFailure,
    BuiltBinary,
    CapabilityStatus,
    Confirmation,
    Finding,
    Fingerprint,
    Frame,
    FullCheckReport,
    Hotspot,
    Location,
    ProfileReport,
    SanitizerKind,
    Severity,
    SuggestedFix,
    ThreadAccess,
    Variant,
    VariantResult,
)
from cpp_analysis_mcp.store.store import FindingStore
from cpp_analysis_mcp.store.triage import Tier, tier_for

__all__ = [
    "SANITIZER_FOR",
    "SCHEME_VERSION",
    "AccessOp",
    "Analysis",
    "AnalysisReport",
    "BenchmarkReport",
    "BuildFailure",
    "BuiltBinary",
    "CapabilityStatus",
    "Confirmation",
    "Finding",
    "FindingStore",
    "Fingerprint",
    "Frame",
    "FullCheckReport",
    "Hotspot",
    "Location",
    "ProfileReport",
    "SanitizerKind",
    "Severity",
    "SuggestedFix",
    "ThreadAccess",
    "Tier",
    "Variant",
    "VariantResult",
    "compute_fingerprint",
    "fingerprint",
    "fingerprint_batch",
    "tier_for",
]
