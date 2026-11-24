"""Append-only writer for the per-run trace ledger.

Design constraints:

* **The chain is per run.** Appending locks the run row, so two processes (or
  two concurrent tool calls) can never fork the chain; ``(run_id, seq)`` and
  ``(run_id, event_id)`` are also enforced by the database.
* **Tracing must never break the conversation.** A failed insert is reported to
  the caller and persisted as a capture-health degradation, but the business
  result is returned unchanged. Nothing is ever replayed because a trace write
  failed.
* **Capture health is independent of chain integrity.** Once a run has dropped
  an event it stays degraded for the rest of the run; later successful writes
  cannot turn it back into "healthy".
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from asyncpg import Pool

from tracing.canonical import (
    SEAL_VERSION,
    event_digest,
    seal_digest,
)
from tracing.context import ActiveRun

logger = logging.getLogger(__name__)

MAX_PAYLOAD_STRING = 600
CAPTURE_OK = "ok"
CAPTURE_DEGRADED = "degraded"
CAPTURE_UNKNOWN = "unknown"

_fault_injected: set[str] = set()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(moment: datetime) -> str:
    """ISO-8601 with millisecond precision and a ``Z`` suffix.

    The digest and the stored column both go through this function so a later
    verification recomputes exactly the same string.
    """
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip(value: Any, depth: int = 0) -> Any:
    """Whitelist-friendly payload cleaning: primitives only, bounded size."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # Floats format differently across languages; never let them into the chain.
        return str(round(value, 3))
    if isinstance(value, str):
        return value[:MAX_PAYLOAD_STRING]
    if isinstance(value, list) and depth < 3:
        return [_clip(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict) and depth < 3:
        return {
            str(key)[:64]: _clip(item, depth + 1)
            for key, item in list(value.items())[:30]
            if item is not None
        }
    return None


def clean_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    cleaned = {key: _clip(value) for key, value in (payload or {}).items()}
    return {key: value for key, value in cleaned.items() if value is not None}


def new_id() -> str:
    return uuid.uuid4().hex


class _InjectedFault(RuntimeError):
    """Raised only by the explicit fault-injection hook (see ``_fault_hook``)."""


def _fault_hook(event_type: str) -> None:
    """Controlled fixture: ``AVATAR2_TRACE_FAULT_EVENT`` drops one event.

    Used by the acceptance suite to prove that a dropped write is reported as a
    capture degradation rather than as a healthy chain. Each listed type fails
    once per process; the variable is empty in normal operation.
    """
    configured = os.getenv("AVATAR2_TRACE_FAULT_EVENT", "").strip()
    if not configured:
        return
    types = {value.strip() for value in configured.split(",") if value.strip()}
    if event_type in types and event_type not in _fault_injected:
        _fault_injected.add(event_type)
        raise _InjectedFault(f"injected failure for {event_type}")


class TraceWriter:
    def __init__(self, pool: "Pool", seal_key: bytes | None) -> None:
        self._pool = pool
        self._seal_key = seal_key
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, run_id: str) -> asyncio.Lock:
        lock = self._locks.get(run_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[run_id] = lock
        return lock

    async def start_run(
        self,
        *,
        chat_id: str,
        user_id: str,
        user_message_id: str | None,
        model_name: str | None,
    ) -> ActiveRun | None:
        run_id = new_id()
        started_at = utc_now()
        try:
            async with self._pool.acquire() as conn:
                attempt = 1
                if user_message_id:
                    attempt = await conn.fetchval(
                        """SELECT COALESCE(MAX(attempt), 0) + 1 FROM agent_trace_runs
                           WHERE chat_id = $1 AND user_message_id = $2""",
                        chat_id,
                        user_message_id,
                    )
                await conn.execute(
                    """INSERT INTO agent_trace_runs
                       (run_id, chat_id, user_id, user_message_id, attempt,
                        status, model_name, started_at, capture_status, last_event_at)
                       VALUES ($1, $2, $3, $4, $5, 'running', $6, $7, 'ok', $7)""",
                    run_id,
                    chat_id,
                    user_id,
                    user_message_id,
                    attempt or 1,
                    model_name,
                    started_at,
                )
        except Exception:
            logger.exception("Trace run %s could not be created; continuing without tracing", run_id)
            return None
        return ActiveRun(
            run_id=run_id,
            chat_id=chat_id,
            user_id=user_id,
            writer=self,
            model_name=model_name,
        )

    async def append(
        self,
        *,
        run_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        span_id: str,
        parent_span_id: str | None = None,
        event_id: str | None = None,
    ) -> bool:
        """Append one event. Returns False when the event was not recorded."""
        event_id = event_id or new_id()
        body = clean_payload(payload)
        async with self._lock(run_id):
            try:
                _fault_hook(type)
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            """SELECT last_seq, head_digest FROM agent_trace_runs
                               WHERE run_id = $1 FOR UPDATE""",
                            run_id,
                        )
                        if row is None:
                            logger.warning("Trace run %s vanished; dropping event %s", run_id, type)
                            return False
                        seq = int(row["last_seq"]) + 1
                        previous = row["head_digest"]
                        moment = utc_now()
                        ts = format_ts(moment)
                        digest = event_digest(
                            run_id=run_id,
                            seq=seq,
                            event_id=event_id,
                            span_id=span_id,
                            parent_span_id=parent_span_id,
                            type=type,
                            ts=ts,
                            payload=body,
                            previous_digest=previous,
                        )
                        await conn.execute(
                            """INSERT INTO agent_trace_events
                               (run_id, seq, event_id, span_id, parent_span_id, type, ts,
                                payload, previous_digest, digest)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)""",
                            run_id,
                            seq,
                            event_id,
                            span_id,
                            parent_span_id,
                            type,
                            moment,
                            _json(body),
                            previous,
                            digest,
                        )
                        await conn.execute(
                            """UPDATE agent_trace_runs
                               SET last_seq = $2, head_digest = $3, last_event_at = $4
                               WHERE run_id = $1""",
                            run_id,
                            seq,
                            digest,
                            moment,
                        )
                return True
            except Exception as exc:
                logger.exception("Trace event %s for run %s was not recorded", type, run_id)
                await self._persist_degradation(run_id, f"{type}: {exc.__class__.__name__}")
                return False

    async def mark_capture_degraded(self, run_id: str, reason: str) -> None:
        """Public hook for callers that observed an unrecordable step."""
        await self._persist_degradation(run_id, reason)

    async def _persist_degradation(self, run_id: str, reason: str) -> None:
        """Best-effort durable evidence that events may be missing.

        If the database itself is unavailable this write fails too; the reader
        then falls back to the run's stale heartbeat (see the detail API).
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """UPDATE agent_trace_runs
                       SET capture_status = 'degraded',
                           capture_error = COALESCE(capture_error, $2),
                           dropped_events = dropped_events + 1
                       WHERE run_id = $1""",
                    run_id,
                    reason[:300],
                )
        except Exception:
            logger.exception("Capture degradation for run %s could not be persisted", run_id)

    async def finish(
        self,
        *,
        run_id: str,
        status: str,
        error_code: str | None = None,
        assistant_message_id: str | None = None,
        capture_status: str = CAPTURE_OK,
        capture_error: str | None = None,
        dropped_events: int = 0,
    ) -> None:
        async with self._lock(run_id):
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            """SELECT last_seq, head_digest FROM agent_trace_runs
                               WHERE run_id = $1 FOR UPDATE""",
                            run_id,
                        )
                        if row is None:
                            return
                        last_seq = int(row["last_seq"])
                        head = row["head_digest"]
                        seal = (
                            seal_digest(
                                self._seal_key,
                                run_id=run_id,
                                last_seq=last_seq,
                                head_digest=head,
                            )
                            if self._seal_key and last_seq > 0
                            else None
                        )
                        # A run that never recorded a single event cannot claim
                        # healthy capture even if the process believes it was fine.
                        effective_capture = capture_status
                        if last_seq == 0 and capture_status != CAPTURE_DEGRADED:
                            effective_capture = CAPTURE_UNKNOWN
                        await conn.execute(
                            """UPDATE agent_trace_runs
                               SET status = $2, error_code = $3, ended_at = $4,
                                   assistant_message_id = COALESCE($5, assistant_message_id),
                                   seal_version = $6, seal = $7,
                                   capture_status = CASE
                                       WHEN capture_status = 'degraded' THEN 'degraded'
                                       ELSE $8
                                   END,
                                   capture_error = COALESCE(capture_error, $9),
                                   dropped_events = GREATEST(dropped_events, $10)
                               WHERE run_id = $1""",
                            run_id,
                            status,
                            error_code,
                            utc_now(),
                            assistant_message_id,
                            SEAL_VERSION if seal else None,
                            seal,
                            effective_capture,
                            capture_error,
                            dropped_events,
                        )
            except Exception:
                logger.exception("Trace run %s could not be finalised", run_id)
            finally:
                self._locks.pop(run_id, None)


def _json(body: dict[str, Any]) -> str:
    from tracing.canonical import canonical_json

    return canonical_json(body)


def build_writer(pool: "Pool") -> TraceWriter | None:
    """Create a writer from the environment; ``None`` disables tracing."""
    enabled = os.getenv("AVATAR2_TRACE_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None
    seal_key = os.getenv("AGENT_TRACE_SEAL_KEY", "").strip()
    if not seal_key:
        logger.warning(
            "AGENT_TRACE_SEAL_KEY is unset: traces are recorded but the chain head cannot be sealed"
        )
    return TraceWriter(pool, seal_key.encode("utf-8") if seal_key else None)


__all__ = [
    "CAPTURE_DEGRADED",
    "CAPTURE_OK",
    "CAPTURE_UNKNOWN",
    "TraceWriter",
    "build_writer",
    "clean_payload",
    "format_ts",
    "new_id",
    "utc_now",
]
