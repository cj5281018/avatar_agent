import asyncio
import json
import logging

from .db import ServiceError, identifier, now, setting
from .protocol import post_avatar

log = logging.getLogger(__name__)


def record(db, store, row, sender, direction, items, source_session):
    for item in items:
        db.execute("""INSERT INTO notification_outbox
            (id, leave_id, sender, recipient, direction, payload, source_session, next_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(leave_id,sender,recipient,direction) DO NOTHING""",
            (identifier("evt"), row["id"], sender, item["handle"], direction,
             store.seal(json.dumps(item, ensure_ascii=False)), source_session, now()))


def resend(db, identity, leave, direction):
    if direction not in ("request", "decision"):
        raise ServiceError("direction 必须是 request 或 decision")
    args = (leave["id"], identity["handle"], direction)
    rows = db.execute("""SELECT id, delivered_at FROM notification_outbox
                         WHERE leave_id=? AND sender=? AND direction=?""", args).fetchall()
    if not rows:
        raise ServiceError("没有可补发的本账号通知", 404)
    db.execute("""UPDATE notification_outbox SET next_at=? WHERE leave_id=? AND sender=?
                  AND direction=? AND delivered_at IS NULL""", (now(), *args))
    return {"requestId": leave["id"], "delivery": [
        {"eventId": row["id"], "status": "delivered" if row["delivered_at"] else "queued"} for row in rows
    ]}


def publish_batch(store, origin):
    with store.connection() as db:
        key = setting(db, "service.key")
        if not key:
            return
        rows = db.execute("""SELECT * FROM notification_outbox WHERE delivered_at IS NULL AND next_at<=?
                            ORDER BY next_at,id LIMIT 20""", (now(),)).fetchall()
    for row in rows:
        event = {name: row[name] for name in ("id", "leave_id", "sender", "recipient", "direction", "source_session")}
        event["item"] = json.loads(store.open(row["payload"]))
        accepted = False
        try:
            reply = post_avatar("/internal/hr/deliveries", {"serviceKey": store.open(key), "event": event}, origin)
            accepted = reply.get("ok") is True and reply.get("eventId") == row["id"]
        except ServiceError:
            log.info("HR notification %s pending retry", row["id"])
        with store.connection(write=True) as db:
            if accepted:
                db.execute("UPDATE notification_outbox SET delivered_at=? WHERE id=?", (now(), row["id"]))
            else:
                db.execute("""UPDATE notification_outbox SET attempts=attempts+1,next_at=?
                              WHERE id=? AND delivered_at IS NULL AND next_at=?""",
                           (now() + min(60000, 1000 * 2 ** min(row["attempts"], 6)),
                            row["id"], row["next_at"]))


async def run(store, origin):
    while True:
        try:
            await asyncio.to_thread(publish_batch, store, origin)
        except Exception:
            log.exception("HR outbox poll failed")
        await asyncio.sleep(1)
