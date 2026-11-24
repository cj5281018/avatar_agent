import hashlib
import json
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def now():
    return time.time_ns() // 1_000_000


def uid(prefix):
    return prefix + "_" + secrets.token_urlsafe(12)


def data_dir():
    return Path(os.environ.get("PY_AVATAR_DATA_DIR", "data/avatar"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def valid_password(user, password):
    salt = user["salt"] if user else "0" * 32
    hashed = password_hash(password, salt)
    return user is not None and hmac.compare_digest(hashed, user["password_hash"])


class Store:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "avatar.sqlite3"
        with self.open() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, handle TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    role TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS logins (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    title TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
                    archived INTEGER NOT NULL DEFAULT 0, unread INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(user_id, updated_at);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL, text TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id);
            """)

            if not any(row["name"] == "origin" for row in db.execute("PRAGMA table_info(messages)")):
                db.execute("ALTER TABLE messages ADD COLUMN origin TEXT")
            if not any(row["name"] == "activity" for row in db.execute("PRAGMA table_info(messages)")):
                db.execute("ALTER TABLE messages ADD COLUMN activity TEXT")

    @contextmanager
    def open(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed_demo(self):
        created = []
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            for handle, name, role in (("admin", "测试管理员", "admin"), ("marco", "马可波罗", "member")):
                if db.execute("SELECT 1 FROM users WHERE handle=?", (handle,)).fetchone():
                    continue
                password = secrets.token_urlsafe(18)
                salt = secrets.token_hex(16)
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                           (uid("usr"), handle, name, role, salt, password_hash(password, salt)))
                created.append((handle, password))
        return created

    def login_user(self, key):
        with self.open() as db:
            return db.execute(
                """SELECT u.*,l.expires_at FROM logins l JOIN users u ON u.id=l.user_id
                   WHERE token_hash=? AND expires_at>?""", (key, now())
            ).fetchone()


def user_view(row):
    return {"id": row["id"], "handle": row["handle"], "displayName": row["name"],
            "role": row["role"], "approvalPolicy": "ask", "contactable": True}


def message_view(row):
    return {"id": row["id"], "sessionId": row["session_id"], "role": row["role"],
            "text": row["text"], "attachments": [], "activity": json.loads(row["activity"]) if row["activity"] else [],
            "origin": json.loads(row["origin"]) if row["origin"] else
            {"kind": "self" if row["role"] == "user" else "hermes"},
            "createdAt": row["created_at"]}


def append(db, session_id, role, text, unread=False, origin=None, activity=None):
    stamp = now()
    message_id = uid("msg")
    db.execute("INSERT INTO messages(id,session_id,role,text,created_at,origin,activity) VALUES (?,?,?,?,?,?,?)",
               (message_id, session_id, role, text, stamp, json.dumps(origin) if origin else None, json.dumps(activity) if activity else None))
    db.execute("UPDATE sessions SET updated_at=?, unread=unread+? WHERE id=?",
               (stamp, int(unread), session_id))
    return message_view(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())


def summary(db, row):
    message = db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
                         (row["id"],)).fetchone()
    count = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (row["id"],)).fetchone()[0]
    result = {"id": row["id"], "title": row["title"], "kind": "chat", "pinned": bool(row["pinned"]),
              "archived": bool(row["archived"]), "unread": row["unread"], "messageCount": count,
              "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
    if message:
        result.update(lastMessagePreview=message["text"][:160], lastMessageOrigin=message_view(message)["origin"])
    return result

