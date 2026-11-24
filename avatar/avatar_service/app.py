import asyncio
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .db import Store, append, data_dir, digest, message_view, now, password_hash, summary, uid, user_view, valid_password
from .hermes import Hermes
from .receiver import Receiver
from .hr_bridge import HrBridge
from .operations import Operations, routes as operation_routes
from .services import CODES, ServiceBroker
from .stream import Runtime, Turn

LOGIN_TTL = 12 * 60 * 60 * 1000


def create_app(directory=None, delay=0.22):
    bridge = HrBridge()
    services = ServiceBroker(bridge)

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal store, runtime
        store = Store(directory if directory is not None else data_dir())
        services.start(store)
        hermes = Hermes(store, bridge, services) if os.getenv("PY_AVATAR_HERMES_BIN") else None
        runtime = Runtime(store, delay, hermes)
        services.operations = Operations(services, runtime.publish)
        bridge.start(store, runtime.publish)
        bridge.receiver = Receiver(runtime) if hermes else None
        try:
            yield
        finally:
            await runtime.close()
            if bridge.receiver:
                await bridge.receiver.close()

    store = runtime = None
    app = FastAPI(title="Python Avatar", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        if _request.url.path.startswith("/internal/services/"):
            headers = exc.headers or {}
            return JSONResponse({"ok": False, "code": headers.get("X-Avatar-Error-Code",
                                CODES.get(exc.status_code, "FORBIDDEN" if exc.status_code == 401 else "UPSTREAM_ERROR")),
                                 "message": exc.detail, "requestId": headers.get("X-Avatar-Request-Id")},
                                status_code=exc.status_code, headers=exc.headers)
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
                429: "limit_exceeded", 502: "upstream_error", 504: "upstream_timeout"}.get(exc.status_code, "bad_request")
        return JSONResponse({"error": code, "message": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, _exc):
        return JSONResponse({"error": "bad_request", "message": "请求格式无效"}, status_code=400)

    async def current(authorization: str = Header(default="")):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 200:
            raise HTTPException(401, "请先登录 Avatar")
        key = digest(token)
        row = store.login_user(key)
        if row is None:
            raise HTTPException(401, "登录已失效，请重新登录")
        return row, key

    def bootstrap(row):
        return {"user": user_view(row), "org": {"name": "Avatar Python 测试"},
                "service": {"version": app.version, "canChangePassword": True}, "models": []}

    def own_session(db, user_id, session_id):
        row = db.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "会话不存在")
        return row

    def update_event(owner, item, kind="session.updated"):
        runtime.publish(owner, {"type": kind, "session": item})

    @app.get("/health")
    async def health():
        return {"ok": True, "service": app.version,
                "mode": "hermes" if runtime.hermes else "mock",
                "hermesConfigured": runtime.hermes is not None,
                "hermesAcpReady": runtime.acp_ready}

    @app.post("/v1/auth/login")
    async def login(body: dict):
        handle, password = body.get("username"), body.get("password")
        if not isinstance(handle, str) or not isinstance(password, str) or len(password) > 512:
            raise HTTPException(401, "用户名或密码不正确")
        with store.open() as db:
            user = db.execute("SELECT * FROM users WHERE handle=?", (handle.strip().lower(),)).fetchone()
        if not await asyncio.to_thread(valid_password, user, password):
            raise HTTPException(401, "用户名或密码不正确")
        token = secrets.token_urlsafe(32)
        expires = now() + LOGIN_TTL
        with store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
            if fresh is None or fresh["password_hash"] != user["password_hash"]:
                raise HTTPException(401, "凭据已更新，请重新登录")
            db.execute("DELETE FROM logins WHERE expires_at<=?", (now(),))
            db.execute("INSERT INTO logins VALUES (?,?,?)", (digest(token), user["id"], expires))
        return {"token": token, "tokenExpiresAt": expires, "bootstrap": bootstrap(user),
                "departmentKeys": [], "serviceKeys": []}

    @app.post("/v1/auth/change-password")
    async def change_password(body: dict, auth=Depends(current)):
        user, login_key = auth
        old, new = body.get("currentPassword"), body.get("newPassword")
        if not isinstance(old, str) or not 1 <= len(old) <= 512:
            raise HTTPException(400, "请输入当前密码")
        if not isinstance(new, str) or not 12 <= len(new) <= 512 or old == new:
            raise HTTPException(400, "新密码须为 12–512 个字符，并与当前密码不同")
        if not await asyncio.to_thread(valid_password, user, old):
            raise HTTPException(400, "当前密码不正确")
        salt = secrets.token_hex(16)
        hashed = await asyncio.to_thread(password_hash, new, salt)
        with store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("""UPDATE users SET salt=?,password_hash=?
                WHERE id=? AND password_hash=? AND EXISTS (
                    SELECT 1 FROM logins WHERE token_hash=? AND user_id=users.id AND expires_at>?
                )""", (salt, hashed, user["id"], user["password_hash"], login_key, now()))
            if updated.rowcount != 1:
                raise HTTPException(401, "登录或凭据已失效，请重新登录")
            db.execute("DELETE FROM logins WHERE user_id=?", (user["id"],))
        return {"ok": True}

    @app.post("/v1/auth/logout")
    async def logout(auth=Depends(current)):
        with store.open() as db:
            db.execute("DELETE FROM logins WHERE token_hash=?", (auth[1],))
        return {"ok": True}

    @app.get("/v1/auth/session")
    async def auth_session(auth=Depends(current)):
        return {"user": user_view(auth[0]), "tokenExpiresAt": auth[0]["expires_at"]}

    @app.get("/v1/me")
    async def me(auth=Depends(current)):
        return bootstrap(auth[0])

    @app.get("/v1/colleagues")
    async def colleagues(auth=Depends(current)):
        with store.open() as db:
            return [{**user_view(row), "online": bool(runtime.streams.get(row["id"]))}
                    for row in db.execute("SELECT * FROM users WHERE id!=? ORDER BY handle", (auth[0]["id"],))]

    @app.get("/v1/tasks")
    @app.get("/v1/permissions")
    @app.get("/v1/identity/keys")
    @app.get("/v1/identity/service-keys")
    async def empty_list(_auth=Depends(current)):
        return []

    @app.get("/v1/sessions")
    async def sessions(archived: bool = Query(False), auth=Depends(current)):
        with store.open() as db:
            rows = db.execute(
                "SELECT * FROM sessions WHERE user_id=? AND (? OR archived=0) ORDER BY pinned DESC,updated_at DESC",
                (auth[0]["id"], int(archived)),
            )
            return [summary(db, row) for row in rows]

    @app.post("/v1/sessions", status_code=201)
    async def create_session(body: dict, auth=Depends(current)):
        owner = auth[0]["id"]
        title = body.get("title", "")
        if not isinstance(title, str):
            raise HTTPException(400, "会话标题必须是文本")
        session_id, stamp = uid("ses"), now()
        with store.open() as db:
            db.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                       (session_id, owner, title.strip()[:160] or "新对话", stamp, stamp))
            item = summary(db, own_session(db, owner, session_id))
        update_event(owner, item, "session.created")
        return item

    @app.get("/v1/sessions/{session_id}")
    async def detail(session_id: str, auth=Depends(current)):
        owner = auth[0]["id"]
        with store.open() as db:
            row = own_session(db, owner, session_id)
            if row["unread"]:
                db.execute("UPDATE sessions SET unread=0 WHERE id=?", (session_id,))
                row = own_session(db, owner, session_id)
            item = summary(db, row)
            item["operations"] = services.operations.list_for(db, owner, session_id)
            item["leaveConfirmations"] = bridge.confirmations.list_for(db, owner, session_id)
            item["messages"] = [message_view(message) for message in db.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY rowid", (session_id,)
            )]
        turn = runtime.turns.get(session_id)
        if turn:
            item["activeTurn"] = turn.snapshot()
        return item

    @app.patch("/v1/sessions/{session_id}")
    async def patch_session(session_id: str, body: dict, auth=Depends(current)):
        owner = auth[0]["id"]
        with store.open() as db:
            row = own_session(db, owner, session_id)
            title = body.get("title", row["title"])
            pinned = body.get("pinned", bool(row["pinned"]))
            archived = body.get("archived", bool(row["archived"]))
            if not isinstance(title, str) or not isinstance(pinned, bool) or not isinstance(archived, bool):
                raise HTTPException(400, "会话设置格式无效")
            db.execute("UPDATE sessions SET title=?,pinned=?,archived=?,updated_at=? WHERE id=?",
                       (title.strip()[:160] or row["title"], int(pinned), int(archived), now(), session_id))
            item = summary(db, own_session(db, owner, session_id))
        update_event(owner, item)
        return item

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, auth=Depends(current)):
        owner = auth[0]["id"]
        with store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM operations WHERE owner=? AND session=? AND status IN ('executing','unknown')",
                          (owner, session_id)).fetchone():
                raise HTTPException(409, "请先核实该会话中的操作结果")
            cursor = db.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, owner))
        if cursor.rowcount:
            turn = runtime.turns.get(session_id)
            if turn:
                turn.stop.set()
            runtime.publish(owner, {"type": "session.deleted", "sessionId": session_id})
        return Response(status_code=204)

    @app.post("/v1/sessions/{session_id}/read")
    async def read_session(session_id: str, auth=Depends(current)):
        owner = auth[0]["id"]
        with store.open() as db:
            row = own_session(db, owner, session_id)
            changed = bool(row["unread"])
            if changed:
                db.execute("UPDATE sessions SET unread=0 WHERE id=?", (session_id,))
                row = own_session(db, owner, session_id)
            item = summary(db, row)
        if changed:
            update_event(owner, item)
        return item

    @app.post("/v1/sessions/{session_id}/messages", status_code=202)
    async def send(session_id: str, body: dict, auth=Depends(current)):
        owner = auth[0]["id"]
        text = body.get("text", "")
        if body.get("attachmentIds"):
            raise HTTPException(400, "当前只支持文本")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
            raise HTTPException(400, "请输入 1–500 字的消息")
        text = text.strip()
        with store.open() as db:
            row = own_session(db, owner, session_id)
            if session_id in runtime.turns:
                raise HTTPException(409, "当前会话正在回复")
            message = append(db, session_id, "user", text)
            if row["title"] == "新对话":
                db.execute("UPDATE sessions SET title=? WHERE id=?", (text.splitlines()[0][:48], session_id))
            item = summary(db, own_session(db, owner, session_id))
        turn = Turn(session_id, owner, auth[1])
        runtime.turns[session_id] = turn
        runtime.publish(owner, {"type": "message.appended", "sessionId": session_id, "message": message})
        update_event(owner, item)
        turn.task = asyncio.create_task(runtime.reply(turn, text))
        return {"message": message, "turnId": turn.turn_id, "state": "running"}

    @app.post("/v1/sessions/{session_id}/cancel")
    async def cancel(session_id: str, auth=Depends(current)):
        with store.open() as db:
            own_session(db, auth[0]["id"], session_id)
        turn = runtime.turns.get(session_id)
        if turn:
            turn.stop.set()
        return {"cancelled": turn is not None}

    @app.get("/v1/stream")
    async def stream(auth=Depends(current)):
        return StreamingResponse(runtime.stream(auth[0]["id"], auth[1]), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    app.include_router(bridge.routes(current))
    app.include_router(services.routes(current))
    app.include_router(operation_routes(lambda: services.operations, current))
    return app


app = create_app()

