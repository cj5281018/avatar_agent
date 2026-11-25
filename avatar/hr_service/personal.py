"""Employee and supervisor personal pages for the HR service.

These pages are a separate surface from the admin console: they are reached
through a single-use ticket issued by Avatar, hold a personal cookie session,
and act strictly as the signed-in employee. The admin cookie is ignored here.

Every page and every write re-validates the Avatar binding behind the session,
so unlinking or letting the binding expire blocks an already-open page.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .db import ServiceError, setting, set_setting
from .leaves import employee, execute, leave_view

COOKIE = "hr_personal"
TTL_MS = 2 * 60 * 60 * 1000
CSRF_FIELD = "csrf"

router = APIRouter()


def _now() -> int:
    return int(time.time() * 1000)


def _gateway(path: str, payload: dict) -> dict:
    import os

    base = os.environ.get("HR_AVATAR_BASE_URL", "").rstrip("/")
    token = os.environ.get("AVATAR2_INTERNAL_TOKEN", "")
    if not base or not token:
        raise ServiceError("HR 个人页未配置服务凭据", 503)
    try:
        response = httpx.post(
            base + path,
            json=payload,
            headers={"Authorization": "Bearer " + token},
            timeout=15,
            trust_env=False,
        )
    except httpx.HTTPError as exc:
        raise ServiceError("Avatar 暂时不可用", 503) from exc
    if response.status_code == 410:
        raise ServiceError("票据已失效或已使用", 410)
    if response.status_code == 403:
        raise ServiceError("员工绑定不可用，请回到工作台续期", 403)
    if response.status_code == 409:
        raise ServiceError("绑定已变更，请从 Avatar 重新进入", 409)
    if not response.is_success:
        raise ServiceError("Avatar 拒绝了请求", 502)
    return response.json()


def _session_key(token: str) -> str:
    return "personal.session." + hashlib.sha256(token.encode()).hexdigest()


def _load_session(request: Request, write: bool = False) -> tuple[str, dict]:
    store = request.app.state.store
    token = request.cookies.get(COOKIE, "")
    if not token:
        raise ServiceError("请从工作台重新进入 HR 页面", 401)
    key = _session_key(token)
    with store.connection() as db:
        raw = setting(db, key)
    if not raw:
        raise ServiceError("登录已过期，请从工作台重新进入", 401)
    record = json.loads(raw)
    if record["expiresAt"] <= _now():
        raise ServiceError("登录已过期，请从工作台重新进入", 401)
    if write and not hmac.compare_digest(
        hashlib.sha256(request.headers.get("x-hr-csrf", "").encode()).hexdigest(),
        hashlib.sha256(record["csrf"].encode()).hexdigest(),
    ):
        raise ServiceError("CSRF 校验失败", 403)
    return key, record


def _identity(store, handle: str) -> dict:
    with store.connection() as db:
        row = employee(db, handle)
    return {
        "handle": row["handle"],
        "displayName": row["display_name"],
        "role": "member",
        "jobTitle": row["job_title"],
    }


def _revalidate(record: dict) -> None:
    _gateway(
        "/internal/hr/web-session",
        {"user_id": record["user_id"], "version": record.get("bindingVersion")},
    )


def _page(title: str, body: str, record: dict | None = None, message: str = "") -> HTMLResponse:
    nav = ""
    if record:
        nav = (
            '<nav><a href="/web/me">我的申请</a><a href="/web/approvals">我的待审批</a>'
            f'<span>{html.escape(record["displayName"])}</span></nav>'
        )
    message_html = f'<div class="msg">{html.escape(message)}</div>' if message else ""
    return HTMLResponse(
        headers={"cache-control": "no-store", "referrer-policy": "no-referrer"},
        content=f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="hr-csrf" content="{html.escape(record["csrf"]) if record else ""}">
<title>{html.escape(title)} · HR</title>
<style>
:root{{color-scheme:light dark}}
body{{font-family:system-ui,sans-serif;margin:0;background:#f7f7f5;color:#1b1b1f}}
nav{{display:flex;gap:16px;align-items:center;padding:14px 20px;background:#111827;color:#fff}}
nav a{{color:#dbeafe;text-decoration:none}} nav span{{margin-left:auto;opacity:.8}}
main{{max-width:760px;margin:0 auto;padding:24px 20px}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;margin:12px 0}}
.card h3{{margin:0 0 8px;font-size:16px}} .muted{{color:#6b7280;font-size:13px}}
button{{background:#1d4ed8;color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}}
.msg{{background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:10px;margin:12px 0}}
@media (prefers-color-scheme:dark){{body{{background:#0b1020;color:#e5e7eb}} .card{{background:#111827;border-color:#1f2937}}}}
</style></head><body>{nav}<main>{message_html}{body}</main></body></html>""",
    )


