"""Append-only writer for the per-run trace ledger.

Design constraints:

* **The chain is per run.** Appending locks the run row, so two processes (or
  two concurrent tool calls) can never fork the chain; ``(run_id, seq)`` and
  ``(run_id, event_id)`` are also enforced by the database.
* **Tracing must never break the conversation.** Every failure is logged and
  flips the run into a degraded state at most; the caller's business result is
  returned unchanged. A failed write is retried exactly once by the caller with
  the same event id, and business actions are never replayed because of it.
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
    ALGORITHM,
    event_digest,
    seal_digest,
)
from tracing.context import ActiveRun

logger = logging.getLogger(__name__)

MAX_PAYLOAD_STRING = 600


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
                        status, model_name, started_at)
                       VALUES ($1, $2, $3, $4, $5, 'running', $6, $7)""",
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
    ) -> None:
        event_id = event_id or new_id()
        body = clean_payload(payload)
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
                            logger.warning("Trace run %s vanished; dropping event %s", run_id, type)
                            return
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
                               SET last_seq = $2, head_digest = $3
                               WHERE run_id = $1""",
                            run_id,
                            seq,
                            digest,
                        )
            except Exception:
                logger.exception("Trace event %s for run %s was not recorded", type, run_id)

    async def finish(
        self,
        *,
        run_id: str,
        status: str,
        error_code: str | None = None,
        assistant_message_id: str | None = None,
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
                        await conn.execute(
                            """UPDATE agent_trace_runs
                               SET status = $2, error_code = $3, ended_at = $4,
                                   assistant_message_id = COALESCE($5, assistant_message_id),
                                   seal_version = $6, seal = $7
                               WHERE run_id = $1""",
                            run_id,
                            status,
                            error_code,
                            utc_now(),
                            assistant_message_id,
                            SEAL_VERSION if seal else None,
                            seal,
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
    "ALGORITHM",
    "SEAL_VERSION",
    "TraceWriter",
    "build_writer",
    "clean_payload",
    "format_ts",
    "new_id",
    "utc_now",
]
