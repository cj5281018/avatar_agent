import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from actions import install_actions
from bindings import EmployeeBindings
from avatar_service.db import Store, digest, now, uid
from avatar_service.hr_bridge import HrBridge
from avatar_service.leave_confirmation import LEAVE_TYPES, validate_leave
from avatar_service.stream import Turn


class LeaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    startDate: str
    endDate: str
    leaveType: str
    reason: str


leave_schema = LeaveInput.model_json_schema()
leave_schema["properties"]["leaveType"]["enum"] = sorted(LEAVE_TYPES)
leave_schema["properties"]["leaveType"]["description"] = "Use annual for 年假."
TOOLS = [
    {"name": "hr_list_my_requests", "description": "查询当前员工的请假申请",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "hr_preview_leave", "description": "预览请假日期、假别和事由，返回待员工核对的内容",
     "inputSchema": leave_schema},
]


def create_app(directory=None):
    bridge = HrBridge()
    bridge.receiver = None

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(directory or Path(os.environ["AVATAR2_DATA_DIR"]))
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
            result = {"tools": [{**tool, "annotations": {"readOnlyHint": True}} for tool in TOOLS]}
        elif method in ("resources/list", "resources/templates/list", "prompts/list"):
            result = {{"resources/list": "resources", "resources/templates/list": "resourceTemplates",
                       "prompts/list": "prompts"}[method]: []}
        elif method == "tools/call":
            if auth is None:
                raise HTTPException(403, "工具执行需要个人凭据")
            name, arguments = params.get("name"), params.get("arguments", {})
            try:
                if name == "hr_preview_leave" and isinstance(arguments, dict):
                    data = {"type": "leave_preview", "input": validate_leave(arguments)}
                elif name == "hr_list_my_requests" and arguments == {}:
                    data = (await bridge.invoke("list_my_requests", {}, auth[1], "", uid("srq")))["data"]
                else:
                    raise HTTPException(400, "未知工具或参数不正确")
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            except HTTPException as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc.detail)}]}
        else:
            return {**envelope, "error": {"code": -32601, "message": "Method not found"}}
        return {**envelope, "result": result}

    from cards import install_cards
    install_cards(app, bridge, authenticate)
    install_actions(app, bridge, TOOLS)
    return app