@router.post("/web/ticket")
async def exchange_ticket(request: Request):
    # POST only: the ticket travels in the body, never in a URL or referrer.
    raw = (await request.body()).decode("utf-8", "ignore")
    ticket = parse_qs(raw).get("ticket", [""])[0]
    record = _gateway("/internal/hr/ticket/consume", {"ticket": ticket})
    token = secrets.token_urlsafe(32)
    entry = {
        "user_id": record["user_id"],
        "handle": record["handle"],
        "displayName": record["displayName"],
        "bindingVersion": record.get("bindingVersion"),
        "csrf": secrets.token_urlsafe(24),
        "expiresAt": _now() + TTL_MS,
    }
    with request.app.state.store.connection(write=True) as db:
        set_setting(db, _session_key(token), json.dumps(entry))
    requested = record.get("target") or "me"
    if requested == "approvals":
        target = "/web/approvals"
    elif requested.startswith("request:"):
        target = "/web/request/" + requested.split(":", 1)[1]
    else:
        target = "/web/me"
    response = RedirectResponse(target, status_code=303)
    response.headers["cache-control"] = "no-store"
    response.headers["referrer-policy"] = "no-referrer"
    response.set_cookie(
        COOKIE,
        token,
        max_age=TTL_MS // 1000,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/web/logout")
def logout(request: Request):
    response = RedirectResponse("/web/me", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response


@router.get("/web/request/{request_id}")
def request_detail(request: Request, request_id: str):
    try:
        _key, record = _load_session(request)
        _revalidate(record)
        store = request.app.state.store
        with store.connection() as db:
            row = db.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["requester_handle"] != record["handle"]:
            raise ServiceError("无权查看该申请", 403)
        body = (
            f'<article class="card" data-testid="request-detail"><h3>{html.escape(row["leave_type"])} · '
            f'{html.escape(row["start_date"])} → {html.escape(row["end_date"])}</h3>'
            f'<p>{html.escape(store.open(row["encrypted_reason"]))}</p>'
            f'<p class="muted">单号 {html.escape(row["id"])} · 状态 {html.escape(row["status"])}</p>'
            f'</article>'
        )
        return _page("申请详情", body, record)
    except ServiceError as exc:
        return _page("无法查看", f'<p class="muted">{html.escape(str(exc))}</p>', None, str(exc))


@router.get("/web/me")
def my_requests(request: Request):
    try:
        _key, record = _load_session(request)
        _revalidate(record)
        store = request.app.state.store
        with store.connection() as db:
            rows = db.execute(
                "SELECT * FROM leave_requests WHERE requester_handle=? ORDER BY created_at DESC",
                (record["handle"],),
            ).fetchall()
            cards = "".join(
                f'<article class="card"><h3>{html.escape(row["leave_type"])} · '
                f'{html.escape(row["start_date"])} → {html.escape(row["end_date"])}</h3>'
                f'<p>{html.escape(store.open(row["encrypted_reason"]))}</p>'
                f'<p class="muted">单号 {html.escape(row["id"])} · 状态 {html.escape(row["status"])}</p>'
                f"</article>"
                for row in rows
            )
        body = cards or '<p class="muted">还没有请假申请。</p>'
        return _page("我的申请", body, record)
    except ServiceError as exc:
        return _page("无法访问", f'<p class="muted">{html.escape(str(exc))}</p>', None, str(exc))


@router.get("/web/approvals")
def my_approvals(request: Request):
    try:
        _key, record = _load_session(request)
        _revalidate(record)
        store = request.app.state.store
        with store.connection() as db:
            rows = db.execute(
                """SELECT r.*, a.approver_name FROM leave_requests r
                   JOIN leave_approvals a ON a.request_id=r.id
                   WHERE a.approver_handle=? AND a.status='pending' AND r.status='pending'
                   ORDER BY r.created_at""",
                (record["handle"],),
            ).fetchall()
            cards = "".join(
                f'<article class="card"><h3>{html.escape(row["requester_name"])} · '
                f'{html.escape(row["start_date"])} → {html.escape(row["end_date"])}</h3>'
                f'<p>{html.escape(store.open(row["encrypted_reason"]))}</p>'
                f'<p class="muted">单号 {html.escape(row["id"])} · 假别 {html.escape(row["leave_type"])}</p>'
                f'<form method="post" action="/web/approvals/{html.escape(row["id"])}/approve">'
                f'<input type="hidden" name="{CSRF_FIELD}" value="{html.escape(record["csrf"])}">'
                f'<button type="submit">批准</button></form></article>'
                for row in rows
            )
        body = cards or '<p class="muted">没有待审批的申请。</p>'
        return _page("我的待审批", body, record)
    except ServiceError as exc:
        return _page("无法访问", f'<p class="muted">{html.escape(str(exc))}</p>', None, str(exc))


@router.post("/web/approvals/{request_id}/approve")
async def approve(request: Request, request_id: str):
    try:
        _key, record = _load_session(request, write=False)
        # Parsed by hand so the HR image does not need extra form middleware.
        raw = (await request.body()).decode("utf-8", "ignore")
        submitted = parse_qs(raw).get(CSRF_FIELD, [""])[0]
        if not hmac.compare_digest(
            hashlib.sha256(submitted.encode()).hexdigest(),
            hashlib.sha256(record["csrf"].encode()).hexdigest(),
        ):
            raise ServiceError("CSRF 校验失败", 403)
        # Re-check the Avatar binding on the write as well.
        _revalidate(record)
        store = request.app.state.store
        identity = _identity(store, record["handle"])
        with store.connection() as db:
            assigned = db.execute(
                """SELECT 1 FROM leave_approvals a JOIN leave_requests r ON r.id=a.request_id
                   WHERE a.request_id=? AND a.approver_handle=? AND a.status='pending'
                   AND r.status='pending'""",
                (request_id, record["handle"]),
            ).fetchone()
        if assigned is None:
            raise ServiceError("这笔申请没有分配给你，或已处理", 403)
        execute(
            store,
            identity,
            "review_leave",
            {"requestId": request_id, "decision": "approve"},
            "web_" + secrets.token_urlsafe(16),
        )
        return RedirectResponse("/web/approvals", status_code=303)
    except ServiceError as exc:
        return _page("审批失败", f'<p class="muted">{html.escape(str(exc))}</p>', None, str(exc))
