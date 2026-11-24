"""Controlled trace fixtures, run inside the AI service container.

Usage (from the host):

    docker exec -i avatar2-source-ai-1 python - multi-draft
    docker exec -i avatar2-source-ai-1 python - stale-create
    docker exec -i avatar2-source-ai-1 python - stale-finish <runId>
    docker exec -i avatar2-source-ai-1 python - not-sealed

They create real HR drafts through the gateway (so the business records are
authoritative) and valid hash-chained runs, then print identifiers as JSON.
Test data only; never pointed at production.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

from db.connection import get_db_pool
from tracing.writer import TraceWriter, new_id


async def _bob_context(pool) -> tuple[str, str]:
    async with pool.acquire() as conn:
        user_id = await conn.fetchval("SELECT id FROM users WHERE email = 'bob@avatar2.test'")
        chat_id = await conn.fetchval(
            "SELECT id FROM chats WHERE user_id = $1 ORDER BY created_at DESC LIMIT 1",
            user_id,
        )
    if not user_id or not chat_id:
        raise SystemExit("bob or a bob-owned chat is missing")
    return user_id, chat_id


async def _gateway_post(path: str, payload: dict) -> dict:
    base = os.environ["AVATAR2_GATEWAY_URL"].rstrip("/")
    token = os.environ["AVATAR2_INTERNAL_TOKEN"]
    async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as client:
        response = await client.post(
            path, json=payload, headers={"Authorization": "Bearer " + token}
        )
        response.raise_for_status()
        return response.json()


async def _append(writer: TraceWriter, run_id: str, type: str, payload: dict, span: str, parent: str | None = None) -> None:
    await writer.append(
        run_id=run_id, type=type, payload=payload, span_id=span, parent_span_id=parent
    )


async def multi_draft() -> dict:
    pool = await get_db_pool()
    user_id, chat_id = await _bob_context(pool)
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)

    drafts = []
    for day in ("2026-12-29", "2026-12-30"):
        item = await _gateway_post(
            "/internal/tools/call",
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "name": "hr_preview_leave",
                "arguments": {
                    "startDate": day,
                    "endDate": day,
                    "leaveType": "annual",
                    "reason": "trace fixture",
                },
            },
        )
        drafts.append(item["operationId"])

    run = await writer.start_run(
        chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="deepseek-flash"
    )
    assert run is not None
    model_span, d1, d2, d3, decoy = new_id(), new_id(), new_id(), new_id(), new_id()
    await _append(writer, run.run_id, "run.started", {"model": "fixture"}, run.run_id)
    await _append(writer, run.run_id, "model.started", {"model": "deepseek-flash", "callIndex": 1}, model_span)
    await _append(writer, run.run_id, "model.completed", {"model": "deepseek-flash", "durationMs": 10, "callIndex": 1}, model_span)
    await _append(
        writer, run.run_id, "tool.completed",
        {"tool": "hr_preview_leave", "kind": "action", "status": "ok", "durationMs": 5, "operationId": drafts[0]},
        d1, model_span,
    )
    await _append(
        writer, run.run_id, "tool.completed",
        {"tool": "hr_preview_leave", "kind": "action", "status": "ok", "durationMs": 6, "operationId": drafts[1]},
        d2, model_span,
    )
    # A second drafting step for the first business record: two spans, one
    # operation, both must stay reachable.
    await _append(
        writer, run.run_id, "tool.completed",
        {"tool": "hr_preview_leave", "kind": "action", "status": "ok", "durationMs": 4, "operationId": drafts[0]},
        d3, model_span,
    )
    # A later, non-draft tool that also happens to return an operation id must
    # not become the business association.
    await _append(
        writer, run.run_id, "tool.completed",
        {"tool": "hr_list_my_requests", "kind": "action", "status": "ok", "operationId": "lop_DECOY_not_a_draft"},
        decoy, model_span,
    )
    await _append(writer, run.run_id, "run.finished", {"status": "completed"}, run.run_id)
    await writer.finish(run_id=run.run_id, status="completed", capture_status="ok")

    confirmed = await _gateway_post(
        f"/internal/cards/{chat_id}/{drafts[0]}/confirm", {"user_id": user_id}
    )
    return {
        "chatId": chat_id,
        "runId": run.run_id,
        "draftOperationIds": drafts,
        "draftSpanIds": [d1, d2, d3],
        "decoySpanId": decoy,
        "confirmedLeaveId": confirmed.get("leaveId"),
        "secondDraftStatus": (
            await _gateway_post(
                f"/internal/cards/{chat_id}/{drafts[1]}/status", {"user_id": user_id}
            )
        ).get("status"),
    }


async def running_create() -> dict:
    """A run that is genuinely running right now, for poll-path tests."""
    pool = await get_db_pool()
    user_id, chat_id = await _bob_context(pool)
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)
    run = await writer.start_run(
        chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="deepseek-flash"
    )
    assert run is not None
    model_span = new_id()
    await _append(writer, run.run_id, "run.started", {"model": "running-fixture"}, run.run_id)
    await _append(writer, run.run_id, "model.started", {"model": "deepseek-flash", "callIndex": 1}, model_span)
    return {"chatId": chat_id, "runId": run.run_id, "modelSpanId": model_span}


async def many_drafts() -> dict:
    """Seven drafts in one run, to prove every drafting step is reachable."""
    pool = await get_db_pool()
    user_id, chat_id = await _bob_context(pool)
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)
    drafts = []
    for day in range(1, 8):
        item = await _gateway_post(
            "/internal/tools/call",
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "name": "hr_preview_leave",
                "arguments": {
                    "startDate": f"2027-04-0{day}",
                    "endDate": f"2027-04-0{day}",
                    "leaveType": "annual",
                    "reason": "many drafts fixture",
                },
            },
        )
        drafts.append(item["operationId"])
    run = await writer.start_run(
        chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="many-drafts-fixture"
    )
    assert run is not None
    model_span = new_id()
    await _append(writer, run.run_id, "run.started", {"model": "many-drafts-fixture"}, run.run_id)
    await _append(writer, run.run_id, "model.started", {"model": "deepseek-flash", "callIndex": 1}, model_span)
    await _append(writer, run.run_id, "model.completed", {"model": "deepseek-flash", "durationMs": 10, "callIndex": 1}, model_span)
    spans = []
    for index, operation in enumerate(drafts):
        span = new_id()
        spans.append(span)
        await _append(
            writer, run.run_id, "tool.completed",
            {"tool": "hr_preview_leave", "kind": "action", "status": "ok", "durationMs": 1, "operationId": operation},
            span, model_span,
        )
    await _append(writer, run.run_id, "run.finished", {"status": "completed"}, run.run_id)
    await writer.finish(run_id=run.run_id, status="completed", capture_status="ok")
    return {"chatId": chat_id, "runId": run.run_id, "operationIds": drafts, "spanIds": spans}


async def stale_create() -> dict:
    pool = await get_db_pool()
    user_id, chat_id = await _bob_context(pool)
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)
    run = await writer.start_run(
        chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="deepseek-flash"
    )
    assert run is not None
    model_span = new_id()
    await _append(writer, run.run_id, "run.started", {"model": "stale-fixture"}, run.run_id)
    await _append(writer, run.run_id, "model.started", {"model": "deepseek-flash", "callIndex": 1}, model_span)
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE agent_trace_runs
               SET started_at = now() - interval '3 minutes',
                   last_event_at = now() - interval '3 minutes'
               WHERE run_id = $1""",
            run.run_id,
        )
    return {"chatId": chat_id, "runId": run.run_id, "modelSpanId": model_span}


