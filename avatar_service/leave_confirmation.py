import json
import re
from datetime import date

from fastapi import HTTPException

from .db import now, uid

TTL_MS = 30 * 60 * 1000
LEAVE_TYPES = {"annual", "sick", "personal", "compensatory", "other"}


def validate_leave(args):
    if set(args) - {"startDate", "endDate", "leaveType", "reason"} or any(
        not isinstance(value, str) for value in args.values()
    ):
        raise HTTPException(400, "请假参数只接受日期、假别和事由")
    start, end = (args.get(key, "").strip() for key in ("startDate", "endDate"))
    try:
        if any(date.fromisoformat(value).isoformat() != value for value in (start, end)):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "起止日期必须为有效的 YYYY-MM-DD") from None
    kind = args.get("leaveType", "annual").strip() or "annual"
    reason = args.get("reason", "").strip()
    if end < start or kind not in LEAVE_TYPES or not 1 <= len(reason) <= 2000:
        raise HTTPException(400, "请检查起止日期、假别和事由（1–2000 字）")
    return {"startDate": start, "endDate": end, "leaveType": kind, "reason": reason}


def validate_review(args):
    if set(args) - {"requestId", "decision", "note"} or any(
        not isinstance(value, str) for value in args.values()
    ):
        raise HTTPException(400, "审批参数只接受单号、决定和备注")
    request_id = args.get("requestId", "").strip()
    decision = args.get("decision", "").strip()
    note = args.get("note", "").strip()
    if not re.fullmatch(r"leave_[A-Za-z0-9_-]{1,100}", request_id):
        raise HTTPException(400, "审批需要有效的请假单号")
    if decision not in ("approve", "reject") or len(note) > 2000:
        raise HTTPException(400, "审批决定须为 approve 或 reject，备注最多 2000 字")
    return {"requestId": request_id, "decision": decision, "note": note}


def view(row):
    status = row["status"]
    if status == "pending" and row["expires_at"] <= now():
        status = "expired"
    return {"operationId": row["id"], "action": row["action"],
            **({"reviewResult": json.loads(row["review_result"])} if row["review_result"] else {}), "input": json.loads(row["input"]), "status": status,
            "expiresAt": row["expires_at"], "leaveId": row["leave_id"]}


class LeaveConfirmations:
    def __init__(self, store):
        self.store = store
        with store.open() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS leave_confirmations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL, input TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','submitted')),
                    leave_id TEXT
                );
                CREATE INDEX IF NOT EXISTS leave_confirmations_session
                    ON leave_confirmations(session_id);
            """)

            columns = {row["name"] for row in db.execute("PRAGMA table_info(leave_confirmations)")}
            if "action" not in columns:
                db.execute("ALTER TABLE leave_confirmations ADD COLUMN action TEXT NOT NULL DEFAULT 'request_leave'")
            if "review_result" not in columns:
                db.execute("ALTER TABLE leave_confirmations ADD COLUMN review_result TEXT")

    def propose(self, turn, args, action="request_leave"):
        validate = validate_review if action == "review_leave" else validate_leave
        encoded = json.dumps(validate(args), ensure_ascii=False, sort_keys=True)
        stamp = now()
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=? AND user_id=?",
                              (turn.session_id, turn.owner)).fetchone():
                raise HTTPException(404, "会话不存在")
            row = db.execute("""
                SELECT * FROM leave_confirmations
                WHERE user_id=? AND session_id=? AND action=? AND input=? AND (
                    (status='pending' AND expires_at>?) OR status='confirmed'
                    OR (status='submitted' AND turn_id=?)
                ) ORDER BY rowid DESC LIMIT 1
            """, (turn.owner, turn.session_id, action, encoded, stamp, turn.turn_id)).fetchone()
            if row is None:
                operation = uid("rop" if action == "review_leave" else "lop")
                db.execute("""INSERT INTO leave_confirmations
                    (id,user_id,session_id,turn_id,input,expires_at,status,action)
                    VALUES (?,?,?,?,?,?,'pending',?)""",
                           (operation, turn.owner, turn.session_id, turn.turn_id, encoded, stamp + TTL_MS, action))
                row = self.owned(db, turn.owner, turn.session_id, operation)
        return view(row)

    @staticmethod
    def owned(db, owner, session, operation):
        row = db.execute("SELECT * FROM leave_confirmations WHERE id=? AND user_id=? AND session_id=?",
                         (operation, owner, session)).fetchone()
        if row is None:
            raise HTTPException(404, "请假确认记录不存在")
        return row

    @staticmethod
    def list_for(db, owner, session):
        return [view(row) for row in db.execute("""
            SELECT * FROM leave_confirmations WHERE user_id=? AND session_id=? ORDER BY rowid
        """, (owner, session))]

    def confirm(self, owner, session, operation):
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            item = view(self.owned(db, owner, session, operation))
            if item["status"] == "expired":
                raise HTTPException(409, "确认已过期，请在对话中重新发起")
            if item["status"] == "pending":
                db.execute("UPDATE leave_confirmations SET status='confirmed' WHERE id=?", (operation,))
                item["status"] = "confirmed"
        return item

    def complete(self, owner, session, operation, leave_id, review_result=None):
        with self.store.open() as db:
            db.execute("""UPDATE leave_confirmations SET status='submitted',leave_id=?,review_result=?
                          WHERE id=? AND user_id=? AND session_id=? AND status='confirmed'""",
                       (leave_id, json.dumps(review_result) if review_result else None, operation, owner, session))
            return view(self.owned(db, owner, session, operation))
