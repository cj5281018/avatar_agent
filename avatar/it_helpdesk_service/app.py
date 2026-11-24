import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from avatar_contract import VERSION, base_url, manifest_digest, schema_error
from .store import IdempotencyConflict, Store, service_key, setting, set_setting

from .actions import ACTIONS, CONTRACT_VERSION, SLUG, manifest


log = logging.getLogger("uvicorn.error")


def error(code, message, status, request_id=None):
    return JSONResponse({"ok": False, "code": code, "message": message, "requestId": request_id}, status)


def matches(left, right):
    return isinstance(left, str) and hmac.compare_digest(left.encode(), right.encode())


def create_app(directory=None, actions=None):
    @asynccontextmanager
    async def lifespan(application):
        application.state.avatar = base_url(os.environ.get("IT_AVATAR_BASE_URL", "http://127.0.0.1:18790"))
        application.state.origin = base_url(os.environ.get("IT_PUBLIC_URL", "http://127.0.0.1:18892"))
        application.state.store = Store(directory or os.environ.get("IT_DATA_DIR", "data/it"))
        yield

    app = FastAPI(title="IT Helpdesk", lifespan=lifespan)
    actions = ACTIONS if actions is None else actions
    contract = manifest(actions)
    digest = manifest_digest(contract)

    def callback(avatar, path, payload):
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            response = client.post(avatar + path, json=payload)
            response.raise_for_status()
            return response.json()

    @app.get("/health")
    def health():
        store = app.state.store
        with store.connection() as db:
            return {"ok": True, "paired": bool(setting(db, "it.key"))}

    @app.post("/setup")
    def setup(body: dict):
        store, avatar, origin = app.state.store, app.state.avatar, app.state.origin
        identity = body.get("service")
        credentials = body.get("credentials")
        key = credentials.get("serviceKey") if isinstance(credentials, dict) else None
        trusted = {"baseUrl": avatar, "serviceCheckUrl": avatar + "/internal/services/servicecheck",
                   "identityCheckUrl": avatar + "/internal/services/identitycheck"}
        if (body.get("protocolVersion") != VERSION or body.get("avatar") != trusted
                or not isinstance(identity, dict) or identity.get("slug") != SLUG
                or identity.get("id") != "svc_" + SLUG or not 32 <= len(service_key()) <= 200
                or not matches(key, service_key())):
            return error("FORBIDDEN", "配对身份或服务地址不匹配", 403)
        result = {"ok": True, "protocolVersion": VERSION, "invoke": {"transport": "http", "target": origin + "/avatar"}}
        with store.connection() as db:
            code = setting(db, "it.code")
            previous = setting(db, "it.key")
            paired_origin = setting(db, "it.avatar")
        if previous == store.seal(key) and paired_origin == avatar and not code:
            return result
        supplied = body.get("setupCode")
        if not code or not isinstance(supplied, str) or not matches(store.seal(supplied), code):
            return error("FORBIDDEN", "配对码无效", 403)
        try:
            verified = callback(avatar, "/internal/services/servicecheck", {"serviceKey": key})
            if verified.get("ok") is not True or verified.get("service", {}).get("id") != identity["id"]:
                return error("FORBIDDEN", "Avatar 未确认服务身份", 403)
        except (httpx.HTTPError, ValueError, AttributeError):
            return error("FORBIDDEN", "Avatar 未确认服务身份", 403)
        with store.connection(write=True) as db:
            if setting(db, "it.code") != code:
                return error("FORBIDDEN", "配对码已消费", 403)
            set_setting(db, "it.key", store.seal(key))
            set_setting(db, "it.avatar", avatar)
            db.execute("DELETE FROM settings WHERE key='it.code'")
        return result

    @app.get("/manifest")
    def get_manifest(authorization: str = Header(default="")):
        store, avatar = app.state.store, app.state.avatar
        with store.connection() as db:
            key = setting(db, "it.key")
            if not key or key != store.seal(service_key()) or setting(db, "it.avatar") != avatar or not matches(authorization, "Bearer " + service_key()):
                return error("FORBIDDEN", "清单访问未授权", 403)
        return {"ok": True, "manifest": contract}

    @app.post("/avatar")
    @app.post("/operations/status")
    def invoke(request: Request, body: dict):
        lookup = request.url.path.endswith("/operations/status")
        store, avatar = app.state.store, app.state.avatar
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not re.fullmatch(r"srq_[A-Za-z0-9_-]{16}", request_id):
            return error("INVALID_INPUT", "请求编号无效", 400)
        with store.connection() as db:
            key = setting(db, "it.key")
            paired_origin = setting(db, "it.avatar")
        user_key = body.get("userKey")
        if not key or key != store.seal(service_key()) or paired_origin != avatar or not isinstance(user_key, str) or not user_key:
            return error("FORBIDDEN", "身份回查失败", 403, request_id)
        try:
            identity = callback(avatar, "/internal/services/identitycheck",
                                {"serviceKey": service_key(), "userKey": user_key})
            user = identity.get("user", {})
            if identity.get("ok") is not True or identity.get("service", {}).get("slug") != SLUG:
                return error("FORBIDDEN", "身份回查失败", 403, request_id)
        except (httpx.HTTPError, ValueError, AttributeError):
            return error("FORBIDDEN", "身份回查失败", 403, request_id)
        if (body.get("protocolVersion") != VERSION or body.get("contractVersion") != CONTRACT_VERSION
                or body.get("manifestSha256") != digest):
            return error("CONTRACT_MISMATCH", "工单服务契约需要重新核验", 409, request_id)
        name = body.get("action")
        action = actions.get(name) if isinstance(name, str) else None
        if not action or not isinstance(user, dict) or user.get("role") not in action.roles:
            return error("FORBIDDEN", "当前用户不能执行该动作", 403, request_id)
        operation = body.get("operationId")
        grant = {"action": name, "requestId": request_id}
        if action.kind == "write":
            if not isinstance(operation, str) or not re.fullmatch(r"op_[A-Za-z0-9_-]{16}", operation):
                return error("FORBIDDEN", "缺少确认操作授权", 403, request_id)
            grant.update(operationId=operation, purpose="status" if lookup else "execute")
        handle = user.get("handle")
        if identity.get("grant") != grant or not isinstance(handle, str) or not handle or (lookup and action.kind != "write"):
            return error("FORBIDDEN", "动作授权不匹配", 403, request_id)
        if action.kind == "write" and handle not in {"marco", "bob", "admin"}:
            return error("FORBIDDEN", "当前员工未开通 IT 报修", 403, request_id)
        scope = paired_origin + "\n" + identity["service"]["id"]
        if lookup:
            with store.connection() as db:
                row = store.operation(db, scope, handle, name, operation)
            log.info("it.status request=%s operation=%s found=%s", request_id, operation, bool(row))
            return {"ok": True, "requestId": request_id, "operationId": operation,
                    **({"state": "succeeded", "data": json.loads(row[1])} if row else {"state": "not_recorded"})}
        params = body.get("input", {})
        try:
            if schema_error(contract["actions"][name]["inputSchema"], params):
                return error("INVALID_INPUT", "工单查询参数不符合契约", 400, request_id)
            data = action.input.model_validate(params)
        except ValidationError:
            return error("INVALID_INPUT", "工单查询参数不符合契约", 400, request_id)
        if action.kind == "write":
            try:
                payload = action.handler(store, scope, handle, name, operation, data)
            except IdempotencyConflict:
                return error("IDEMPOTENCY_CONFLICT", "同一操作的参数不一致", 409, request_id)
            if schema_error(contract["actions"][name]["outputSchema"], payload):
                return error("OUTPUT_SCHEMA_MISMATCH", "工单服务响应无效", 502, request_id)
            log.info("it.write request=%s operation=%s", request_id, operation)
            return {"ok": True, "requestId": request_id, "operationId": operation, "data": payload}
        result = action.handler(handle, data, store)
        if result is None:
            log.info("it.result request=%s action=%s code=NOT_FOUND", request_id, name)
            return error("NOT_FOUND", "未找到该工单", 404, request_id)
        payload = result.model_dump(mode="json")
        if schema_error(contract["actions"][name]["outputSchema"], payload):
            return error("OUTPUT_SCHEMA_MISMATCH", "工单服务响应无效", 502, request_id)
        log.info("it.result request=%s action=%s code=OK", request_id, name)
        return {"ok": True, "requestId": request_id, "data": payload}

    return app


app = create_app()
