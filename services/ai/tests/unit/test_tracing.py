"""Unit tests for the per-turn trace ledger.

Written with the standard library test runner so they can run both in a bare
Python environment and inside the service image:

    python -m unittest discover -s tests/unit -p test_tracing.py -v

They never touch Postgres: ``FakePool`` implements the small slice of the
asyncpg API the writer uses, so the hash chain, sealing and tamper detection can
be exercised deterministically.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from tracing.canonical import (
    ALGORITHM,
    SEAL_VERSION,
    canonical_json,
    event_digest,
    seal_digest,
)
from tracing.writer import TraceWriter, clean_payload

GOLDEN_DIGEST = "8b11e8634c1bcc5ef3f8c8d2e91e89dbb3f23d3910ad26060fffb241be8d814a"
GOLDEN_SEAL = "5d90cd0816d8e348113f0935af70002cc1f7ef62d2c59ed7ced8ed617383cacd"


class _Transaction:
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class FakePool:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def acquire(self) -> "_Connection":
        return _Connection(self)


class _Connection:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> "_Connection":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "COALESCE(MAX(attempt)" in sql:
            chat_id, user_message_id = args
            attempts = [
                run["attempt"]
                for run in self.pool.runs.values()
                if run["chat_id"] == chat_id and run["user_message_id"] == user_message_id
            ]
            return max(attempts, default=0) + 1
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "SELECT last_seq, head_digest" in sql:
            run = self.pool.runs.get(args[0])
            if run is None:
                return None
            return {"last_seq": run["last_seq"], "head_digest": run["head_digest"]}
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def execute(self, sql: str, *args: Any) -> None:
        if "INSERT INTO agent_trace_runs" in sql:
            (run_id, chat_id, user_id, user_message_id, attempt, model_name, started_at) = args
            self.pool.runs[run_id] = {
                "run_id": run_id,
                "chat_id": chat_id,
                "user_id": user_id,
                "user_message_id": user_message_id,
                "attempt": attempt,
                "model_name": model_name,
                "started_at": started_at,
                "status": "running",
                "last_seq": 0,
                "head_digest": None,
                "seal": None,
                "capture_status": "ok",
                "dropped_events": 0,
            }
        elif "INSERT INTO agent_trace_events" in sql:
            (run_id, seq, event_id, span_id, parent_span_id, type, ts, payload, previous, digest) = args
            self.pool.events.append(
                {
                    "run_id": run_id,
                    "seq": seq,
                    "event_id": event_id,
                    "span_id": span_id,
                    "parent_span_id": parent_span_id,
                    "type": type,
                    "ts": ts,
                    # jsonb round-trips through JSON text, exactly like Postgres.
                    "payload": json.loads(payload) if isinstance(payload, str) else payload,
                    "previous_digest": previous,
                    "digest": digest,
                }
            )
        elif "UPDATE agent_trace_runs" in sql and "SET last_seq" in sql:
            run_id, seq, digest, _last_event_at = args
            self.pool.runs[run_id]["last_seq"] = seq
            self.pool.runs[run_id]["head_digest"] = digest
        elif "SET capture_status = 'degraded'" in sql:
            run_id, reason = args
            run = self.pool.runs[run_id]
            run["capture_status"] = "degraded"
            run["capture_error"] = run.get("capture_error") or reason
            run["dropped_events"] = run.get("dropped_events", 0) + 1
        elif "UPDATE agent_trace_runs" in sql and "SET status" in sql:
            (
                run_id,
                status,
                error_code,
                ended_at,
                assistant_message_id,
                seal_version,
                seal,
                capture_status,
                capture_error,
                dropped_events,
            ) = args
            run = self.pool.runs[run_id]
            run.update(
                status=status,
                error_code=error_code,
                ended_at=ended_at,
                seal_version=seal_version,
                seal=seal,
                capture_error=run.get("capture_error") or capture_error,
                dropped_events=max(run.get("dropped_events", 0), dropped_events),
            )
            if run.get("capture_status") != "degraded":
                run["capture_status"] = capture_status
        else:
            raise AssertionError(f"unexpected execute: {sql}")


def replay(events: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """Recompute the chain the way a reader would; returns (seq, digest)."""
    from tracing.writer import format_ts

    previous = None
    out = []
    for event in sorted(events, key=lambda item: item["seq"]):
        ts = event["ts"]
        digest = event_digest(
            run_id=event["run_id"],
            seq=event["seq"],
            event_id=event["event_id"],
            span_id=event["span_id"],
            parent_span_id=event["parent_span_id"],
            type=event["type"],
            ts=ts if isinstance(ts, str) else format_ts(ts),
            payload=event["payload"],
            previous_digest=previous,
        )
        out.append((event["seq"], digest))
        previous = digest
    return out


class CanonicalSpecTest(unittest.TestCase):
    def test_golden_vectors_match_the_shared_spec(self):
        """The web verifier must produce these same values (see digest.test.ts)."""
        self.assertEqual(
            event_digest(
                run_id="run-golden",
                seq=1,
                event_id="evt-golden",
                span_id="span-golden",
                parent_span_id=None,
                type="model.started",
                ts="2026-11-24T09:00:00.000Z",
                payload={"model": "deepseek-flash", "n": 2, "ok": True, "note": "年假"},
                previous_digest=None,
            ),
            GOLDEN_DIGEST,
        )
        self.assertEqual(
            canonical_json({"b": 1, "a": "年假", "c": [True, None]}),
            '{"a":"年假","b":1,"c":[true,null]}',
        )
        self.assertEqual(ALGORITHM, "sha256.canonical-json.v1")
        self.assertEqual(SEAL_VERSION, "hmac-sha256.v1")
        self.assertEqual(
            seal_digest(b"k1", run_id="run-golden", last_seq=1, head_digest=GOLDEN_DIGEST),
            GOLDEN_SEAL,
        )

    def test_clean_payload_rejects_values_that_cannot_be_canonicalised(self):
        cleaned = clean_payload(
            {
                "fraction": 0.5,
                "long": "x" * 900,
                "nested": {"ok": True, "none": None},
                "list": [1, 2],
            }
        )
        self.assertEqual(cleaned["fraction"], "0.5")
        self.assertEqual(len(cleaned["long"]), 600)
        self.assertEqual(cleaned["nested"], {"ok": True})
        self.assertEqual(cleaned["list"], [1, 2])


class WriterTest(unittest.IsolatedAsyncioTestCase):
    async def test_chain_appends_in_order_and_seals_the_head(self):
        pool = FakePool()
        writer = TraceWriter(pool, b"k1")
        run = await writer.start_run(
            chat_id="chat-1", user_id="user-1", user_message_id="msg-1", model_name="deepseek-flash"
        )
        self.assertIsNotNone(run)
        for index in range(3):
            await writer.append(
                run_id=run.run_id,
                type="tool.completed",
                payload={"tool": "search", "durationMs": index},
                span_id="span-1",
            )
        await writer.finish(run_id=run.run_id, status="completed")

        stored = pool.runs[run.run_id]
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["last_seq"], 3)
        rebuilt = replay(pool.events)
        self.assertEqual([seq for seq, _ in rebuilt], [1, 2, 3])
        self.assertEqual(rebuilt[-1][1], stored["head_digest"])
        self.assertEqual(stored["seal_version"], SEAL_VERSION)
        self.assertEqual(
            stored["seal"],
            seal_digest(b"k1", run_id=run.run_id, last_seq=3, head_digest=stored["head_digest"]),
        )
        self.assertEqual(stored["capture_status"], "ok")

    async def test_dropped_event_is_reported_as_degraded_capture(self):
        import os

        pool = FakePool()
        writer = TraceWriter(pool, None)
        run = await writer.start_run(
            chat_id="chat-1", user_id="user-1", user_message_id=None, model_name=None
        )
        recorded = await writer.append(
            run_id=run.run_id, type="run.started", payload={}, span_id="s1"
        )
        self.assertTrue(recorded)

        os.environ["AVATAR2_TRACE_FAULT_EVENT"] = "model.started"
        try:
            dropped = await writer.append(
                run_id=run.run_id, type="model.started", payload={}, span_id="s2"
            )
        finally:
            os.environ.pop("AVATAR2_TRACE_FAULT_EVENT")
        self.assertFalse(dropped)
        self.assertEqual(pool.runs[run.run_id]["capture_status"], "degraded")
        self.assertEqual(pool.runs[run.run_id]["dropped_events"], 1)

        # A later successful write must not clear the degradation.
        await writer.append(run_id=run.run_id, type="run.finished", payload={}, span_id="s1")
        await writer.finish(run_id=run.run_id, status="completed", capture_status="ok")
        self.assertEqual(pool.runs[run.run_id]["capture_status"], "degraded")
        self.assertEqual(pool.runs[run.run_id]["status"], "completed")

    async def test_attempt_number_increases_per_user_message(self):
        pool = FakePool()
        writer = TraceWriter(pool, None)
        first = await writer.start_run(
            chat_id="chat-1", user_id="user-1", user_message_id="msg-1", model_name=None
        )
        await writer.finish(run_id=first.run_id, status="completed")
        second = await writer.start_run(
            chat_id="chat-1", user_id="user-1", user_message_id="msg-1", model_name=None
        )
        self.assertEqual(pool.runs[second.run_id]["attempt"], 2)

    async def test_concurrent_appends_do_not_fork_the_chain(self):
        pool = FakePool()
        writer = TraceWriter(pool, None)
        run = await writer.start_run(
            chat_id="chat-1", user_id="user-1", user_message_id=None, model_name=None
        )
        await asyncio.gather(
            *[
                writer.append(
                    run_id=run.run_id,
                    type="tool.started",
                    payload={"tool": "search"},
                    span_id=f"span-{index}",
                )
                for index in range(6)
            ]
        )
        self.assertEqual(sorted(event["seq"] for event in pool.events), [1, 2, 3, 4, 5, 6])
        rebuilt = [digest for _, digest in replay(pool.events)]
        stored = [event["digest"] for event in sorted(pool.events, key=lambda e: e["seq"])]
        self.assertEqual(rebuilt, stored)

    async def test_tampering_with_a_recorded_event_is_detectable(self):
        pool = FakePool()
        writer = TraceWriter(pool, b"k1")
        run = await writer.start_run(
            chat_id="chat-1", user_id="user-1", user_message_id=None, model_name=None
        )
        await writer.append(
            run_id=run.run_id, type="model.started", payload={"model": "m1"}, span_id="s1"
        )
        await writer.append(
            run_id=run.run_id, type="run.finished", payload={"status": "completed"}, span_id="s1"
        )
        await writer.finish(run_id=run.run_id, status="completed")

        pool.events[0]["payload"] = {"model": "tampered"}
        rebuilt = replay(pool.events)
        self.assertNotEqual(rebuilt[0][1], pool.events[0]["digest"])
        # The downstream digest is chained to the altered one, so it breaks too.
        self.assertNotEqual(rebuilt[1][1], pool.events[1]["digest"])

    async def test_unsealed_runs_are_still_recorded(self):
        pool = FakePool()
        writer = TraceWriter(pool, None)
        run = await writer.start_run(
            chat_id="chat-1", user_id="user-1", user_message_id=None, model_name=None
        )
        await writer.append(run_id=run.run_id, type="run.started", payload={}, span_id="s1")
        await writer.finish(run_id=run.run_id, status="failed", error_code="ProviderError")
        stored = pool.runs[run.run_id]
        self.assertIsNone(stored["seal"])
        self.assertEqual(stored["error_code"], "ProviderError")


if __name__ == "__main__":
    unittest.main()