async def stale_finish(run_id: str, model_span: str) -> dict:
    pool = await get_db_pool()
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)
    await _append(writer, run_id, "model.completed", {"model": "deepseek-flash", "durationMs": 20, "callIndex": 1}, model_span)
    await _append(writer, run_id, "run.finished", {"status": "completed"}, run_id)
    await writer.finish(run_id=run_id, status="completed", capture_status="ok")
    return {"runId": run_id, "status": "completed"}


async def _consume(stream) -> list[str]:
    from tracing.tee import traced_turn

    user_id, chat_id = await _bob_context(await get_db_pool())
    out = []
    async for chunk in traced_turn(
        stream, chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="fixture"
    ):
        out.append(chunk)
    return out


def _frame(event: str, data) -> str:
    nl = chr(10)
    return "event: " + event + nl + "data: " + json.dumps(data) + nl + nl


async def _run_frames(frames: list[str]) -> dict:
    async def stream():
        for frame in frames:
            yield frame

    from tracing.tee import traced_turn

    pool = await get_db_pool()
    user_id, chat_id = await _bob_context(pool)
    async for _chunk in traced_turn(
        stream(), chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="fixture"
    ):
        pass
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT run_id,status,capture_status,dropped_events,capture_error,last_seq
               FROM agent_trace_runs WHERE chat_id=$1 AND model_name='fixture'
               ORDER BY started_at DESC LIMIT 1""",
            chat_id,
        )
        types = await conn.fetch(
            "SELECT type FROM agent_trace_events WHERE run_id=$1 ORDER BY seq", row["run_id"]
        )
    return {
        "runId": row["run_id"],
        "status": row["status"],
        "captureStatus": row["capture_status"],
        "droppedEvents": row["dropped_events"],
        "captureError": row["capture_error"],
        "eventTypes": [item["type"] for item in types],
    }


async def tap_faults() -> dict:
    start = _frame("message", {"type": "message_start", "message": {"model": "m", "usage": {}}})
    stop = _frame("message", {"type": "message_stop"})
    malformed = await _run_frames(["event: message" + chr(10) + "data: {bad json" + chr(10) + chr(10), start, stop])
    broken_usage = _frame(
        "message", {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": "x"}}}
    )
    inspection = await _run_frames([broken_usage, stop])
    ignored = await _run_frames(
        [
            _frame("steering", {"type": "steering_message"}),
            _frame("ping", {}),
            _frame("save_message", {"role": "assistant", "content": []}),
            start,
            stop,
        ]
    )
    return {"malformed": malformed, "inspection": inspection, "ignored": ignored}


async def finish_only(run_id: str) -> dict:
    pool = await get_db_pool()
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)
    await writer.finish(run_id=run_id, status="completed", capture_status="ok")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status,last_seq,seal_version FROM agent_trace_runs WHERE run_id=$1", run_id
        )
    return {"runId": run_id, **dict(row)}


async def append_many(run_id: str, span: str, count: int) -> dict:
    pool = await get_db_pool()
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)
    for index in range(count):
        await _append(writer, run_id, "tool.completed", {"tool": "search", "index": index}, span)
    async with pool.acquire() as conn:
        last = await conn.fetchval(
            "SELECT last_seq FROM agent_trace_runs WHERE run_id=$1", run_id
        )
    return {"runId": run_id, "lastSeq": last}


async def sanitization() -> dict:
    from tools.registry import ToolResult
    from tools.traced_handler import TracedToolHandler
    from tracing.context import bind_run, reset_run

    class LeakyHandler:
        def get_tools(self):
            return []

        def can_handle(self, name):
            return name == "hr_preview_leave"

        def requires_approval(self, name):
            return False

        async def execute(self, name, tool_input, context):
            return ToolResult(
                content=[{"type": "text", "text": "LEAK_MARKER_9f2 upstream body token=abc123"}],
                is_error=True,
            )

    pool = await get_db_pool()
    user_id, chat_id = await _bob_context(pool)
    seal = os.environ.get("AGENT_TRACE_SEAL_KEY", "").strip()
    writer = TraceWriter(pool, seal.encode() if seal else None)
    run = await writer.start_run(
        chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="fixture"
    )
    token = bind_run(run)
    try:
        await TracedToolHandler(LeakyHandler()).execute("hr_preview_leave", {"query": "LEAK_MARKER"}, {})
    finally:
        reset_run(token)
        await writer.finish(run_id=run.run_id, status="completed", capture_status="ok")

    async def stream():
        nl = chr(10)
        yield "event: stream_error" + nl + "data: " + json.dumps({"message": "LEAK_MARKER stream body"}) + nl + nl

    from tracing.tee import traced_turn

    async for _ in traced_turn(
        stream(), chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="fixture"
    ):
        pass
    async with pool.acquire() as conn:
        latest = await conn.fetchval(
            """SELECT run_id FROM agent_trace_runs WHERE chat_id=$1 AND model_name='fixture'
               ORDER BY started_at DESC LIMIT 1""",
            chat_id,
        )
        leaked = await conn.fetchval(
            """SELECT count(*) FROM agent_trace_events
               WHERE run_id = ANY($1::text[]) AND payload::text LIKE '%LEAK_MARKER%'""",
            [run.run_id, latest],
        )
        payload = await conn.fetchval(
            """SELECT payload FROM agent_trace_events
               WHERE run_id=$1 AND type='tool.failed'""",
            run.run_id,
        )
    return {
        "toolPayload": payload,
        "streamRunId": latest,
        "markerRows": leaked,
    }


async def not_sealed() -> dict:
    pool = await get_db_pool()
    user_id, chat_id = await _bob_context(pool)
    writer = TraceWriter(pool, None)
    run = await writer.start_run(
        chat_id=chat_id, user_id=user_id, user_message_id=None, model_name="deepseek-flash"
    )
    assert run is not None
    span = new_id()
    await _append(writer, run.run_id, "run.started", {"model": "no-seal-fixture"}, run.run_id)
    await _append(writer, run.run_id, "model.started", {"model": "deepseek-flash", "callIndex": 1}, span)
    await _append(writer, run.run_id, "model.completed", {"model": "deepseek-flash", "durationMs": 5}, span)
    await _append(writer, run.run_id, "run.finished", {"status": "completed"}, run.run_id)
    await writer.finish(run_id=run.run_id, status="completed", capture_status="ok")
    return {"chatId": chat_id, "runId": run.run_id}


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "multi-draft":
        result = asyncio.run(multi_draft())
    elif command == "running-create":
        result = asyncio.run(running_create())
    elif command == "many-drafts":
        result = asyncio.run(many_drafts())
    elif command == "stale-create":
        result = asyncio.run(stale_create())
    elif command in ("stale-finish", "running-finish"):
        result = asyncio.run(stale_finish(sys.argv[2], sys.argv[3]))
    elif command == "tap-faults":
        result = asyncio.run(tap_faults())
    elif command == "finish-only":
        result = asyncio.run(finish_only(sys.argv[2]))
    elif command == "append-many":
        result = asyncio.run(append_many(sys.argv[2], sys.argv[3], int(sys.argv[4])))
    elif command == "sanitization":
        result = asyncio.run(sanitization())
    elif command == "not-sealed":
        result = asyncio.run(not_sealed())
    else:
        raise SystemExit(f"unknown command {command!r}")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
