"""Validated employee bindings for the private workbench integration."""
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from secrets import token_urlsafe
from avatar_service.db import digest, now


class EmployeeBindings:
    def __init__(self, store, encryption_key):
        self.store = store
        self.cipher = Fernet(encryption_key)
        with store.open() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS workbench_bindings(
                workbench_user TEXT PRIMARY KEY,
                employee_id TEXT NOT NULL UNIQUE REFERENCES users(id),
                credential BLOB NOT NULL
            )""")

            db.execute("""CREATE TABLE IF NOT EXISTS workbench_hr_directory(
                workbench_user TEXT PRIMARY KEY,
                employee_id TEXT NOT NULL UNIQUE REFERENCES users(id)
            )""")

    def assign(self, workbench_user, handle):
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            employee = db.execute("SELECT id FROM users WHERE handle=?", (handle,)).fetchone()
            if employee is None:
                raise HTTPException(404, "找不到该 HR 身份")
            employee_id = employee["id"]
            for table in ("workbench_hr_directory", "workbench_bindings"):
                conflict = db.execute(
                    f"""SELECT 1 FROM {table}
                        WHERE (employee_id=? AND workbench_user<>?)
                           OR (workbench_user=? AND employee_id<>?)""",
                    (employee_id, workbench_user, workbench_user, employee_id),
                ).fetchone()
                if conflict:
                    raise HTTPException(409, "工作台账号或 HR 身份已有其他关联")
            db.execute("INSERT OR IGNORE INTO workbench_hr_directory VALUES (?,?)",
                       (workbench_user, employee_id))

    def renew(self, workbench_user):
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            person = db.execute("""SELECT u.id,u.handle FROM workbench_hr_directory d
                JOIN users u ON d.employee_id=u.id WHERE d.workbench_user=?""",
                (workbench_user,)).fetchone()
            if person is None:
                raise HTTPException(403, "当前工作台账号尚未由管理员关联 HR 人员目录")
            if db.execute("""SELECT 1 FROM workbench_bindings WHERE
                (employee_id=? AND workbench_user<>?) OR (workbench_user=? AND employee_id<>?)""",
                (person["id"], workbench_user, workbench_user, person["id"])).fetchone():
                raise HTTPException(409, "员工身份绑定冲突，请联系管理员")
            self._remove_session(db, workbench_user)
            token = token_urlsafe(32)
            expiry = now() + 7200000
            db.execute("INSERT INTO logins VALUES(?,?,?)", (digest(token), person["id"], expiry))
            db.execute("INSERT INTO workbench_bindings VALUES(?,?,?)",
                       (workbench_user, person["id"], self.cipher.encrypt(token.encode())))
            return {"handle": person["handle"], "expiresAt": expiry}

    def _remove_session(self, db, workbench_user):
        row = db.execute("SELECT credential FROM workbench_bindings WHERE workbench_user=?",
                         (workbench_user,)).fetchone()
        if row:
            try:
                key = digest(self.cipher.decrypt(row["credential"]).decode())
            except InvalidToken:
                key = None
            db.execute("DELETE FROM logins WHERE token_hash=?", (key,))
            db.execute("DELETE FROM workbench_bindings WHERE workbench_user=?", (workbench_user,))

    def bind(self, workbench_user, token):
        employee = self.store.login_user(digest(token))
        if employee is None:
            raise HTTPException(401, "员工凭据无效或已过期")
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            conflict = db.execute(
                """SELECT 1 FROM workbench_bindings
                   WHERE (employee_id = ? AND workbench_user <> ?)
                      OR (workbench_user = ? AND employee_id <> ?)""",
                (employee["id"], workbench_user, workbench_user, employee["id"]),
            ).fetchone()
            if conflict:
                raise HTTPException(409, "该工作台账号或员工身份已绑定")
            db.execute(
                """INSERT INTO workbench_bindings VALUES (?, ?, ?)
                   ON CONFLICT(workbench_user) DO UPDATE SET credential=excluded.credential""",
                (workbench_user, employee["id"], self.cipher.encrypt(token.encode())),
            )
        return employee["id"]

    def credential(self, workbench_user):
        with self.store.open() as db:
            row = db.execute(
                "SELECT employee_id,credential FROM workbench_bindings WHERE workbench_user=?",
                (workbench_user,),
            ).fetchone()
        if row is None:
            raise HTTPException(403, "请先绑定员工身份")
        try:
            token = self.cipher.decrypt(row["credential"]).decode()
            key = digest(token)
        except InvalidToken as exc:
            raise HTTPException(403, "请重新绑定员工身份") from exc
        employee = self.store.login_user(key)
        if employee is None or employee["id"] != row["employee_id"]:
            raise HTTPException(403, "员工绑定已过期，请续期")
        return employee, token

    def resolve(self, workbench_user):
        employee, token = self.credential(workbench_user)
        return employee, digest(token)

    def unlink(self, workbench_user):
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            self._remove_session(db, workbench_user)
