import asyncio
import logging
from types import SimpleNamespace

from fastapi import HTTPException

from .db import append, now, summary, uid

log = logging.getLogger("uvicorn.error")


class Receiver:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.lock = asyncio.Lock()
        self.pending = set()
        self.events = {}
        with self.store.open() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS hr_deliveries (
                leave_id TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                direction TEXT NOT NULL, source_session TEXT NOT NULL, target_session TEXT NOT NULL,
                message_id TEXT NOT NULL, mode TEXT NOT NULL,
                PRIMARY KEY(leave_id, sender, recipient, direction)
            )""")

    async def accept(self, event):
        if not isinstance(event, dict) or not all(isinstance(event.get(k), str) and event[k]
                for k in ("id", "sender", "recipient", "leave_id", "direction", "source_session")):
            raise HTTPException(400, "通知格式无效")
        item = event.get("item")
        if (event["direction"] not in ("request", "decision") or not isinstance(item, dict)
                or item.get("handle") != event["recipient"]
                or not all(isinstance(item.get(k), str) and item[k] for k in ("title", "text"))):
            raise HTTPException(400, "通知内容无效")
        with self.store.open() as db:
            sender = db.execute("SELECT id FROM users WHERE handle=?", (event["sender"],)).fetchone()
            recipient = db.execute("SELECT id FROM users WHERE handle=?", (event["recipient"],)).fetchone()
            if not sender or not recipient:
                raise HTTPException(409, "通知涉及的 Avatar 用户不存在")
            saved = db.execute("""SELECT mode FROM hr_deliveries
                WHERE leave_id=? AND sender=? AND recipient=? AND direction=?""",
                (event["leave_id"], sender["id"], recipient["id"], event["direction"])).fetchone()
        if saved:
            return {"ok": True, "eventId": event["id"], "mode": saved["mode"]}
        event_id = event["id"]
        if event_id not in self.events and len(self.events) < 20:
            turn = SimpleNamespace(owner=sender["id"], session_id=event["source_session"])
            task = asyncio.create_task(self.deliver(turn, event["leave_id"], event["direction"], item))
            self.events[event_id] = task
            self.pending.add(task)

            def complete(finished):
                self.events.pop(event_id, None)
                self.finished(finished)

            task.add_done_callback(complete)
        return {"ok": False, "eventId": event_id, "status": "queued"}

    async def forward(self, turn, action, result):
        data = result.get("data", {})
        coord = data.get("coordination", {})
        leave_id = data.get("request", {}).get("id")
        if not leave_id or action not in ("request_leave", "review_leave"):
            return
        direction = "request" if action == "request_leave" else "decision"
        items = coord.get("deliveries", []) if direction == "request" else [coord.get("notifyRequester")]
        result["delivery"] = []
        for item in filter(None, items):
            try:
                task = asyncio.create_task(self.deliver(turn, leave_id, direction, item))
                self.pending.add(task)
                task.add_done_callback(self.finished)
                status = await asyncio.wait_for(asyncio.shield(task), 8)
            except asyncio.TimeoutError:
                status = {"status": "queued", "handle": item["handle"]}
            except Exception:
                log.exception("HR delivery failed for %s", leave_id)
                status = {"status": "failed", "handle": item["handle"]}
            result["delivery"].append(status)

    def finished(self, task):
        self.pending.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Queued HR delivery failed", exc_info=task.exception())

    async def close(self):
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def deliver(self, turn, leave_id, direction, item):
        with self.store.open() as db:
            sender = db.execute("SELECT * FROM users WHERE id=?", (turn.owner,)).fetchone()
            recipient = db.execute("SELECT * FROM users WHERE handle=?", (item["handle"],)).fetchone()
        if not recipient:
            return {"status": "failed", "handle": item["handle"]}
        owner = recipient["id"]
        key = (leave_id, turn.owner, owner, direction)
        async with self.lock:
            with self.store.open() as db:
                previous = db.execute(
                    "SELECT * FROM hr_deliveries WHERE leave_id=? AND sender=? AND recipient=? AND direction=?", key
                ).fetchone()
                if previous:
                    return {"status": "delivered", "mode": previous["mode"], "handle": item["handle"]}
                linked = db.execute(
                    """SELECT s.id FROM hr_deliveries d JOIN sessions s ON s.id=d.source_session
                       WHERE d.leave_id=? AND d.sender=? AND d.recipient=? AND d.direction='request' AND s.user_id=?""",
                    (leave_id, owner, turn.owner, owner),
                ).fetchone()
            source = item["text"]
            mode = "receiver"
            try:
                rendered = await self.runtime.hermes.receive(owner, source, leave_id)
            except Exception as error:
                log.warning("Receiver fallback %s -> %s %s (%s)", sender["handle"], item["handle"], leave_id, type(error).__name__)
                rendered, mode = source, "fallback"
            session_id = linked["id"] if linked else uid("ses")
            origin = {"kind": "receiver", "fromUserId": sender["id"],
                      "fromHandle": sender["handle"], "fromDisplayName": sender["name"]}
            with self.store.open() as db:
                db.execute("BEGIN IMMEDIATE")
                previous = db.execute(
                    "SELECT mode FROM hr_deliveries WHERE leave_id=? AND sender=? AND recipient=? AND direction=?", key
                ).fetchone()
                if previous:
                    return {"status": "delivered", "mode": previous["mode"], "handle": item["handle"]}
                existing = db.execute("SELECT 1 FROM sessions WHERE id=? AND user_id=?", (session_id, owner)).fetchone()
                if not existing:
                    stamp = now()
                    db.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                               (session_id, owner, item["title"], stamp, stamp))
                message = append(db, session_id, "assistant", rendered, True, origin)
                db.execute("INSERT INTO hr_deliveries VALUES (?,?,?,?,?,?,?,?)",
                           (*key, turn.session_id, session_id, message["id"], mode))
                session = summary(db, db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())
            self.runtime.publish(owner, {"type": "session.updated", "session": session})
            self.runtime.publish(owner, {"type": "message.appended", "sessionId": session_id, "message": message})
            log.info("HR %s %s %s -> %s mode=%s", leave_id, direction, sender["handle"], item["handle"], mode)
            return {"status": "delivered", "mode": mode, "handle": item["handle"]}

