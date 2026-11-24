import hashlib
import hmac
import json
import re
import secrets
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from .db import ServiceError, identifier, now, set_setting, setting
from .leaves import employee, employee_view, leave_view, required_leave, submit
from .protocol import post_avatar, text

COOKIE = "hrdaily_admin"
TTL = 8 * 60 * 60 * 1000
STATES = ("pending", "approved", "rejected", "cancelled")
PUBLIC = Path(__file__).with_name("public")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_digest(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def set_password(store, password):
    if not 12 <= len(password) <= 512:
        raise ServiceError("密码须为 12–512 个字符")
    salt = secrets.token_hex(16)
    with store.connection(write=True) as db:
        set_setting(db, "admin.password", salt + ":" + password_digest(password, salt))
        db.execute("DELETE FROM settings WHERE key LIKE 'admin.session.%'")


def handle_of(value):
    handle = text(value).lower().lstrip("@")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", handle):
        raise ServiceError("人员账号格式无效")
    return handle


def save_employee(store, body, existing=None):
    handle = handle_of(existing if existing is not None else body.get("handle"))
    name, job, rank = (text(body.get(field)) for field in ("displayName", "jobTitle", "rank"))
    active, assigned = body.get("active", True), body.get("approvers", [])
    if not name or not job or max(len(name), len(job), len(rank)) > 100:
        raise ServiceError("请填写姓名和岗位，每个字段最多 100 字")
    if not isinstance(active, bool) or not isinstance(assigned, list) or len(assigned) > 100:
        raise ServiceError("人员状态或审批人格式无效")
    assigned = list(dict.fromkeys(handle_of(item) for item in assigned))
    with store.connection(write=True) as db:
        current = db.execute("SELECT * FROM employees WHERE handle=?", (handle,)).fetchone()
        if existing is None and current:
            raise ServiceError("人员已存在", 409)
        if existing is not None and not current:
            raise ServiceError("人员不存在", 404)
        for approver in assigned:
            if approver == handle:
                raise ServiceError("不能将自己设为审批人")
            employee(db, approver)
        db.execute("""INSERT INTO employees(handle,display_name,job_title,rank,approvers_json,active)
                      VALUES (?,?,?,?,?,?) ON CONFLICT(handle) DO UPDATE SET
                      display_name=excluded.display_name, job_title=excluded.job_title,
                      rank=excluded.rank, approvers_json=excluded.approvers_json, active=excluded.active""",
                   (handle, name, job, rank or None, json.dumps(assigned), int(active)))
        row = db.execute("SELECT * FROM employees WHERE handle=?", (handle,)).fetchone()
        return {"employee": {**employee_view(row), "active": active, "approvers": assigned}}


def integration(store, origin):
    with store.connection() as db:
        key = setting(db, "service.key")
        service_id = setting(db, "service.id")
        name = setting(db, "service.name")
    if not key:
        return {"connectionStatus": "unpaired"}
    try:
        result = post_avatar("/servicecheck", {"serviceKey": store.open(key)}, origin)
        service = result.get("service") or {}
        status = ("invalid" if not result.get("ok") or service.get("id") != service_id else
                  "connected" if service.get("setupStatus") == "ready" else "pending")
    except ServiceError as error:
        status = "invalid" if error.status == 401 else "unreachable"
    return {"connectionStatus": status, "checkedAt": now(), "service": {"name": name}}


def dashboard(store, origin):
    today = date.today().isoformat()
    with store.connection() as db:
        employees = {row["handle"]: row for row in db.execute("SELECT * FROM employees ORDER BY active DESC,handle")}
        leaves = [leave_view(db, store, row) for row in db.execute("SELECT * FROM leave_requests ORDER BY created_at DESC")]
        pending = dict(db.execute("""SELECT a.approver_handle,COUNT(*) FROM leave_approvals a
            JOIN leave_requests r ON r.id=a.request_id WHERE a.status='pending' AND r.status='pending'
            GROUP BY a.approver_handle"""))
    counts = dict.fromkeys(STATES, 0)
    on_leave = set()
    for leave in leaves:
        counts[leave["status"]] += 1
        leave["manuallyDecided"] = leave.get("decidedBy", {}).get("handle") == "hrdaily-admin"
        if leave["status"] == "approved" and leave["startDate"] <= today <= leave["endDate"]:
            on_leave.add(leave["requester"]["handle"])
    people = [
        {**employee_view(row), "active": bool(row["active"]),
         "workStatus": "inactive" if not row["active"] else "on_leave" if handle in on_leave else "working",
         "approvers": [employee_view(employees.get(a)) or {"handle": a, "displayName": a, "missing": True}
                       for a in json.loads(row["approvers_json"])],
         "pendingApprovals": pending.get(handle, 0)}
        for handle, row in employees.items()
    ]
    return {"employees": people, "leaves": leaves, "counts": {**counts, "total": len(leaves)},
            "today": today, "updatedAt": now(), "integration": integration(store, origin)}


def routes(origin=None):
    router = APIRouter()

    def store_for(request):
        return request.app.state.store

    def session(request, write=False):
        store = store_for(request)
        key = "admin.session." + digest(request.cookies.get(COOKIE, ""))
        with store.connection() as db:
            raw = setting(db, key)
        if not raw:
            raise ServiceError("请先登录 HR 管理后台", 401)
        record = json.loads(raw)
        if record["expiresAt"] <= now():
            raise ServiceError("HR 登录已过期", 401)
        if write and not hmac.compare_digest(digest(request.headers.get("x-hrdaily-csrf", "")), digest(record["csrf"])):
            raise ServiceError("CSRF 校验失败", 403)
        return key, {"ok": True, "username": "admin", **record}

    @router.post("/api/admin/login")
    def login(request: Request, response: Response, body: dict):
        store = store_for(request)
        password = body.get("password")
        if not isinstance(password, str) or len(password) > 512:
            raise ServiceError("用户名或密码不正确", 401)
        with store.connection() as db:
            credential = setting(db, "admin.password")
        if not credential:
            raise ServiceError("请先在 HR 服务本机设置管理密码", 503)
        salt, hashed = credential.split(":")
        if not hmac.compare_digest(password_digest(password, salt), hashed) or body.get("username") != "admin":
            raise ServiceError("用户名或密码不正确", 401)
        token = secrets.token_urlsafe(32)
        record = {"csrf": secrets.token_urlsafe(24), "expiresAt": now() + TTL}
        with store.connection(write=True) as db:
            if setting(db, "admin.password") != credential:
                raise ServiceError("凭据已更新，请重新登录", 401)
            db.execute("""DELETE FROM settings WHERE key LIKE 'admin.session.%'
                          AND json_extract(value,'$.expiresAt')<=?""", (now(),))
            set_setting(db, "admin.session." + digest(token), json.dumps(record))
        response.set_cookie(COOKIE, token, max_age=TTL // 1000, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path=request.scope.get("root_path") or "/")
        return {"ok": True, "username": "admin", **record}

    @router.get("/api/admin/session")
    def get_session(request: Request):
        return session(request)[1]

    @router.post("/api/admin/logout")
    def logout(request: Request, response: Response):
        key, _ = session(request, write=True)
        with store_for(request).connection(write=True) as db:
            db.execute("DELETE FROM settings WHERE key=?", (key,))
        response.delete_cookie(COOKIE, path=request.scope.get("root_path") or "/")
        return {"ok": True}

    @router.get("/api/dashboard")
    @router.get("/api/admin/dashboard")
    def read_dashboard(request: Request):
        session(request)
        return dashboard(store_for(request), origin)

    @router.post("/api/admin/employees")
    def create_employee(request: Request, body: dict):
        session(request, write=True)
        return {"ok": True, **save_employee(store_for(request), body)}

    @router.put("/api/admin/employees/{handle}")
    def update_employee(request: Request, handle: str, body: dict):
        session(request, write=True)
        return {"ok": True, **save_employee(store_for(request), body, handle)}

    @router.post("/api/admin/leaves")
    def create_leave(request: Request, body: dict):
        session(request, write=True)
        store = store_for(request)
        with store.connection(write=True) as db:
            row = employee(db, handle_of(body.get("requesterHandle")))
            result = submit(db, store, {"handle": row["handle"], "displayName": row["display_name"]},
                            body, identifier("manual"))
        return {"ok": True, "request": result["request"]}

    @router.patch("/api/admin/leaves/{leave_id}")
    def set_status(request: Request, leave_id: str, body: dict):
        session(request, write=True)
        status = body.get("status")
        if status not in STATES:
            raise ServiceError("请假状态无效")
        store = store_for(request)
        with store.connection(write=True) as db:
            required_leave(db, leave_id)
            actor = (None, None)
            if status == "pending":
                db.execute("""UPDATE leave_approvals SET status='pending', encrypted_note=NULL,
                              decided_at=NULL WHERE request_id=?""", (leave_id,))
            else:
                actor = ("hrdaily-admin", "HR 管理员")
            db.execute("""UPDATE leave_requests SET status=?,decided_by_handle=?,decided_by_name=?,updated_at=?
                          WHERE id=?""", (status, *actor, now(), leave_id))
            result = leave_view(db, store, required_leave(db, leave_id))
        return {"ok": True, "request": result}

    @router.delete("/api/admin/leaves/{leave_id}")
    def delete_leave(request: Request, leave_id: str):
        session(request, write=True)
        store = store_for(request)
        with store.connection(write=True) as db:
            db.execute("DELETE FROM leave_requests WHERE id=?", (leave_id,))
        return {"ok": True, **dashboard(store, origin)}

    @router.get("/")
    def index():
        return FileResponse(PUBLIC / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'self'",
            "X-Content-Type-Options": "nosniff",
        })

    def serve_asset(file):
        def asset():
            return FileResponse(file)
        return asset

    for path in ("app.css", "app.js", "assets/hr-leave-banner.png"):
        router.add_api_route("/" + path, serve_asset(PUBLIC / path), methods=["GET"], include_in_schema=False)

    return router
