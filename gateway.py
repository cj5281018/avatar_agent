import hashlib
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from actions import install_actions
from bindings import EmployeeBindings
from plugins import (
    ActionContext,
    ActionError,
    DepartmentPlugin,
    PluginAction,
    build_catalog,
)
from avatar_service.db import Store, digest, now, uid
from avatar_service.hr_bridge import HrBridge
from avatar_service.leave_confirmation import LEAVE_TYPES, validate_leave
from avatar_service.stream import Turn

logger = logging.getLogger("uvicorn.error")


class LeaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    startDate: str
    endDate: str
    leaveType: str
    reason: str


leave_schema = LeaveInput.model_json_schema()
leave_schema["properties"]["leaveType"]["enum"] = sorted(LEAVE_TYPES)
leave_schema["properties"]["leaveType"]["description"] = "Use annual for 年假."


# ---------------------------------------------------------------------------
# Department handlers. They receive a trusted ActionContext; identity never
# comes from arguments.
# ---------------------------------------------------------------------------


async def hr_preview(ctx: ActionContext, arguments: dict) -> dict:
    fields = validate_leave(arguments)
    if not ctx.chat_id:
        # MCP clients without a chat session get a read-only preview.
        return {"type": "leave_preview", "input": fields, "persisted": False}
    with ctx.store.open() as db:
        db.execute(
            "INSERT OR IGNORE INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (ctx.chat_id, ctx.employee["id"], "工作台请假", now(), now()),
        )
    item = ctx.bridge.confirmations.propose(
        Turn(ctx.chat_id, ctx.employee["id"], ctx.login_key), fields
    )
    return {"type": "leave_preview", **item}


async def hr_list_my_requests(ctx: ActionContext, arguments: dict) -> dict:
    if arguments:
        raise ActionError("invalid_arguments", "该动作不接受参数")
    data = await ctx.bridge.invoke("list_my_requests", {}, ctx.login_key, "", uid("srq"))
    return data["data"]


async def testkit_write(ctx: ActionContext, arguments: dict) -> dict:
    """A write action that must never run through an ordinary tool entry."""
    with ctx.store.open() as db:
        db.execute("CREATE TABLE IF NOT EXISTS testkit_writes(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO testkit_writes DEFAULT VALUES")
    return {"type": "testkit_write", "written": True}


async def testkit_fail(ctx: ActionContext, arguments: dict) -> dict:
    """A registered read action that always fails, for isolation tests."""
    raise ActionError("tool_error", "测试插件执行失败")


async def testkit_counter(ctx: ActionContext, arguments: dict) -> dict:
    with ctx.store.open() as db:
        db.execute("CREATE TABLE IF NOT EXISTS testkit_writes(id INTEGER PRIMARY KEY)")
        count = db.execute("SELECT count(*) FROM testkit_writes").fetchone()[0]
    return {"type": "testkit_counter", "writes": count}


async def testkit_preview(ctx: ActionContext, arguments: dict) -> dict:
    # Deliberately reuses the action id "preview_leave" under another
    # department: routing is by tool name, not by action id alone.
    return {"type": "echo", "department": "testkit", "action": "preview_leave", "pluginVersion": "0.0.1"}


async def testkit_echo(ctx: ActionContext, arguments: dict) -> dict:
    message = arguments.get("message", "")
    if not isinstance(message, str):
        raise ActionError("invalid_arguments", "参数不正确")
    return {
        "type": "echo",
        "department": "testkit",
        "action": "echo",
        "pluginVersion": "0.0.1",
        "message": message[:100],
    }


HR_PLUGIN = DepartmentPlugin(
    id="hr",
    display_name="人力资源",
    version="1.0.0",
    web_entry="/web/me",
    actions=(
        PluginAction(
            id="preview_leave",
            tool="hr_preview_leave",
            description="预览请假日期、假别和事由，返回待员工核对的内容",
            input_schema=leave_schema,
            handler=hr_preview,
            policy="draft",
            summary_fields=("startDate", "endDate", "leaveType"),
        ),
        PluginAction(
            id="list_my_requests",
            tool="hr_list_my_requests",
            description="查询当前员工的请假申请",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=hr_list_my_requests,
            policy="read",
            summary_fields=("id", "status"),
        ),
    ),
)

TEST_PLUGIN = DepartmentPlugin(
    id="testkit",
    display_name="接入测试",
    version="0.0.1",
    actions=(
        PluginAction(
            id="preview_leave",
            tool="testkit_preview_leave",
            description="仅用于接入测试的同名动作，只读",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=testkit_preview,
            policy="read",
        ),
        PluginAction(
            id="write",
            tool="testkit_write",
            description="仅用于策略测试的写动作，普通工具入口必须拒绝",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=testkit_write,
            policy="write",
        ),
        PluginAction(
            id="fail",
            tool="testkit_fail",
            description="仅用于故障隔离测试的只读动作，执行时必定失败",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=testkit_fail,
            policy="read",
        ),
        PluginAction(
            id="counter",
            tool="testkit_counter",
            description="读取测试写动作的调用次数",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=testkit_counter,
            policy="read",
        ),
        PluginAction(
            id="echo",
            tool="testkit_echo",
            description="仅用于接入测试的只读回显，不返回任何业务数据",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string", "description": "回显文本"}},
                "additionalProperties": False,
            },
            handler=testkit_echo,
            policy="read",
        ),
    ),
)


