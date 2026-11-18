import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from hr_service.db import Store, set_setting
from hr_service.leaves import execute
from hr_service.outbox import publish_batch
from avatar_service.db import Store as AvatarStore
from avatar_service.receiver import Receiver
from avatar_service.stream import Runtime


class Renderer:
    async def receive(self, owner, source, leave_id):
        return source


class RecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_restart_and_lost_ack(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            hr = Store(root / "hr")
            avatar = AvatarStore(root / "avatar")
            avatar.seed_demo()
            with hr.connection(write=True) as db:
                set_setting(db, "service.key", hr.seal("test-key"))
            with avatar.open() as db:
                owner = db.execute("SELECT id FROM users WHERE handle='marco'").fetchone()[0]
                db.execute("INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES ('original',?,'test',1,1)", (owner,))
            marco = {"handle": "marco", "displayName": "Marco", "role": "member"}
            admin = {"handle": "admin", "displayName": "Admin", "role": "admin"}
            fields = {"startDate": "2026-12-01", "endDate": "2026-12-02", "reason": "test"}
            first = execute(hr, marco, "request_leave", fields, "one", "original")
            duplicate = execute(hr, marco, "request_leave", fields, "two", "other")
            leave_id = first["request"]["id"]
            self.assertEqual(duplicate["request"]["id"], leave_id)
            with hr.connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
            # First delivery attempt takes place only after the original Store is replaced.
            hr = Store(root / "hr")
            receiver = Receiver(Runtime(AvatarStore(root / "avatar"), 0, Renderer()))
            loop = asyncio.get_running_loop()

            def callback(path, body, origin):
                return asyncio.run_coroutine_threadsafe(receiver.accept(body["event"]), loop).result(5)

            async def drain(identity, direction):
                params = {"requestId": leave_id, "direction": direction}
                with patch("hr_service.outbox.post_avatar", side_effect=callback):
                    for _ in range(2):
                        execute(hr, identity, "resend_notification", params, "")
                        await asyncio.to_thread(publish_batch, hr, None)
                        await asyncio.gather(*receiver.pending)
                    self.assertEqual(execute(hr, identity, "resend_notification", params, "")["delivery"][0]["status"], "delivered")

            await drain(marco, "request")
            execute(hr, admin, "review_leave", {"requestId": leave_id, "decision": "approve"}, "review", "approval")
            await receiver.close()
            receiver = Receiver(Runtime(AvatarStore(root / "avatar"), 0, Renderer()))
            await drain(admin, "decision")
            with avatar.open() as db:
                self.assertEqual([tuple(row) for row in db.execute("SELECT direction,mode FROM hr_deliveries ORDER BY rowid")],
                                 [("request", "receiver"), ("decision", "receiver")])
                self.assertEqual(db.execute("SELECT target_session FROM hr_deliveries WHERE direction='decision'").fetchone()[0], "original")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
