import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    handle TEXT PRIMARY KEY, display_name TEXT NOT NULL, job_title TEXT NOT NULL,
    rank TEXT, approvers_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS leave_requests (
    id TEXT PRIMARY KEY, requester_handle TEXT NOT NULL REFERENCES employees(handle),
    requester_name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
    leave_type TEXT NOT NULL, encrypted_reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','cancelled')),
    decided_by_handle TEXT, decided_by_name TEXT, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, request_key TEXT NOT NULL,
    UNIQUE(requester_handle, request_key)
);
CREATE TABLE IF NOT EXISTS leave_approvals (
    request_id TEXT NOT NULL REFERENCES leave_requests(id) ON DELETE CASCADE,
    approver_handle TEXT NOT NULL REFERENCES employees(handle), approver_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    encrypted_note TEXT, decided_at INTEGER, PRIMARY KEY(request_id, approver_handle)
);
CREATE INDEX IF NOT EXISTS leave_by_requester ON leave_requests(requester_handle, created_at);
CREATE INDEX IF NOT EXISTS approval_inbox ON leave_approvals(approver_handle, status, request_id);
CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY, leave_id TEXT NOT NULL REFERENCES leave_requests(id) ON DELETE CASCADE,
    sender TEXT NOT NULL, recipient TEXT NOT NULL, direction TEXT NOT NULL,
    payload TEXT NOT NULL, source_session TEXT NOT NULL, next_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, delivered_at INTEGER,
    UNIQUE(leave_id,sender,recipient,direction)
);
CREATE INDEX IF NOT EXISTS outbox_due ON notification_outbox(delivered_at,next_at);
CREATE TABLE IF NOT EXISTS leave_review_operations (
    approver_handle TEXT NOT NULL, operation_id TEXT NOT NULL,
    leave_id TEXT NOT NULL REFERENCES leave_requests(id) ON DELETE CASCADE,
    encrypted_input TEXT NOT NULL, encrypted_result TEXT NOT NULL,
    PRIMARY KEY(approver_handle, operation_id)
);
CREATE TABLE IF NOT EXISTS leave_request_keys (
    requester_handle TEXT NOT NULL, request_key TEXT NOT NULL,
    leave_id TEXT NOT NULL REFERENCES leave_requests(id) ON DELETE CASCADE,
    PRIMARY KEY(requester_handle,request_key)
);
"""

EMPLOYEES = (
    ("admin", "管理员", "公司负责人", "L5", []),
    ("marco", "马可波罗", "小助理", "P2", ["admin"]),
    ("alice", "艾丽丝", "产品设计师", "P4", ["bob", "carol"]),
    ("bob", "鲍勃", "产品负责人", "M2", ["admin"]),
    ("carol", "卡萝", "HR 负责人", "M2", ["admin"]),
)


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "hr-python.sqlite3"
        key_path = directory / "hr-python.key"
        if not key_path.exists():
            if self.path.exists():
                raise RuntimeError("现有 HR 数据库缺少加密密钥")
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode = WAL")
            from .lifecycle import SCHEMA as LIFECYCLE_SCHEMA
            from .recruitment import SCHEMA as RECRUITMENT_SCHEMA

            db.executescript(SCHEMA + RECRUITMENT_SCHEMA + LIFECYCLE_SCHEMA)
        finally:
            db.close()
        with self.connection(write=True) as db:
            if setting(db, "demo.seeded") is None:
                db.executemany(
                    "INSERT OR IGNORE INTO employees(handle,display_name,job_title,rank,approvers_json) VALUES (?,?,?,?,?)",
                    [(handle, name, job, rank, json.dumps(approvers)) for handle, name, job, rank, approvers in EMPLOYEES],
                )
                set_setting(db, "demo.seeded", "1")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seal(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def open(self, value):
        return self.cipher.decrypt(value.encode()).decode()


def setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(db, key, value):
    db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def now():
    return time.time_ns() // 1_000_000


def identifier(prefix):
    return prefix + "_" + secrets.token_urlsafe(9)

