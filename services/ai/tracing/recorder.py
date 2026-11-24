"""Thin recording API used by the model tap and the tool decorator."""

from __future__ import annotations

from typing import Any

from tracing.context import active_run, mark_failure


async def emit(
    type: str,
    payload: dict[str, Any] | None = None,
    *,
    span_id: str | None = None,
    parent_span_id: str | None = None,
) -> bool:
    """Record one event on the active run, if any.

    Returns ``True`` when the event was persisted. A failure marks the run as
    degraded and is remembered for the final row, but never surfaces to the
    request that produced it. Degradation is sticky: a later success cannot
    restore the run to "healthy".
    """
    run = active_run()
    if run is None:
        return False
    if run.degraded:
        # Keep counting so the report says how much may be missing.
        run.dropped += 1
        return False
    try:
        recorded = await run.writer.append(
            run_id=run.run_id,
            type=type,
            payload=payload,
            span_id=span_id or run.run_id,
            parent_span_id=parent_span_id,
        )
    except Exception:
        recorded = False
    if not recorded:
        run.degraded = True
        run.capture_error = run.capture_error or type
        run.dropped += 1
    return recorded


def fail_run(message: str) -> None:
    mark_failure(message)