def _catalog():
    plugins = [HR_PLUGIN]
    if os.environ.get("AVATAR2_ENABLE_TEST_PLUGIN", "").strip() == "1":
        plugins.append(TEST_PLUGIN)
    return build_catalog(plugins)


CATALOG = _catalog()


async def execute_action(
    ctx: ActionContext, tool: str, arguments: object, *, surface: str = "tool"
) -> dict:
    """Shared dispatch for every entry point (MCP and internal tools).

    Ordinary tool entries execute only ``read`` and ``draft`` actions. A
    ``write`` action (or an unknown policy, defensively) is refused before the
    handler runs; official writes stay on their human-triggered paths.
    """
    route = CATALOG.route(tool) if isinstance(tool, str) else None
    if route is None:
        raise ActionError("unknown_tool", "未知工具或参数不正确")
    if not isinstance(arguments, dict):
        raise ActionError("invalid_arguments", "参数格式不正确")
    _plugin, action = route
    if surface == "tool" and action.policy not in ("read", "draft"):
        raise ActionError("approval_required", "该动作需要人工确认后执行", 403)
    try:
        return await action.handler(ctx, arguments)
    except ActionError:
        raise
    except HTTPException as exc:
        code = "upstream_unavailable" if exc.status_code >= 500 else "invalid_arguments"
        raise ActionError(code, str(exc.detail), 400) from exc
    except ValidationError as exc:
        raise ActionError("invalid_arguments", "参数校验失败", 400) from exc
    except Exception:
        logger.exception("action %s failed", tool)
        raise ActionError("tool_error", "工具执行失败") from None


def _service_allowed(authorization: str) -> bool:
    secret = os.environ.get("AVATAR2_INTERNAL_TOKEN", "")
    return bool(secret) and compare_digest(authorization.encode(), ("Bearer " + secret).encode())


