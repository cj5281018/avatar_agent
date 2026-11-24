"""Request-scoped trace state.

``routers/chat.py`` binds the active run before it starts consuming the model
stream, so the model-event tap, the tool decorators, and the search handler all
record into the same ledger without threading a parameter through the agent
loop. Context variables propagate into tasks created while the run is bound,
which covers the Redis producer path as well.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracing.writer import TraceWriter


@dataclass
class ActiveRun:
    run_id: str
    chat_id: str
    user_id: str
    writer: "TraceWriter"
    model_name: str | None = None
    # Last model call that produced tool calls; tool spans hang off it.
    model_span_id: str | None = None
    degraded: bool = False
    dropped: int = 0
    capture_error: str | None = None
    failure: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


_active_run: ContextVar[ActiveRun | None] = ContextVar("avatar2_active_trace_run", default=None)


def active_run() -> ActiveRun | None:
    return _active_run.get()


def bind_run(run: ActiveRun):
    return _active_run.set(run)


def reset_run(token) -> None:
    _active_run.reset(token)


def set_model_span(span_id: str | None) -> None:
    run = _active_run.get()
    if run is not None:
        run.model_span_id = span_id


def mark_failure(message: str) -> None:
    run = _active_run.get()
    if run is not None and run.failure is None:
        run.failure = message
