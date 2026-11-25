"""Trace decorator for tool handlers.

Wrapping a handler keeps the shared registry and the agent loop untouched: the
decorator records a span around ``execute`` and forwards everything else. Only
whitelisted, user-safe fields are recorded - never raw tool input (which may
contain a leave reason) and never credentials.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anthropic.types import ToolParam

from tracing.context import active_run
from tracing.recorder import emit
from tracing.writer import new_id

from tracing.tool_refs import RETRIEVAL_TOOLS, SAFE_REASON, _classify_failure, _extract_refs


class TracedToolHandler:
    """Delegating wrapper that records one span per tool execution."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_tools(self) -> list["ToolParam"]:
        return self._inner.get_tools()

    def can_handle(self, tool_name: str) -> bool:
        return self._inner.can_handle(tool_name)

    def requires_approval(self, tool_name: str) -> bool:
        return self._inner.requires_approval(tool_name)

    async def check_oauth_required(self, tool_name: str, tool_input: dict, context: Any):
        checker = getattr(self._inner, "check_oauth_required", None)
        if checker is None:
            return None
        return await checker(tool_name, tool_input, context)

    async def execute(self, tool_name: str, tool_input: dict, context: Any):
        run = active_run()
        if run is None:
            return await self._inner.execute(tool_name, tool_input, context)

        span_id = new_id()
        parent = run.model_span_id
        kind = "retrieval" if tool_name in RETRIEVAL_TOOLS else "action"
        await emit(
            "tool.started",
            {"tool": tool_name, "kind": kind},
            span_id=span_id,
            parent_span_id=parent,
        )
        started = time.monotonic()
        try:
            result = await self._inner.execute(tool_name, tool_input, context)
        except Exception as exc:
            await emit(
                "tool.failed",
                {
                    "tool": tool_name,
                    "kind": kind,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "error": type(exc).__name__,
                },
                span_id=span_id,
                parent_span_id=parent,
            )
            raise

        payload: dict[str, Any] = {
            "tool": tool_name,
            "kind": kind,
            "durationMs": int((time.monotonic() - started) * 1000),
            "status": "error" if getattr(result, "is_error", False) else "ok",
        }
        # Department/action/plugin version come from the tool catalog, so a
        # trace can say which department produced a step without the model
        # supplying anything.
        metadata = getattr(self._inner, "trace_metadata", None)
        if callable(metadata):
            payload.update(metadata(tool_name))
        # The query itself is what the employee typed in this chat; keep it short
        # so the step can be identified without storing the whole request.
        if tool_name in RETRIEVAL_TOOLS:
            query = tool_input.get("query") if isinstance(tool_input, dict) else None
            if isinstance(query, str) and query.strip():
                payload["queryPreview"] = query.strip()[:80]
        payload.update(
            _extract_refs(
                getattr(result, "content", []) or [],
                is_error=bool(getattr(result, "is_error", False)),
            )
        )
        await emit(
            "tool.failed" if payload["status"] == "error" else "tool.completed",
            payload,
            span_id=span_id,
            parent_span_id=parent,
        )
        return result


__all__ = ["TracedToolHandler", "RETRIEVAL_TOOLS"]
