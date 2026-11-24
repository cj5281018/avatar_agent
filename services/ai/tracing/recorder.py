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
    # Keep writing after a failure: the chain only knows about events that were
    # stored, and losing every later step would hide far more than the one that
    # failed. Degradation stays sticky via ``run.degraded``.
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


async def capture_failure(run, category: str) -> None:
    """Mark a turn degraded for an observed-but-unrecordable step.

    Used when the tap saw data it could not parse or inspect, so there is no
    append to fail. The counter means "observed failures", not an estimate of
    how many steps are missing.
    """
    run.degraded = True
    run.capture_error = run.capture_error or category
    run.dropped += 1
    try:
        await run.writer.mark_capture_degraded(run.run_id, category)
    except Exception:
        # The database may be the thing that is broken; memory state still
        # carries the degradation to the final update if that succeeds.
        pass
