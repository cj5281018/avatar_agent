import asyncio
import unittest
from dataclasses import replace

from avatar_service.db import now
from avatar_service.operations import Operations
from it_helpdesk_service.store import IdempotencyConflict
from test_it_helpdesk_contract import ItFixture


class OperationsTest(ItFixture):
    allowed_actions = ItFixture.allowed_actions + ["create_ticket"]
    params = {"title": "网络故障", "location": " 三楼 ", "description": "无法联网"}
    member = {"Authorization": "Bearer member"}

    async def propose(self, **extra):
        response = await self.tool(action="create_ticket", input={**self.params, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["operationId"]

    async def change(self, key, method, headers=None):
        return await self.avatar.post(f"/v1/sessions/fixture/operations/{key}/{method}",
                                      headers=headers or self.member, json={"title": "ignore this"})

    def count(self):
        with self.it_store.connection() as db:
            return db.execute("SELECT COUNT(*) FROM ticket_operations").fetchone()[0]

    async def test_confirmation_boundaries_and_concurrency(self):
        key = await self.propose()
        self.assertEqual(await self.propose(), key)
        for field in ("employeeId", "confirmed", "operationId"):
            response = await self.tool(action="create_ticket", input={**self.params, field: True})
            self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.change(key, "confirm", self.admin)).status_code, 404)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.calls.count("/avatar"), 0)
        self.mode = "hold"
        task = asyncio.create_task(self.change(key, "confirm"))
        try:
            await asyncio.wait_for(self.reached.wait(), 4)
            self.assertEqual((await self.change(key, "confirm")).json()["status"], "executing")
            self.assertEqual((await self.change(key, "cancel")).json()["status"], "executing")
            self.assertEqual((await self.avatar.delete("/v1/sessions/fixture", headers=self.member)).status_code, 409)
        finally:
            self.release.set()
        first = (await task).json()
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["fields"]["location"], "三楼")
        self.assertEqual(first["result"], (await self.change(key, "confirm")).json()["result"])
        self.assertEqual(self.calls.count("/avatar"), 1)
        self.assertEqual(self.count(), 1)
        get = await self.tool(action="get_ticket", input={"ticketId": first["result"]["ticket"]["ticketId"]})
        self.assertEqual(get.json()["data"], first["result"])

    async def test_cancel_expire_and_revoked_action(self):
        a = await self.propose()
        self.assertEqual((await self.change(a, "cancel")).json()["status"], "cancelled")
        self.assertEqual((await self.change(a, "confirm")).json()["status"], "cancelled")
        b = await self.propose(title="过期")
        with self.store.open() as db:
            db.execute("UPDATE operations SET expires=? WHERE id=?", (now()-1, b))
        self.assertEqual((await self.change(b, "confirm")).json()["status"], "expired")
        c = await self.propose(title="停用")
        service = self.broker.services["it-helpdesk"]
        self.broker.services["it-helpdesk"] = replace(service, allowed=("get_ticket",))
        self.assertEqual((await self.change(c, "confirm")).status_code, 403)
        self.assertEqual(self.calls.count("/avatar"), 0)

    async def test_recovery_after_commit_and_retry_before_commit(self):
        key = await self.propose()
        self.mode = "lost"
        self.assertEqual((await self.change(key, "confirm")).json()["status"], "unknown")
        with self.store.open() as db:
            db.execute("UPDATE operations SET status='executing' WHERE id=?", (key,))
        old = self.broker.operations
        self.broker.operations = Operations(self.broker, old.publish)
        value = (await self.change(key, "reconcile")).json()
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.calls.count("/avatar"), 1)
        self.assertEqual(self.calls.count("/operations/status"), 1)

        key = await self.propose(title="另一个故障")
        self.mode = "before_commit"
        self.assertEqual((await self.change(key, "confirm")).json()["status"], "unknown")
        self.assertEqual(self.count(), 1)
        self.mode = None
        result = (await self.change(key, "retry")).json()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.bodies[-1]["operationId"], self.bodies[-2]["operationId"])
        self.assertNotEqual(self.bodies[-1]["requestId"], self.bodies[-2]["requestId"])

    async def test_it_idempotency_is_atomic_and_rejects_changed_parameters(self):
        arguments = ("http://avatar\nsvc_it-helpdesk", "marco", "create_ticket", "op_simulated")
        tasks = [asyncio.to_thread(self.it_store.create_ticket, *arguments, self.params) for _ in range(2)]
        first, second = await asyncio.gather(*tasks)
        self.assertEqual(first, second)
        self.assertEqual(self.count(), 1)
        with self.assertRaises(IdempotencyConflict):
            self.it_store.create_ticket(*arguments, {**self.params, "title": "changed"})

    async def test_unknown_is_preserved_during_status_failure_and_contract_change(self):
        key = await self.propose()
        self.mode = "lost"
        self.assertEqual((await self.change(key, "confirm")).json()["status"], "unknown")
        for mode in ("status_timeout", "status_bad", "changed"):
            self.mode = mode
            result = (await self.change(key, "retry")).json()
            self.assertEqual(result["status"], "unknown")
        self.assertEqual((await self.change(key, "reconcile")).json()["status"], "unknown")
        self.assertEqual(self.calls.count("/avatar"), 1)
        self.mode = None
        self.assertEqual((await self.change(key, "reconcile")).json()["status"], "succeeded")
        self.assertEqual(self.count(), 1)

    async def test_replay_window_limits_writes_but_status_can_still_be_checked(self):
        key = await self.propose()
        self.mode = "before_commit"
        await self.change(key, "confirm")
        with self.store.open() as db:
            db.execute("UPDATE operations SET replay_until=? WHERE id=?", (now()-1, key))
        self.mode = None
        item = (await self.change(key, "retry")).json()
        self.assertEqual(item["status"], "unknown")
        self.assertEqual(item["availableActions"], ["reconcile"])
        self.assertEqual(self.calls.count("/avatar"), 1)
        self.assertEqual(self.calls.count("/operations/status"), 1)
        self.assertEqual(self.count(), 0)
