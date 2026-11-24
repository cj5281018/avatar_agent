import json
import os
from datetime import datetime, timezone
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, directory):
        path = Path(directory)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path / "it.sqlite3"
        with self.connection(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                public TEXT NOT NULL, description TEXT, location TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS ticket_operations (
                scope TEXT NOT NULL, owner TEXT NOT NULL, action TEXT NOT NULL, operation TEXT NOT NULL,
                input_digest TEXT NOT NULL, result TEXT NOT NULL,
                ticket_number INTEGER NOT NULL UNIQUE REFERENCES tickets(number),
                PRIMARY KEY(scope,owner,action,operation)
            )""")
            if setting(db, "tickets.seeded") is None:
                from .actions import TICKETS
                db.executemany("INSERT OR IGNORE INTO tickets(number,owner,public) VALUES (?,?,?)",
                               [(int(t.ticketId[3:]), owner, t.model_dump_json()) for owner, t in TICKETS])
                set_setting(db, "tickets.seeded", "1")

    def tickets(self, owner, ticket_id=None):
        with self.connection() as db:
            if ticket_id is None:
                rows = db.execute("SELECT public FROM tickets WHERE owner=?", (owner,))
            else:
                number = ticket_id[3:] if ticket_id.startswith("IT-") else ""
                if not number.isascii() or not number.isdecimal() or len(number) > 18:
                    return []
                rows = db.execute("SELECT public FROM tickets WHERE owner=? AND number=?",
                                  (owner, int(number)))
                return [row[0] for row in rows if json.loads(row[0])["ticketId"] == ticket_id]
            return [row[0] for row in rows]

    def operation(self, db, scope, owner, action, operation):
        return db.execute("""SELECT input_digest,result FROM ticket_operations
            WHERE scope=? AND owner=? AND action=? AND operation=?""", (scope, owner, action, operation)).fetchone()

    def create_ticket(self, scope, owner, action, operation, data):
        from avatar_contract import manifest_digest

        digest = manifest_digest(data)
        with self.connection(write=True) as db:
            existing = self.operation(db, scope, owner, action, operation)
            if existing:
                if existing[0] != digest:
                    raise IdempotencyConflict
                return json.loads(existing[1])
            cursor = db.execute("INSERT INTO tickets(owner,public,description,location) VALUES (?,'',?,?)",
                                (owner, data["description"], data["location"]))
            number = cursor.lastrowid
            public = {"ticketId": f"IT-{number}", "title": data["title"], "status": "open",
                      "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            result = {"ticket": public}
            db.execute("UPDATE tickets SET public=? WHERE number=?", (json.dumps(public), number))
            db.execute("INSERT INTO ticket_operations VALUES (?,?,?,?,?,?,?)",
                       (scope, owner, action, operation, digest, json.dumps(result), number))
            return result

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    @staticmethod
    def seal(value):
        import hashlib
        return hashlib.sha256(value.encode()).hexdigest()


class IdempotencyConflict(Exception):
    pass


def service_key():
    return os.environ.get("PY_AVATAR_PLUGIN_IT_KEY", "")


def setting(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(db, key, value):
    db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
