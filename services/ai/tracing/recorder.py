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
) -> None:
    """Record one event on the active run, if any.

    Recording is best effort: a database hiccup degrades the trace and is
    logged by the writer, but never surfaces to the request that produced it.
    """
    run = active_run()
    if run is None or run.degraded:
        return
    try:
        await run.writer.append(
            run_id=run.run_id,
            type=type,
            payload=payload,
            span_id=span_id or run.run_id,
            parent_span_id=parent_span_id,
        )
    except Exception:
        # The writer already logs; this is the last line of defence.
        run.degraded = True


def fail_run(message: str) -> None:
    mark_failure(message)