def create_app(directory=None):
    bridge = HrBridge()
    bridge.receiver = None

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(directory or Path(os.environ["AVATAR2_DATA_DIR"]))
        with app.state.store.open() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS web_tickets (
                    ticket_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    target TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER,
                    binding_version TEXT
                )"""
            )
        with app.state.store.open() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(web_tickets)")}
            if "binding_version" not in columns:
                db.execute("ALTER TABLE web_tickets ADD COLUMN binding_version TEXT")
        bridge.start(app.state.store, lambda owner, event: None)
        encryption_key = os.environ.get('AVATAR2_BINDING_KEY')
        app.state.bindings = EmployeeBindings(app.state.store, encryption_key.encode()) if encryption_key else None
        yield

    app = FastAPI(title="avatar2", lifespan=lifespan)

    def authenticate(request: Request, authorization: str = Header(default="")):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 200:
            raise HTTPException(401, "需要员工凭据")
        key = digest(token)
        user = request.app.state.store.login_user(key)
        if not user:
            raise HTTPException(401, "员工登录已失效")
        return user, key

    app.include_router(bridge.routes(authenticate))

    @app.post("/sessions", status_code=201)
    def create_session(request: Request, auth=Depends(authenticate)):
        session, stamp = uid("ses"), now()
        with request.app.state.store.open() as db:
            db.execute(
                "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session, auth[0]["id"], "avatar2", stamp, stamp),
            )
        return {"sessionId": session}

    @app.post("/sessions/{session}/leave-drafts", status_code=201)
    def draft(session: str, body: LeaveInput, auth=Depends(authenticate)):
        user, key = auth
        return bridge.confirmations.propose(Turn(session, user["id"], key), body.model_dump())

    @app.get("/sessions/{session}/leave-drafts/{operation}")
    def status(session: str, operation: str, auth=Depends(authenticate)):
        from avatar_service.leave_confirmation import view

        with bridge.store.open() as db:
            return view(bridge.confirmations.owned(db, auth[0]["id"], session, operation))

    @app.post("/mcp")
    async def mcp(request: Request, authorization: str = Header(default="")):
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "Invalid Request"}}, status_code=400)
        method = payload.get("method")
        _, _, token = authorization.partition(" ")
        discovery = os.environ.get("AVATAR2_DISCOVERY_TOKEN", "")
        discovery_allowed = bool(discovery and compare_digest(token.encode(), discovery.encode()))
        auth = None if discovery_allowed else authenticate(request, authorization)
        if "id" not in payload:
            return Response(status_code=202)
        envelope = {"jsonrpc": "2.0", "id": payload["id"]}
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return {**envelope, "error": {"code": -32602, "message": "Invalid params"}}

        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "avatar2", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            # Protocol-shaped definitions only; trace metadata is served by the
            # internal catalog endpoint.
            result = {"tools": CATALOG.protocol_tools()}
        elif method in ("resources/list", "resources/templates/list", "prompts/list"):
            result = {{"resources/list": "resources", "resources/templates/list": "resourceTemplates",
                       "prompts/list": "prompts"}[method]: []}
        elif method == "tools/call":
            if auth is None:
                raise HTTPException(403, "工具执行需要个人凭据")
            name, arguments = params.get("name"), params.get("arguments", {})
            ctx = ActionContext(
                bridge=bridge,
                store=request.app.state.store,
                user_id=auth[0]["id"],
                login_key=auth[1],
                chat_id=None,
                employee=auth[0],
            )
            try:
                data = await execute_action(ctx, name, arguments)
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            except ActionError as exc:
                payload_error = {"errorCode": exc.code, "message": exc.message}
                result = {
                    "isError": True,
                    "structuredContent": payload_error,
                    "content": [{"type": "text", "text": json.dumps(payload_error, ensure_ascii=False)}],
                }
        else:
            return {**envelope, "error": {"code": -32601, "message": "Method not found"}}
        return {**envelope, "result": result}

    from cards import install_cards
    install_cards(app, bridge, authenticate)
    install_actions(app, bridge, CATALOG, execute_action, CATALOG.protocol_tools())

    @app.post("/internal/hr/web-ticket")
    def web_ticket(
        response: Response, body: dict, authorization: str = Header(default="")
    ):
        response.headers["cache-control"] = "no-store"
        if not _service_allowed(authorization):
            raise HTTPException(401, "服务凭据无效")
        user_id = body.get("user_id")
        target = body.get("target")
        if not isinstance(user_id, str) or len(user_id) > 64:
            raise HTTPException(400, "用户标识无效")
        if target not in ("me", "approvals"):
            raise HTTPException(400, "目标页面无效")
        bindings = request_bindings = app.state.bindings
        if bindings is None:
            raise HTTPException(503, "员工绑定未配置")
        bindings.resolve(user_id)
        version = bindings.current_version(user_id)
        ticket = secrets.token_urlsafe(32)
        expires = int(time.time() * 1000) + 60_000
        with app.state.store.open() as db:
            db.execute(
                "INSERT INTO web_tickets(ticket_hash,user_id,target,expires_at,binding_version) "
                "VALUES (?,?,?,?,?)",
                (
                    hashlib.sha256(ticket.encode()).hexdigest(),
                    user_id,
                    target,
                    expires,
                    version,
                ),
            )
        return {"ticket": ticket, "expiresAt": expires, "target": target}

    @app.post("/internal/hr/ticket/consume")
    def consume_ticket(
        response: Response, body: dict, authorization: str = Header(default="")
    ):
        response.headers["cache-control"] = "no-store"
        if not _service_allowed(authorization):
            raise HTTPException(401, "服务凭据无效")
        ticket = body.get("ticket")
        if not isinstance(ticket, str) or not ticket or len(ticket) > 200:
            raise HTTPException(400, "票据无效")
        digest_value = hashlib.sha256(ticket.encode()).hexdigest()
        stamp = int(time.time() * 1000)
        with app.state.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE web_tickets SET used_at=?
                   WHERE ticket_hash=? AND used_at IS NULL AND expires_at>?""",
                (stamp, digest_value, stamp),
            ).rowcount
            row = db.execute(
                "SELECT user_id,target,binding_version FROM web_tickets WHERE ticket_hash=?",
                (digest_value,)
            ).fetchone()
        if changed != 1 or row is None:
            raise HTTPException(410, "票据已失效或已使用")
        if app.state.bindings is None:
            raise HTTPException(503, "员工绑定未配置")
        employee, _key = app.state.bindings.resolve(row["user_id"])
        app.state.bindings.check_version(row["user_id"], row["binding_version"])
        return {
            "user_id": row["user_id"],
            "handle": employee["handle"],
            "displayName": employee["name"],
            "target": row["target"],
            "bindingVersion": row["binding_version"],
        }

    @app.post("/internal/hr/web-session")
    def web_session(body: dict, authorization: str = Header(default="")):
        """Re-validate the binding behind an already-open personal page."""
        if not _service_allowed(authorization):
            raise HTTPException(401, "服务凭据无效")
        user_id = body.get("user_id")
        version = body.get("version")
        if not isinstance(user_id, str):
            raise HTTPException(400, "用户标识无效")
        if app.state.bindings is None:
            raise HTTPException(503, "员工绑定未配置")
        employee, _key = app.state.bindings.resolve(user_id)
        app.state.bindings.check_version(user_id, version)
        return {
            "user_id": user_id,
            "handle": employee["handle"],
            "displayName": employee["name"],
            "bindingVersion": version,
        }

    @app.get("/internal/catalog")
    def catalog(authorization: str = Header(default="")):
        if not _service_allowed(authorization):
            raise HTTPException(401, "服务凭据无效")
        return {"tools": CATALOG.metadata(), "plugins": CATALOG.describe()}

    return app
