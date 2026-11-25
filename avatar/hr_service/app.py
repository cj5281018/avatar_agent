import asyncio
import logging
import re
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from avatar_contract import schema_error

from . import lifecycle, recruitment, registry
from .admin import routes as admin_routes
from .personal import router as personal_router
from .db import ServiceError, Store, setting
from .leaves import execute
from .outbox import run
from .protocol import VERSION, accept_setup, authenticate, avatar_url, base_url, public_url, text


log = logging.getLogger("uvicorn.error")


def data_dir():
    return Path(os.getenv("HR_DATA_DIR", "data"))


def create_app(directory=None, origin=None, invoke_base=None, secondary=None):
    @asynccontextmanager
    async def lifespan(application):
        avatar_url()
        public_url()
        application.state.store = Store(directory if directory is not None else data_dir())
        if secondary is not None:
            secondary.state.store = Store(data_dir() / "avatar8790")
        for instance in (application, secondary):
            if instance is not None and os.getenv("HR_RECRUITMENT_DEMO") == "1":
                recruitment.seed(instance.state.store)
        workers = [asyncio.create_task(run(application.state.store, origin))]
        if secondary is not None:
            workers.append(asyncio.create_task(run(secondary.state.store, secondary.state.notification_origin)))
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    application = FastAPI(title="Python HR Daily", version="0.1.0", lifespan=lifespan)
    application.state.notification_origin = origin

    def error(status, message):
        return JSONResponse({"ok": False, "error": "service_error" if status >= 500 else "request_error",
                             "message": message}, status_code=status)

    @application.exception_handler(ServiceError)
    async def service_error(_request, exc):
        return error(exc.status, str(exc))

    @application.exception_handler(RequestValidationError)
    async def invalid_body(_request, _exc):
        return error(400, "请求体必须是有效的 JSON 对象")

    @application.get("/health")
    def health(request: Request):
        with request.app.state.store.connection() as db:
            return {"ok": True, "service": "hrdaily", "version": "0.1.0", "paired": bool(setting(db, "service.key"))}

    @application.post("/setup")
    def setup(request: Request, body: dict):
        if body.get("protocolVersion") != VERSION:
            raise ServiceError(f"只支持 {VERSION}")
        return accept_setup(request.app.state.store, body, origin, invoke_base)

    contract = registry.MANIFEST

    @application.get("/manifest")
    def manifest():
        return {"ok": True, "manifest": contract}

    @application.post("/operations/status")
    def status(request: Request, body: dict):
        return invoke_action(request, body, lookup=True)

    def invoke_action(request, body, lookup=False):
        action = body.get("action")
        request_id = body.get("requestId")
        operation = body.get("operationId")
        spec = registry.ACTIONS.get(action) if isinstance(action, str) else None
        write = spec is not None and spec["kind"] == "write"

        def failure(code, message, status_code):
            return JSONResponse({"ok": False, "code": code, "message": message,
                                 "requestId": request_id}, status_code=status_code)

        fingerprint = (body.get("contractVersion"), body.get("manifestSha256"))
        if (body.get("protocolVersion") != VERSION or
                (fingerprint != registry.FINGERPRINT and not registry.allows_previous(action, fingerprint))):
            return failure("CONTRACT_MISMATCH", "HR 清单版本不匹配", 409)
        if spec is None or (lookup and not write):
            return failure("FORBIDDEN", "HR 动作未授权", 403)
        if action in lifecycle.ACTIONS and os.getenv("HR_RECRUITMENT_DEMO") != "1":
            return failure("FORBIDDEN", "Offer 和入职仅在演示环境启用", 403)
        if (not isinstance(request_id, str) or not re.fullmatch(r"srq_[A-Za-z0-9_-]{16}", request_id)
                or write and (not isinstance(operation, str) or not re.fullmatch(r"op_[A-Za-z0-9_-]{16}", operation))):
            return failure("INVALID_INPUT", "无效的请求或操作编号", 400)
        store = request.app.state.store
        try:
            identity = authenticate(store, text(body.get("userKey")), origin, action=action, request_id=request_id,
                                    operation=operation if write else None,
                                    purpose="status" if lookup else "execute", require_grant=True)
            if lookup:
                result = registry.operation_result(store, identity["handle"], action, operation)
                return {"ok": True, "requestId": request_id, "operationId": operation,
                        **({"state": "succeeded", "data": result} if result else {"state": "not_recorded"})}
            params = body.get("input", {})
            if schema_error(spec["inputSchema"], params):
                return failure("INVALID_INPUT", "HR 动作参数无效", 400)
            data = registry.execute(store, identity["handle"], action, params, operation)
        except ServiceError as exc:
            code = {400: "INVALID_INPUT", 401: "FORBIDDEN", 403: "FORBIDDEN",
                    404: "NOT_FOUND", 409: "STATE_CONFLICT"}.get(exc.status, "SERVICE_ERROR")
            return failure(code, str(exc), 403 if exc.status == 401 else exc.status)
        return {"ok": True, "requestId": request_id, "data": data,
                **({"operationId": operation} if write else {})}

    @application.post("/avatar")
    def invoke(request: Request, body: dict):
        if body.get("protocolVersion") != VERSION:
            raise ServiceError(f"只支持 {VERSION}")
        action = text(body.get("action"))
        if action in registry.ACTIONS:
            return invoke_action(request, body)
        if not action:
            raise ServiceError("缺少 action")
        params = body.get("input")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ServiceError("input 必须是 JSON object")
        store = request.app.state.store
        request_id = text(body.get("requestId"))
        identity = authenticate(store, text(body.get("userKey")), origin, action=action, request_id=request_id)
        attempt = text(body.get("attemptId"))
        traced = bool(re.fullmatch(r"hat_[A-Za-z0-9_-]{16}", attempt)
                      and re.fullmatch(r"(?:srq|lop|rop)_[A-Za-z0-9_-]{16}", request_id))
        if traced:
            log.info("hr.accept request=%s attempt=%s", request_id, attempt)
        source_session = text(body.get("notificationSession"))
        result = execute(store, identity, action, params, request_id, source_session)
        response = {"ok": True, "actor": {key: identity[key] for key in ("handle", "displayName")}, "data": result}
        if source_session and action in ("request_leave", "review_leave"):
            response["delivery"] = {"status": "queued", "channel": "durable-outbox"}
        if traced:
            response["attemptId"] = attempt
            log.info("hr.completed request=%s attempt=%s", request_id, attempt)
        if "requestId" in body:
            response["requestId"] = body["requestId"]
        return response

    application.include_router(personal_router)
    application.include_router(admin_routes(origin))
    if secondary is not None:
        application.mount("/avatar8790", secondary)
    return application


secondary_origin = os.getenv("HR_SECONDARY_AVATAR_URL")
secondary_app = create_app(origin=base_url(secondary_origin), invoke_base=public_url() + "/avatar8790") if secondary_origin else None
app = create_app(secondary=secondary_app)

