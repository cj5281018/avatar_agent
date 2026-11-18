import json
import re
from datetime import date

from .db import ServiceError, identifier, now
from .protocol import text
from .outbox import record, resend

LEAVE_TYPES = ("annual", "sick", "personal", "compensatory", "other")
ACTIONS = {
    "who_am_i": {},
    "get_approvers": {"handle": "仅管理员可选"},
    "request_leave": {"startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD",
                      "leaveType": list(LEAVE_TYPES), "reason": "string"},
    "list_my_requests": {},
    "list_pending_approvals": {},
    "review_leave": {"requestId": "string", "decision": ["approve", "reject"], "note": "optional"},
    "cancel_leave": {"requestId": "string"},
    "resend_notification": {"requestId": "string", "direction": ["request", "decision"]},
    "list_all_requests": {"access": "Avatar admin or HR role"},
    "employee_directory": {"access": "Avatar admin or HR role"},
}


def employee(db, handle, required=True):
    row = db.execute("SELECT * FROM employees WHERE handle=? AND active=1", (handle,)).fetchone()
    if row is None and required:
        raise ServiceError("HR 人员目录找不到该员工", 404)
    return row


def employee_view(row):
    if row is None:
        return None
    return {"handle": row["handle"], "displayName": row["display_name"],
            "jobTitle": row["job_title"], **({"rank": row["rank"]} if row["rank"] else {})}


def approvers(db, row):
    return [person for handle in dict.fromkeys(json.loads(row["approvers_json"]))
            if handle != row["handle"] and (person := employee(db, handle, required=False)) is not None]


def required_leave(db, request_id):
    if not request_id:
        raise ServiceError("缺少 requestId")
    row = db.execute("SELECT * FROM leave_requests WHERE id=?", (request_id,)).fetchone()
    if row is None:
        raise ServiceError("请假单不存在", 404)
    return row


def approvals(db, request_id):
    return db.execute("SELECT * FROM leave_approvals WHERE request_id=? ORDER BY approver_handle", (request_id,)).fetchall()


