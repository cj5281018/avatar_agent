"""Model-call tap and run lifecycle around the chat stream.

The agent loop in ``streaming/generate.py`` is left untouched. Instead the
router wraps the stream it already receives with an async generator that reads
the SSE frames the loop emits, records one span per provider call, and closes
the run when the stream ends. That keeps the harness' iteration order, retries
and cancellation behaviour exactly as they were.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from streaming.persist import sse_event_data, sse_event_type

from tracing.context import ActiveRun, bind_run, reset_run, set_model_span
from tracing.recorder import emit
from tracing.writer import build_writer, new_id, utc_now

logger = logging.getLogger(__name__)

_NON_TERMINAL_FAILURES = {"stream_error", "save_error"}


class _TapState:
    def __init__(self) -> None:
        self.model_span: str | None = None
        self.model_started_at: float | None = None
        self.model_name: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.tool_requests = 0
        self.model_calls = 0
        self.stream_error: str | None = None

    def reset_model(self) -> None:
        self.model_span = None
        self.model_started_at = None
        self.input_tokens = 0
        self.output_tokens = 0


async def _close_model_span(state: _TapState, *, status: str = "ok") -> None:
    if state.model_span is None or state.model_started_at is None:
        return
    duration_ms = int((time.monotonic() - state.model_started_at) * 1000)
    await emit(
        "model.completed" if status == "ok" else "model.failed",
        {
            "model": state.model_name,
            "durationMs": duration_ms,
            "inputTokens": state.input_tokens,
            "outputTokens": state.output_tokens,
            "status": status,
        },
        span_id=state.model_span,
    )
    state.reset_model()


def _usage_totals(message: Any) -> tuple[int, int]:
    if not isinstance(message, dict):
        return 0, 0
    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


async def _inspect(raw: str, state: _TapState) -> None:
    kind = sse_event_type(raw)
    data_text = sse_event_data(raw)

    if kind in _NON_TERMINAL_FAILURES:
        try:
            payload = json.loads(data_text)
        except ValueError:
            payload = {}
        message = payload.get("message") if isinstance(payload, dict) else None
        state.stream_error = str(message or "stream failed")[:300]
        await _close_model_span(state, status="error")
        await emit("run.error", {"message": state.stream_error})
        return

    if kind != "message" or not data_text:
        return

    try:
        event = json.loads(data_text)
    except ValueError:
        return
    if not isinstance(event, dict):
        return

    event_type = event.get("type")
    if event_type == "message_start":
        message = event.get("message") or {}
        state.model_name = message.get("model") if isinstance(message, dict) else None
        state.model_span = new_id()
        state.model_started_at = time.monotonic()
        state.model_calls += 1
        state.input_tokens, state.output_tokens = _usage_totals(message)
        set_model_span(state.model_span)
        await emit(
            "model.started",
            {"model": state.model_name},
            span_id=state.model_span,
        )
    elif event_type == "message_delta":
        usage = event.get("usage") or {}
        if isinstance(usage, dict):
            state.output_tokens = int(usage.get("output_tokens") or state.output_tokens)
    elif event_type == "content_block_start":
        block = event.get("content_block") or {}
        if isinstance(block, dict) and block.get("type") == "tool_use":
            state.tool_requests += 1
    elif event_type == "message_stop":
        await _close_model_span(state, status="ok")


async def traced_turn(
    stream: AsyncIterator[str],
    *,
    chat_id: str,
    user_id: str,
    user_message_id: str | None,
    model_name: str | None,
) -> AsyncIterator[str]:
    """Wrap a chat stream so the turn is recorded while it is delivered."""
    pool_writer = None
    try:
        from db.connection import get_db_pool

        pool_writer = build_writer(await get_db_pool())
    except Exception:
        logger.exception("Trace writer unavailable; continuing without tracing")

    run: ActiveRun | None = None
    if pool_writer is not None:
        run = await pool_writer.start_run(
            chat_id=chat_id,
            user_id=user_id,
            user_message_id=user_message_id,
            model_name=model_name,
        )

    if run is None:
        async for chunk in stream:
            yield chunk
        return

    token = bind_run(run)
    state = _TapState()
    started = time.monotonic()
    status = "completed"
    error_code: str | None = None

    try:
        await emit(
            "run.started",
            {"model": model_name, "userMessageId": user_message_id},
            span_id=run.run_id,
        )
        async for chunk in stream:
            try:
                await _inspect(chunk, state)
            except Exception:
                logger.exception("Trace tap failed to inspect an event")
            yield chunk

        if state.stream_error:
            status = "failed"
            error_code = "stream_error"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as exc:
        status = "failed"
        error_code = type(exc).__name__
        await _emit_safely(
            "run.error",
            {"message": str(exc)[:300]},
            span_id=run.run_id,
        )
        raise
    finally:
        await _emit_safely(
            "run.finished",
            {
                "status": status,
                "durationMs": int((time.monotonic() - started) * 1000),
                "modelCalls": state.model_calls,
                "toolRequests": state.tool_requests,
            },
            span_id=run.run_id,
        )
        try:
            await run.writer.finish(run_id=run.run_id, status=status, error_code=error_code)
        except Exception:
            logger.exception("Trace run %s could not be finalised", run.run_id)
        reset_run(token)


async def _emit_safely(type: str, payload: dict[str, Any], *, span_id: str) -> None:
    try:
        await emit(type, payload, span_id=span_id)
    except Exception:
        logger.exception("Trace event %s could not be emitted", type)


__all__ = ["traced_turn", "utc_now"]
