"""Probes what this machine can actually do by compiling and running five-line smoke tests,
one per sanitizer and analysis, rather than sniffing version numbers — a compiler can report
a version that supports ThreadSanitizer while the runtime library is missing or a system
setting blocks it. Results become CapabilityStatus values carrying the reason and a concrete
suggestion when something is unavailable, and cache to disk fingerprinted on compiler path,
compiler version, and OS release so only the first run pays the cost.
"""
