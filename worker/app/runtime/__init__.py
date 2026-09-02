"""Worker runtime primitives: identity, reliable queue, heartbeat, subprocess execution.

These are deliberately thin wrappers over Redis and `subprocess`. They exist to make a run
survivable and observable; they contain no editorial logic and no knowledge of the pipeline.
"""
