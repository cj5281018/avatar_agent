import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from hr_service.app import create_app
from hr_service.db import Store
from hr_service.protocol import VERSION, setup_code


class ContractTest(unittest.TestCase):
    def setUp(self):
        config = patch.dict(os.environ, {"HR_AVATAR_BASE_URL": "http://127.0.0.1:8788",
                                         "HR_PUBLIC_URL": "http://127.0.0.1:8891"})
        config.start()
        self.addCleanup(config.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = TestClient(create_app(self.directory.name))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.assertFalse(self.client.get("/health").json()["paired"])
        code = setup_code(Store(self.directory.name))
        base = "http://127.0.0.1:8788"
        self.pairing = {
            "protocolVersion": VERSION, "setupCode": code,
            "service": {"id": "svc_test", "slug": "hr-leave-python", "name": "HR Python"},
            "avatar": {"baseUrl": base, "identityCheckUrl": base + "/identitycheck", "serviceCheckUrl": base + "/servicecheck"},
            "credentials": {"serviceKey": "test-service-key"},
        }
        callback = patch("hr_service.protocol.post_avatar", side_effect=self.avatar_callback)
        self.avatar = callback.start()
        self.addCleanup(callback.stop)
        malicious = {**self.pairing, "avatar": {**self.pairing["avatar"], "serviceCheckUrl": "http://example.org/"}}
        self.assertEqual(self.client.post("/setup", json=malicious).status_code, 400)
        self.avatar.assert_not_called()
        self.assertEqual(self.client.post("/setup", json={**self.pairing, "setupCode": "wrong"}).status_code, 401)
        self.avatar.assert_not_called()
        paired = self.client.post("/setup", json=self.pairing)
        self.assertEqual(paired.status_code, 200, paired.text)
        self.assertEqual(paired.json()["invoke"], {"transport": "http", "target": "http://127.0.0.1:8891/avatar"})
        self.assertTrue(self.client.get("/health").json()["paired"])
        self.assertEqual(self.client.post("/setup", json=self.pairing).status_code, 200)
        rotated = {**self.pairing, "credentials": {"serviceKey": "new-key"}}
        self.assertEqual(self.client.post("/setup", json=rotated).status_code, 409)

    def avatar_callback(self, path, payload):
        self.assertEqual(payload["serviceKey"], "test-service-key")
        if path == "/servicecheck":
            return {"ok": True, "service": {"id": "svc_test"}}
        self.assertEqual(path, "/identitycheck")
        handle = payload["userKey"]
        if handle not in ("marco", "admin", "alice", "bob", "carol"):
            return {"ok": False}
        return {"ok": True, "user": {"id": "usr_" + handle, "handle": handle, "displayName": handle,
                                      "role": "admin" if handle == "admin" else "member"}}

    def call(self, actor, action, params=None, key="srq_submit"):
        return self.client.post("/avatar", json={"protocolVersion": VERSION, "requestId": key,
                                                 "userKey": actor, "action": action, "input": params})

    def submit(self, actor="marco", key="srq_submit"):
        return self.call(actor, "request_leave", {
            "startDate": "2026-12-10", "endDate": "2026-12-11", "leaveType": "annual", "reason": "模拟事由",
        }, key)

    def test_submit_approve_query_and_idempotency(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            replies = list(executor.map(lambda _: self.submit(), range(4)))
        for reply in replies:
            self.assertEqual(reply.status_code, 200, reply.text)
        request_ids = {reply.json()["data"]["request"]["id"] for reply in replies}
        self.assertEqual(len(request_ids), 1)
        request_id = request_ids.pop()
        delivery = replies[0].json()["data"]["coordination"]["deliveries"][0]
        self.assertEqual(delivery["handle"], "admin")
        self.assertIn(request_id, delivery["text"])
        self.assertEqual(self.call("alice", "list_my_requests").json()["data"]["requests"], [])
        self.assertEqual(self.call("marco", "list_all_requests").status_code, 403)
        self.assertEqual(self.call("expired", "list_my_requests").status_code, 401)
        self.assertEqual(self.call("admin", "list_pending_approvals").json()["data"]["requests"][0]["id"], request_id)

        decision = {"requestId": request_id, "decision": "approve", "note": "模拟审批备注"}
        self.assertEqual(self.call("alice", "review_leave", decision).status_code, 403)
        reply = self.call("admin", "review_leave", decision)
        self.assertEqual(reply.status_code, 200, reply.text)
        data = reply.json()["data"]
        self.assertEqual(data["request"]["status"], "approved")
        self.assertEqual(data["coordination"]["notifyRequester"]["handle"], "marco")
        repeat = self.call("admin", "review_leave", {**decision, "note": "新备注"}).json()["data"]
        self.assertTrue(repeat["alreadyFinal"])
        self.assertEqual(repeat["request"], data["request"])
        self.assertEqual(self.call("admin", "list_pending_approvals").json()["data"]["requests"], [])
        requests = self.call("marco", "list_my_requests").json()["data"]["requests"]
        self.assertEqual(requests, [data["request"]])
        changed = self.call("marco", "request_leave", {"startDate": "2026-12-10", "endDate": "2026-12-11", "reason": "不同事由"})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.call("marco", "cancel_leave", {"requestId": request_id}).status_code, 409)
        with sqlite3.connect(Store(self.directory.name).path) as db:
            self.assertNotIn("模拟事由", db.execute("SELECT encrypted_reason FROM leave_requests").fetchone()[0])
            self.assertNotIn("模拟审批备注", db.execute("SELECT encrypted_note FROM leave_approvals").fetchone()[0])

    def test_reject_all_and_cancel(self):
        request_id = self.submit("alice").json()["data"]["request"]["id"]
        rejected = {"requestId": request_id, "decision": "reject"}
        first = self.call("bob", "review_leave", rejected).json()["data"]["request"]
        self.assertEqual(first["status"], "pending")
        second = self.call("carol", "review_leave", rejected).json()["data"]["request"]
        self.assertEqual(second["status"], "rejected")
        cancel_id = self.submit().json()["data"]["request"]["id"]
        self.assertEqual(self.call("alice", "cancel_leave", {"requestId": cancel_id}).status_code, 403)
        self.assertEqual(self.call("marco", "cancel_leave", {"requestId": cancel_id}).json()["data"]["request"]["status"], "cancelled")

    def test_invalid_dates(self):
        bad = self.call("marco", "request_leave", {"startDate": "2026-02-30", "endDate": "2026-03-01", "reason": "模拟"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.call("marco", "list_my_requests").json()["data"]["requests"], [])


if __name__ == "__main__":
    unittest.main()

