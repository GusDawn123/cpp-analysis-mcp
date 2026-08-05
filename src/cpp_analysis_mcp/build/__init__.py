"""Turns source into a BuiltBinary bound to the runtime environment its sanitizer needs."""

from cpp_analysis_mcp.build.single_file import compile_file

__all__ = ["compile_file"]
