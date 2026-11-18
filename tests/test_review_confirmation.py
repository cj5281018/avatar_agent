import asyncio

from test_leave_confirmation import AvatarStore, ConfirmationFixture, HrStore, Turn, digest
from hr_service.leaves import execute


class ReviewConfirmationTest(ConfirmationFixture):
    def setUp(self):
        super().setUp()
        with AvatarStore(self.avatar_dir).open() as db:
            owner = db.execute("SELECT id FROM users WHERE handle='admin'").fetchone()[0]
            db.execute("INSERT INTO sessions VALUES ('review',?,'review',0,0,0,1,1)", (owner,))
        self.turn = Turn("review", owner, digest("admin"))
        self.hr = HrStore(self.hr_dir)
        actor = {"role": "member", "handle": "marco", "displayName": "Marco"}
        self.leave_id = execute(self.hr, actor, "request_leave", self.fields, "sample")["request"]["id"]

    async def prepare(self, client, decision):
        result = await self.propose(client, {"requestId": self.leave_id, "decision": decision}, "review_leave")
        self.assertEqual(result.json()["data"]["status"], "pending")
        detail = await client.get("/v1/sessions/review", headers=self.admin)
        item = detail.json()["leaveConfirmations"][-1]
        self.assertEqual(item["action"], "review_leave")
        return item["operationId"]

    def review_state(self):
        with self.hr.connection() as db:
            status = db.execute("SELECT status FROM leave_requests WHERE id=?", (self.leave_id,)).fetchone()[0]
            decision = db.execute("SELECT status,decided_at FROM leave_approvals WHERE request_id=?",
                                  (self.leave_id,)).fetchone()
            events = db.execute("SELECT id FROM notification_outbox WHERE direction='decision'").fetchall()
            count = db.execute("SELECT COUNT(*) FROM leave_review_operations").fetchone()[0]
        return status, tuple(decision), [row[0] for row in events], count

    @staticmethod
    def url(operation):
        return f"/v1/sessions/review/hr/leave-confirmations/{operation}/confirm"

    async def test_review_approval_requires_confirmation_and_is_concurrent_safe(self):
        async with self.services() as (client, _hr):
            operation = await self.prepare(client, "approve")
            self.assertEqual(await self.prepare(client, "approve"), operation)
            self.assertEqual(self.review_state(), ("pending", ("pending", None), [], 0))
            self.assertEqual((await client.post(self.url(operation), headers=self.marco)).status_code, 404)
            self.hold_forward = True
            requests = [asyncio.create_task(client.post(self.url(operation), headers=self.admin,
                                                        json={"decision": "reject"})) for _ in range(2)]
            await asyncio.wait_for(self.forward_reached.wait(), 5)
            self.release_forward.set()
            replies = await asyncio.gather(*requests)
            self.assertEqual([reply.status_code for reply in replies], [200, 200])
            for reply in replies:
                self.assertEqual(reply.json()["reviewResult"], {"status": "approved", "applied": True})
            status, decision, events, count = self.review_state()
            self.assertEqual((status, decision[0], len(events), count), ("approved", "approved", 1, 1))
            self.assertEqual(set(self.keys), {operation})

    async def test_review_rejection_replays_after_restart_without_resending(self):
        async with self.services() as (client, _hr):
            operation = await self.prepare(client, "reject")
        self.assertEqual(self.review_state(), ("pending", ("pending", None), [], 0))
        async with self.services() as (client, _hr):
            self.drop_response = True
            self.assertEqual((await client.post(self.url(operation), headers=self.admin)).status_code, 502)
            before = self.review_state()
            self.assertEqual((before[0], before[1][0], len(before[2]), before[3]), ("rejected", "rejected", 1, 1))
        async with self.services() as (client, _hr):
            result = await client.post(self.url(operation), headers=self.admin)
            self.assertEqual(result.json()["reviewResult"], {"status": "rejected", "applied": True})
            self.assertEqual(self.review_state(), before)
            self.assertEqual(self.keys, [operation, operation])
