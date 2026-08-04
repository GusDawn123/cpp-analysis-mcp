"""The single place subprocesses get launched. Owns timeouts, output capture, environment
merging, and confinement to the workspace directory — nothing runs outside it. Every layer
that touches a host tool goes through here instead of calling subprocess directly, so the
rules about what may run and for how long are enforced in one file rather than restated at
each call site.
"""