def leave_view(db, store, row):
    details = []
    for item in approvals(db, row["id"]):
        view = {"handle": item["approver_handle"], "displayName": item["approver_name"], "status": item["status"]}
        if item["decided_at"] is not None:
            view["decidedAt"] = item["decided_at"]
        if item["encrypted_note"]:
            view["note"] = store.open(item["encrypted_note"])
        details.append(view)
    result = {
        "id": row["id"], "requester": {"handle": row["requester_handle"], "displayName": row["requester_name"]},
        "startDate": row["start_date"], "endDate": row["end_date"], "leaveType": row["leave_type"],
        "reason": store.open(row["encrypted_reason"]), "status": row["status"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"], "approvals": details,
    }
    if row["decided_by_handle"]:
        result["decidedBy"] = {"handle": row["decided_by_handle"], "displayName": row["decided_by_name"]}
    return result


def iso_date(value, name):
    raw = text(value)
    try:
        parsed = date.fromisoformat(raw)
        if parsed.isoformat() == raw:
            return raw
    except ValueError:
        pass
    raise ServiceError(f"{name} 必须是有效的 YYYY-MM-DD 日期")


def submit(db, store, identity, params, key, source_session=None):
    if not key or len(key) > 200:
        raise ServiceError("提交需要有效的协议 requestId")
    start = iso_date(params.get("startDate"), "startDate")
    end = iso_date(params.get("endDate"), "endDate")
    kind = text(params.get("leaveType")) or "annual"
    reason = text(params.get("reason"))
    if end < start or kind not in LEAVE_TYPES or not 1 <= len(reason) <= 2000:
        raise ServiceError("结束日期不能早于开始日期，须使用支持的假别并填写 1–2000 字的原因")
    handle = identity["handle"]
    requester = employee(db, handle)
    row = db.execute(
        """SELECT * FROM leave_requests WHERE requester_handle=? AND request_key=?
           UNION ALL SELECT r.* FROM leave_request_keys k JOIN leave_requests r ON r.id=k.leave_id
           WHERE k.requester_handle=? AND k.request_key=? LIMIT 1""", (handle, key, handle, key)
    ).fetchone()
    if row is not None:
        if (row["start_date"], row["end_date"], row["leave_type"], store.open(row["encrypted_reason"])) != (start, end, kind, reason):
            raise ServiceError("同一 requestId 已用于不同内容的申请", 409)
    else:
        row = db.execute("""SELECT * FROM leave_requests WHERE requester_handle=? AND start_date=?
                            AND end_date=? AND leave_type=? AND status IN ('pending','approved')
                            ORDER BY created_at LIMIT 1""", (handle, start, end, kind)).fetchone()
        if row is not None:
            if store.open(row["encrypted_reason"]) != reason:
                raise ServiceError("同日期和假别已有有效申请：" + row["id"], 409)
            db.execute("INSERT INTO leave_request_keys VALUES (?,?,?)", (handle, key, row["id"]))
    if row is None:
        people = approvers(db, requester)
        if not people:
            raise ServiceError("HR 目录还没有配置该人员的审批人", 409)
        request_id = identifier("leave")
        stamp = now()
        db.execute(
            """INSERT INTO leave_requests(id,requester_handle,requester_name,start_date,end_date,leave_type,
               encrypted_reason,status,created_at,updated_at,request_key) VALUES (?,?,?,?,?,?,?,'pending',?,?,?)""",
            (request_id, handle, identity["displayName"], start, end, kind, store.seal(reason), stamp, stamp, key),
        )
        db.executemany(
            "INSERT INTO leave_approvals(request_id,approver_handle,approver_name) VALUES (?,?,?)",
            [(request_id, person["handle"], person["display_name"]) for person in people],
        )
        row = required_leave(db, request_id)
    request_id = row["id"]
    name = row["requester_name"]
    assigned = approvals(db, request_id)
    deliveries = [
        {"handle": item["approver_handle"], "title": f"{name} 的请假审批",
         "text": f"{name}（@{handle}）提交了 {start} 至 {end} 的请假申请，类型 {kind}，单号 {request_id}。请在 Avatar 中确认同意或拒绝。",
         "expectReply": True}
        for item in assigned if row["status"] == "pending" and item["status"] == "pending"
    ]
    views = []
    if source_session:
        record(db, store, row, handle, "request", deliveries, source_session)
    for item in assigned:
        view = employee_view(employee(db, item["approver_handle"], required=False)) or {"handle": item["approver_handle"]}
        views.append({**view, "displayName": item["approver_name"]})
    return {"request": leave_view(db, store, row), "approvers": views, "coordination": {"deliveries": deliveries}}


def review(db, store, identity, params, source_session=None, *, confirmed_operation=False):
    row = required_leave(db, text(params.get("requestId")))
    decision = text(params.get("decision"))
    note = text(params.get("note"))
    if decision not in ("approve", "reject") or len(note) > 2000:
        raise ServiceError("decision 必须是 approve 或 reject，备注最多 2000 字")
    request_id, handle = row["id"], identity["handle"]
    item = db.execute(
        "SELECT * FROM leave_approvals WHERE request_id=? AND approver_handle=?", (request_id, handle)
    ).fetchone()
    if item is None:
        raise ServiceError("你不是这张请假单的审批人", 403)
    status = "approved" if decision == "approve" else "rejected"
    already_final = row["status"] != "pending"
    applied = item["status"] == "pending" and not already_final
    if item["status"] != "pending":
        if item["status"] != status and not already_final:
            raise ServiceError("该审批人已提交决定", 409)
        if item["status"] != status or row["status"] == "cancelled":
            return {"request": leave_view(db, store, row), "alreadyFinal": already_final, "decisionApplied": False}
        note = store.open(item["encrypted_note"]) if item["encrypted_note"] else ""
    elif already_final:
        return {"request": leave_view(db, store, row), "alreadyFinal": True, "decisionApplied": False}
    else:
        stamp = now()
        db.execute(
            """UPDATE leave_approvals SET status=?, encrypted_note=?, decided_at=?
               WHERE request_id=? AND approver_handle=?""",
            (status, store.seal(note) if note else None, stamp, request_id, handle),
        )
        other = db.execute("SELECT 1 FROM leave_approvals WHERE request_id=? AND status!='rejected'", (request_id,)).fetchone()
        if status == "approved" or other is None:
            db.execute(
                "UPDATE leave_requests SET status=?,decided_by_handle=?,decided_by_name=?,updated_at=? WHERE id=?",
                (status, handle, identity["displayName"], stamp, request_id),
            )
        else:
            db.execute("UPDATE leave_requests SET updated_at=? WHERE id=?", (stamp, request_id))
        row = required_leave(db, request_id)
    requester = {"handle": row["requester_handle"], "displayName": row["requester_name"]}
    word = "同意" if decision == "approve" else "拒绝"
    remark = f"，备注：{note}" if note else ""
    notice = (
        f"{identity['displayName']} 已{word}你的请假申请（{row['start_date']} 至 {row['end_date']}，"
        f"单号 {request_id}）{remark}。当前状态：{row['status']}。"
    )
    item = {"handle": requester["handle"], "title": f"请假单 {request_id} 的审批结果", "text": notice}
    if source_session and (applied or not confirmed_operation):
        record(db, store, row, handle, "decision", [item], source_session)
    return {
        "request": leave_view(db, store, row), "requester": requester, "decisionApplied": applied,
        **({"alreadyFinal": True} if already_final else {}),
        "coordination": {"notifyRequester": {"handle": requester["handle"],
                         "title": f"请假单 {request_id} 的审批结果", "text": notice}},
    }


def review_operation(db, store, identity, params, key, source_session):
    if not re.fullmatch(r"rop_[A-Za-z0-9_-]{16}", key):
        raise ServiceError("审批操作 ID 无效")
    actor = identity["handle"]
    encoded = json.dumps({name: text(params.get(name)) for name in ("requestId", "decision", "note")},
                         ensure_ascii=False, sort_keys=True)
    previous = db.execute(
        "SELECT * FROM leave_review_operations WHERE approver_handle=? AND operation_id=?", (actor, key)
    ).fetchone()
    if previous:
        if store.open(previous["encrypted_input"]) != encoded:
            raise ServiceError("同一审批操作 ID 已用于不同内容", 409)
        return json.loads(store.open(previous["encrypted_result"]))
    result = review(db, store, identity, params, source_session, confirmed_operation=True)
    db.execute("INSERT INTO leave_review_operations VALUES (?,?,?,?,?)",
               (actor, key, result["request"]["id"], store.seal(encoded), store.seal(json.dumps(result))))
    return result


def execute(store, identity, action, params, request_key, source_session=None):
    handle = identity["handle"]
    is_admin = identity["role"] == "admin"
    if action == "describe":
        return {"service": "HR Daily Leave", "approvalPolicy": "任一审批人同意即通过；只有全部审批人拒绝才拒绝。", "actions": ACTIONS}
    if action not in ACTIONS:
        raise ServiceError(f"未知 HR action：{action}")
    if action in ("list_all_requests", "employee_directory"):
        if not is_admin and not re.search(r"(^|\s)HR(\s|$)", identity["jobTitle"], re.I):
            raise ServiceError("只有 Avatar 管理员或 HR 岗位可以查看全员数据", 403)
    with store.connection(write=action in ("request_leave", "review_leave", "cancel_leave", "resend_notification")) as db:
        if action == "who_am_i":
            return {"identity": identity, "employee": employee_view(employee(db, handle, required=False))}
        if action == "get_approvers":
            target = text(params.get("handle")) or handle
            if target != handle and not is_admin:
                raise ServiceError("只能查询自己的请假审批人", 403)
            person = employee(db, target)
            return {"employee": employee_view(person), "approvers": [employee_view(a) for a in approvers(db, person)]}
        if action == "request_leave":
            return submit(db, store, identity, params, request_key, source_session)
        if action == "review_leave":
            if request_key.startswith("rop_"):
                return review_operation(db, store, identity, params, request_key, source_session)
            return review(db, store, identity, params, source_session)
        if action == "resend_notification":
            return resend(db, identity, required_leave(db, text(params.get("requestId"))), params.get("direction"))
        if action == "cancel_leave":
            row = required_leave(db, text(params.get("requestId")))
            if row["requester_handle"] != handle:
                raise ServiceError("只能撤销自己的请假单", 403)
            if row["status"] == "pending":
                db.execute("UPDATE leave_requests SET status='cancelled',updated_at=? WHERE id=?", (now(), row["id"]))
            elif row["status"] != "cancelled":
                raise ServiceError("只有待审批请假单可以撤销", 409)
            return {"request": leave_view(db, store, required_leave(db, row["id"]))}
        if action == "employee_directory":
            return {"employees": [{**employee_view(p), "approvers": [employee_view(a) for a in approvers(db, p)]}
                                  for p in db.execute("SELECT * FROM employees WHERE active=1 ORDER BY handle")]}
        if action == "list_my_requests":
            rows = db.execute("SELECT * FROM leave_requests WHERE requester_handle=? ORDER BY created_at DESC", (handle,))
        elif action == "list_pending_approvals":
            rows = db.execute(
                """SELECT r.* FROM leave_requests r JOIN leave_approvals a ON a.request_id=r.id
                   WHERE a.approver_handle=? AND a.status='pending' AND r.status='pending' ORDER BY r.created_at""", (handle,)
            )
        else:
            rows = db.execute("SELECT * FROM leave_requests ORDER BY created_at DESC")
        return {"requests": [leave_view(db, store, row) for row in rows]}

