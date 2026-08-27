"""The store: the shared vocabulary, finding identity, and the operations over both.

Layer 2 of architecture v2. Models are the nouns every layer speaks; fingerprints give
findings an identity that survives edits; FindingStore answers the questions a review
gate asks -- what is new, what agrees, what is hidden, what to show first.
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
    ThreadAccess,
    Variant,
    VariantResult,
)
from cpp_analysis_mcp.store.store import FindingStore

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
    "ThreadAccess",
    "Variant",
    "VariantResult",
    "compute_fingerprint",
    "fingerprint",
    "fingerprint_batch",
]
