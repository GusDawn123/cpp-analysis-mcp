"""The MCP tool surface: tool definitions, their input schemas, and serialization of results
back to the assistant. Protocol only — each handler declares its inputs, delegates to one
pipeline, and serializes what comes back. A handler that grows an `if` means workflow logic
has leaked down from pipelines into the wrong layer. Tool descriptions here are load-bearing,
since they are the only information the assistant uses to choose what to run.
"""
