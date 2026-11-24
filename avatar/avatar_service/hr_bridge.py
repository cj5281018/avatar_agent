import asyncio
import logging
import re
import os
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException

from .db import digest, uid, user_view
from .leave_confirmation import LeaveConfirmations

log = logging.getLogger("uvicorn.error")

VERSION = "avatar-service/v1"
SLUG = "hr-leave-python"
ACTIONS = {
    "describe", "who_am_i", "get_approvers", "request_leave", "list_my_requests",
    "list_pending_approvals", "review_leave", "cancel_leave", "list_all_requests", "employee_directory",
    "resend_notification",
}


class HrBridge:
    def __init__(self):
        self.origin = os.environ.get("PY_AVATAR_PUBLIC_URL", "http://127.0.0.1:8790").rstrip("/")
        self.hr_base = os.environ.get("PY_AVATAR_HR_URL", "http://127.0.0.1:8891/avatar8790").rstrip("/")
        self.turns = {}
        self.calls = {}
        self.pair_lock = asyncio.Lock()

    def start(self, store, publish):
        self.store = store
        self.publish = publish
        self.confirmations = LeaveConfirmations(store)
        with store.open() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS hr_pairing (
                id TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE, ready INTEGER NOT NULL
            )""")

    def registration(self, key):
        with self.store.open() as db:
            return db.execute("SELECT * FROM hr_pairing WHERE key_hash=?", (digest(key),)).fetchone()

    @staticmethod
    def service(row):
        return {"id": row["id"], "slug": SLUG, "name": "Python HR Daily",
                "setupStatus": "ready" if row["ready"] else "pending"}

    async def request(self, path, body):
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                response = await client.post(self.hr_base + path, json=body)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "Python HR 服务未响应或返回无效数据") from None
        if not isinstance(data, dict) or not response.is_success or data.get("ok") is not True:
            status = response.status_code if 400 <= response.status_code < 500 else 502
            message = data.get("message") if isinstance(data, dict) else None
            if isinstance(message, str):
                for name in ("userKey", "setupCode"):
                    if body.get(name):
                        message = message.replace(body[name], "[已隐藏]")
                if body.get("credentials", {}).get("serviceKey"):
                    message = message.replace(body["credentials"]["serviceKey"], "[已隐藏]")
            raise HTTPException(status, message or "Python HR 拒绝了调用")
        return data

    async def pair(self, code):
        async with self.pair_lock:
            key, service_id = uid("svc_key"), uid("svc")
            with self.store.open() as db:
                db.execute("DELETE FROM hr_pairing WHERE ready=0")
                db.execute("INSERT INTO hr_pairing VALUES (?,?,0)", (service_id, digest(key)))
            try:
                data = await self.request("/setup", {
                    "protocolVersion": VERSION, "setupCode": code,
                    "service": {"id": service_id, "slug": SLUG, "name": "Python HR Daily"},
                    "avatar": {"baseUrl": self.origin, "identityCheckUrl": self.origin + "/identitycheck",
                               "serviceCheckUrl": self.origin + "/servicecheck"},
                    "credentials": {"serviceKey": key},
                })
                if data.get("invoke") != {"transport": "http", "target": self.hr_base + "/avatar"}:
                    raise HTTPException(502, "HR 返回的调用地址与配置不一致")
                with self.store.open() as db:
                    db.execute("DELETE FROM hr_pairing WHERE id!=?", (service_id,))
                    db.execute("UPDATE hr_pairing SET ready=1 WHERE id=?", (service_id,))
            except BaseException:
                with self.store.open() as db:
                    db.execute("DELETE FROM hr_pairing WHERE id=? AND ready=0", (service_id,))
                raise
            return {"ok": True, "service": {"id": service_id, "slug": SLUG}}

    async def invoke(self, action, args, key, session, request_id, attempt=None):
        with self.store.open() as db:
            registration = db.execute("SELECT id FROM hr_pairing WHERE ready=1").fetchone()
        if not registration:
            raise HTTPException(503, "Python HR 尚未配对")
        user_key = secrets.token_urlsafe(32)
        self.calls[user_key] = (registration["id"], key, action, request_id)
        attempt = attempt or uid("hat")
        start = time.monotonic()
        log.info("hr.dispatch request=%s attempt=%s action=%s", request_id, attempt, action)
        try:
            data = await self.request("/avatar", {"protocolVersion": VERSION, "requestId": request_id,
                                                  "notificationSession": session if self.receiver else "",
                                                  "action": action, "input": args, "userKey": user_key, "attemptId": attempt})
            verified = data.get("attemptId") == attempt and data.get("requestId") == request_id
            log.info("hr.result request=%s attempt=%s outcome=%s elapsed_ms=%d", request_id, attempt,
                     "verified" if verified else "unverified", int((time.monotonic() - start) * 1000))
            return data
        except HTTPException as error:
            log.info("hr.result request=%s attempt=%s status=%d elapsed_ms=%d", request_id, attempt,
                     error.status_code, int((time.monotonic() - start) * 1000))
            raise
        finally:
            self.calls.pop(user_key, None)

    def publish_confirmation(self, owner, session, item):
        self.publish(owner, {"type": "hr.leave_confirmation", "sessionId": session, "confirmation": item})

    def routes(self, current):
        router = APIRouter()

        @router.post("/v1/admin/hr/setup")
        async def setup(body: dict, auth=Depends(current)):
            if auth[0]["role"] != "admin":
                raise HTTPException(403, "仅管理员可配对 HR")
            code = body.get("setupCode")
            if not isinstance(code, str) or not code.strip():
                raise HTTPException(400, "缺少配对码")
            return await self.pair(code.strip())

        @router.post("/servicecheck")
        async def service_check(body: dict):
            row = self.registration(str(body.get("serviceKey", "")))
            if not row:
                raise HTTPException(401, "服务身份校验失败")
            return {"ok": True, "service": self.service(row)}

        @router.post("/identitycheck")
        async def identity_check(body: dict):
            service = self.registration(str(body.get("serviceKey", "")))
            call = self.calls.get(str(body.get("userKey", "")))
            if service and service["ready"] and not call and hasattr(self, "broker"):
                return self.broker.hr_identity(service, body.get("userKey"))
            if not service or not service["ready"] or not call or call[0] != service["id"]:
                raise HTTPException(401, "服务或用户身份校验失败")
            user = self.store.login_user(call[1])
            if not user:
                raise HTTPException(401, "登录已失效")
            return {"ok": True, "service": self.service(service), "user": user_view(user),
                    "grant": {"action": call[2], "requestId": call[3]}}

        @router.post("/internal/hr/deliveries")
        async def deliver(body: dict):
            registration = self.registration(str(body.get("serviceKey", "")))
            if not registration or not registration["ready"]:
                raise HTTPException(401, "HR 服务身份校验失败")
            if not self.receiver:
                raise HTTPException(503, "Receiver 未启用")
            return await self.receiver.accept(body.get("event"))

        @router.post("/v1/sessions/{session}/hr/leave-confirmations/{operation}/confirm")
        async def confirm_leave(session: str, operation: str, auth=Depends(current)):
            owner, key = auth[0]["id"], auth[1]
            item = self.confirmations.confirm(owner, session, operation)
            if item["status"] == "submitted":
                return item
            self.publish_confirmation(owner, session, item)
            action = item["action"]
            result = await self.invoke(action, item["input"], key, session, operation)
            payload = result.get("data", {})
            request = payload.get("request", {})
            leave_id = request.get("id")
            if not isinstance(leave_id, str) or not leave_id:
                raise HTTPException(502, "HR 未返回正式单号，请用同一确认卡片重试")
            review_result = None
            if action == "review_leave":
                status, applied = request.get("status"), payload.get("decisionApplied")
                if (leave_id != item["input"]["requestId"]
                        or status not in ("pending", "approved", "rejected", "cancelled")
                        or not isinstance(applied, bool)):
                    raise HTTPException(502, "HR 未返回有效审批结果，请用同一确认卡片重试")
                review_result = {"status": status, "applied": applied}
            item = self.confirmations.complete(owner, session, operation, leave_id, review_result)
            self.publish_confirmation(owner, session, item)
            return item

        @router.post("/internal/hr/tool")
        async def call_tool(body: dict):
            capability = body.get("capability")
            turn = self.turns.get(capability) if isinstance(capability, str) else None
            key = turn.login_key if turn and not turn.stop.is_set() else None
            if not key or not self.store.login_user(key):
                raise HTTPException(401, "当前聊天回合或登录已失效")
            action, args = body.get("action"), body.get("input", {})
            mcp_id = body.get("mcpTraceId")
            if not isinstance(mcp_id, str) or not re.fullmatch(r"mcp_[a-f0-9]{32}", mcp_id):
                mcp_id = None
            if body.get("service") != SLUG or not isinstance(action, str) or action not in ACTIONS or not isinstance(args, dict):
                raise HTTPException(400, "只支持已注册的 HR 动作")
            title = f"MCP → Avatar · {action}"
            details = f"回合 {turn.turn_id}\nMCP 调用 ID {mcp_id}" if mcp_id else f"回合 {turn.turn_id}"
            if mcp_id:
                turn.trace(mcp_id, title, details, "in_progress")
                log.info("mcp.accept turn=%s call=%s action=%s", turn.turn_id, mcp_id, action)
            try:
                return await self.execute_tool(turn, key, action, args, mcp_id, title, details)
            except HTTPException as error:
                if mcp_id and turn.activity[mcp_id]["status"] == "in_progress":
                    turn.trace(mcp_id, title, details + f"\nHTTP {error.status_code}", "failed")
                raise

        return router

    async def execute_tool(self, turn, key, action, args, mcp_id, title, details):
        if action in ("request_leave", "review_leave"):
            with self.store.open() as db:
                if not db.execute("SELECT 1 FROM hr_pairing WHERE ready=1").fetchone():
                    raise HTTPException(503, "Python HR 尚未配对")
            item = self.confirmations.propose(turn, args, action)
            self.publish_confirmation(turn.owner, turn.session_id, item)
            if mcp_id:
                turn.trace(mcp_id, title, details + f"\n仅准备确认卡片 · 操作 ID {item['operationId']}")
            return {"ok": True, "data": {"status": item["status"], "leaveId": item["leaveId"],
                                          "message": "请在桌面端确认卡片中核对并确认，工具调用本身只准备操作。"}}
        request_id = uid("srq")
        attempt = uid("hat")
        if mcp_id:
            details += f"\n请求 ID {request_id}\n尝试 ID {attempt}"
        try:
            data = await self.invoke(action, args, key, turn.session_id, request_id, attempt)
        except HTTPException as error:
            if mcp_id:
                turn.trace(mcp_id, title, details + f"\n调用失败 · HTTP {error.status_code}", "failed")
            raise
        if mcp_id:
            verified = data.get("requestId") == request_id and data.get("attemptId") == attempt
            evidence = "HR 已响应" if verified else "HR 响应缺少完整追踪标识"
            turn.trace(mcp_id, title, details + f"\n{evidence}")
        if self.receiver and "delivery" not in data:
            await self.receiver.forward(turn, action, data)
        return data

