import os
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StringConstraints

import httpx
from avatar_service.db import digest, now, uid
from avatar_service.stream import Turn
from plugins import ActionContext, ActionError

WorkbenchUser = Annotated[str, StringConstraints(pattern=r"^[0-9A-HJKMNP-TV-Z]{26}$")]
EmployeeToken = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class EmployeeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: WorkbenchUser


class BindRequest(EmployeeRequest):
    token: EmployeeToken


class AssignRequest(EmployeeRequest):
    handle: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class ToolRequest(EmployeeRequest):
    name: str
    arguments: dict
    chat_id: WorkbenchUser


def install_actions(app, bridge, catalog, dispatch, tools):
    def authorize(request: Request, authorization: str = Header(default="")):
        secret = os.environ.get("AVATAR2_INTERNAL_TOKEN", "")
        if not secret or not compare_digest(authorization.encode(), ("Bearer " + secret).encode()):
            raise HTTPException(401, "服务凭据无效")
        if request.app.state.bindings is None:
            raise HTTPException(503, "员工绑定未配置")
        return request.app.state.bindings

    @app.post("/internal/bindings")
    def bind(body: BindRequest, bindings=Depends(authorize)):
        return {"employee_id": bindings.bind(body.user_id, body.token)}

    @app.delete("/internal/bindings/{user_id}", status_code=204)
    def unlink(user_id: WorkbenchUser, bindings=Depends(authorize)):
        bindings.unlink(user_id)

    @app.post("/internal/hr-directory", status_code=204)
    def assign(body: AssignRequest, bindings=Depends(authorize)):
        bindings.assign(body.user_id, body.handle)

    @app.post("/internal/bindings/renew")
    def renew(body: EmployeeRequest, bindings=Depends(authorize)):
        return bindings.renew(body.user_id)

    @app.post("/internal/my-requests")
    async def my_requests(body: EmployeeRequest, bindings=Depends(authorize)):
        _, key = bindings.resolve(body.user_id)
        return (await bridge.invoke("list_my_requests", {}, key, "", uid("srq")))["data"]

    @app.post("/internal/reviews")
    async def pending_reviews(body: EmployeeRequest, bindings=Depends(authorize)):
        _, key = bindings.resolve(body.user_id)
        return (await bridge.invoke("list_pending_approvals", {}, key, "", uid("srq")))["data"]

    @app.post("/internal/reviews/{leave_id}/approve")
    async def approve(leave_id: str, body: EmployeeRequest, bindings=Depends(authorize)):
        employee, token = bindings.credential(body.user_id)
        owner = employee["id"]
        session = "reviews_" + owner
        with bridge.store.open() as db:
            db.execute(
                "INSERT OR IGNORE INTO sessions(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                (session, owner, "请假审批", now(), now()),
            )
        operation = bridge.confirmations.propose(
            Turn(session, owner, digest(token), turn_id=leave_id),
            {"requestId": leave_id, "decision": "approve"}, "review_leave",
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.post(
                f"/v1/sessions/{session}/hr/leave-confirmations/{operation['operationId']}/confirm",
                headers={"Authorization": "Bearer " + token},
            )
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail", "审批失败"))
        item = response.json()
        return {"id": item["leaveId"], **item["reviewResult"]}

    @app.post("/internal/tools/list")
    def list_tools(body: EmployeeRequest, bindings=Depends(authorize)):
        bindings.resolve(body.user_id)
        return {"tools": tools}

    @app.post("/internal/tools/call")
    async def call_tool(body: ToolRequest, bindings=Depends(authorize)):
        employee, key = bindings.resolve(body.user_id)
        ctx = ActionContext(
            bridge=bridge,
            store=bridge.store,
            user_id=body.user_id,
            login_key=key,
            chat_id=body.chat_id,
            employee=employee,
        )
        try:
            return await dispatch(ctx, body.name, body.arguments)
        except ActionError as exc:
            raise HTTPException(exc.status, detail={"errorCode": exc.code, "message": exc.message})

    @app.post("/internal/cards/{session}/{operation}/{action}")
    async def card(session: WorkbenchUser, operation: str, action: str, body: EmployeeRequest,
                   bindings=Depends(authorize)):
        _, token = bindings.credential(body.user_id)
        if not operation.startswith("lop_") or not operation[4:].replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400, "操作编号无效")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            headers = {"Authorization": "Bearer " + token}
            if action == "confirm":
                response = await client.post(
                    f"/v1/sessions/{session}/hr/leave-confirmations/{operation}/confirm", headers=headers)
            elif action == "status":
                response = await client.get(f"/sessions/{session}/leave-drafts/{operation}", headers=headers)
            else:
                raise HTTPException(404, "操作不存在")
        if response.is_error:
            raise HTTPException(response.status_code, response.json().get("detail", "操作失败"))
        item = response.json()
        if action == "status" and item.get("leaveId"):
            history = await bridge.invoke("list_my_requests", {}, digest(token), "", uid("srq"))
            entry = next((record for record in history["data"]["requests"]
                          if record["id"] == item["leaveId"]), None)
            if entry is None:
                raise HTTPException(502, "HR 暂未查到此申请")
            item["hrStatus"] = entry["status"]
        return item

