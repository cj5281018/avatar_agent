import asyncio
import json
import logging
from dataclasses import dataclass, field

from .db import append, now, summary, uid
from .hermes import AcpError

log = logging.getLogger(__name__)


@dataclass
class Turn:
    session_id: str
    owner: str
    login_key: str
    turn_id: str = field(default_factory=lambda: uid("turn"))
    started_at: int = field(default_factory=now)
    text: str = ""
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task = None
    activity: dict = field(default_factory=dict)
    publish: object = None

    def trace(self, entry_id, title, detail, status="completed", kind="tool"):
        previous = self.activity.get(entry_id)
        offset = previous["offset"] if previous else len(self.text.encode("utf-16-le", errors="surrogatepass")) // 2
        entry = {"id": entry_id, "kind": kind, "title": title, "detail": detail,
                 "status": status, "timestamp": now(), "offset": offset}
        self.activity[entry_id] = entry
        if self.publish:
            self.publish(self.owner, self.event("turn.activity", entry=entry))

    def event(self, kind, **fields):
        return {"type": kind, "sessionId": self.session_id, "turnId": self.turn_id, **fields}

    def snapshot(self):
        return {"sessionId": self.session_id, "turnId": self.turn_id, "startedAt": self.started_at,
                "state": "running", "text": self.text, "activity": list(self.activity.values())}


class Runtime:
    def __init__(self, store, delay, hermes=None):
        self.store = store
        self.delay = delay
        self.hermes = hermes
        self.acp_ready = False
        self.streams = {}
        self.turns = {}

    def publish(self, owner, event):
        for queue in self.streams.get(owner, ()):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(event)

    async def stream(self, owner, key):
        queue = asyncio.Queue(maxsize=128)
        queues = self.streams.setdefault(owner, set())
        queues.add(queue)
        try:
            yield frame({"type": "stream.ready", "at": now()})
            while self.store.login_user(key):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if event is None or not self.store.login_user(key):
                    break
                yield frame(event)
        finally:
            queues.discard(queue)
            if not queues:
                self.streams.pop(owner, None)

    async def reply(self, turn, prompt):
        session_id, owner = turn.session_id, turn.owner
        turn.publish = self.publish
        try:
            await asyncio.sleep(self.delay)
            self.publish(owner, turn.event("turn.started", state="running"))

            def emit(chunk):
                turn.text += chunk
                self.publish(owner, turn.event("turn.chunk", channel="text", text=chunk))

            if self.hermes:
                await self.hermes.reply(turn, prompt, emit)
                if turn.text:
                    self.acp_ready = True
            else:
                reply = "【Python Avatar 模拟回复】\n\n收到你的消息。以下文字分段返回，用来验证桌面端的实时显示和历史保存。\n\n你发送的是：" + prompt
                for index in range(0, len(reply), 12):
                    try:
                        await asyncio.wait_for(turn.stop.wait(), timeout=self.delay)
                        break
                    except asyncio.TimeoutError:
                        emit(reply[index:index + 12])
            message = None
            with self.store.open() as db:
                row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
                if row is not None and not turn.stop.is_set():
                    message = append(db, session_id, "assistant", turn.text, not self.streams.get(owner), activity=list(turn.activity.values()))
                    row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
                    updated = summary(db, row)
            if message is None:
                self.publish(owner, turn.event("turn.finished", state="cancelled"))
            else:
                self.publish(owner, turn.event("turn.finished", state="completed", message=message))
                self.publish(owner, {"type": "session.updated", "session": updated})
        except Exception as error:
            log.warning("Avatar reply failed for %s (%s)", session_id, type(error).__name__)
            detail = str(error) if isinstance(error, AcpError) else "回复处理失败，请重试。"
            with self.store.open() as db:
                if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                    message = append(db, session_id, "system", detail, activity=list(turn.activity.values()))
                else:
                    message = None
            self.publish(owner, turn.event("turn.finished", state="failed",
                                           error=detail, message=message))
        finally:
            self.turns.pop(session_id, None)

    async def close(self):
        tasks = [turn.task for turn in self.turns.values() if turn.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def frame(event):
    return "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"

