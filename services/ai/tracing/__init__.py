"""Per-turn execution tracing for the workbench chat.

The package records what actually happened during one assistant attempt:
provider calls, tool executions and the run outcome. Events are appended to a
per-run hash chain so the record can later be shown to a person and checked for
modification.

Submodules are imported explicitly by callers (``tracing.tee``,
``tracing.writer``) so that the canonical encoding helpers can be used - and
unit-tested - without pulling in the streaming stack.
"""

from tracing.canonical import (
    ALGORITHM,
    SEAL_VERSION,
    canonical_json,
    event_digest,
    seal_digest,
)

__all__ = [
    "ALGORITHM",
    "SEAL_VERSION",
    "canonical_json",
    "event_digest",
    "seal_digest",
]
