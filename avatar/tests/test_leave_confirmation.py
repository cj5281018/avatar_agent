import asyncio
import os
import unittest
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from avatar_service.app import create_app
from avatar_service.db import Store as AvatarStore, digest, now
from avatar_service.hr_bridge import HrBridge
from avatar_service.stream import Turn
from hr_service.app import create_app as create_hr
from hr_service.db import Store as HrStore, set_setting
from hr_service.protocol import VERSION


class ConfirmationFixture(unittest.IsolatedAsyncioTestCase):
    hours = 60 * 60 * 1000
    fields = {"startDate": "2026-12-20", "endDate": "2026-12-21",
              "leaveType": "annual", "reason": "confirmation test"}
    root_path = "/v1/sessions/original/hr/leave-confirmations"
    marco = {"Authorization": "Bearer marco"}
    admin = {"Authorization": "Bearer admin"}

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.avatar_dir = root / "avatar"
        self.hr_dir = root / "hr"
        self.keys = []
        self.drop_response = False
        self.forward_reached = asyncio.Event()
        self.release_forward = asyncio.Event()
        self.hold_forward = False
        avatar = AvatarStore(self.avatar_dir)
        avatar.seed_demo()
        with avatar.open() as db:
            self.owner = db.execute("SELECT id FROM users WHERE handle='marco'").fetchone()[0]
            db.execute("INSERT INTO logins SELECT ?,id,? FROM users WHERE handle='marco'",
                       (digest("marco"), now() + self.hours))
            db.execute("INSERT INTO logins SELECT ?,id,? FROM users WHERE handle='admin'",
                       (digest("admin"), now() + self.hours))
            db.execute("INSERT INTO sessions VALUES ('original',?,'test',0,0,0,1,1)", (self.owner,))
        self.turn = Turn("original", self.owner, digest("marco"))
        hr = HrStore(self.hr_dir)
        with hr.connection(write=True) as db:
            set_setting(db, "service.key", hr.seal("test-key"))

    @asynccontextmanager
    async def services(self):
        with patch.dict(os.environ, {"PY_AVATAR_HERMES_BIN": "", "PY_AVATAR_SERVICES_FILE": ""}):
            avatar = create_app(self.avatar_dir)
            hr = create_hr(self.hr_dir)
            async with AsyncExitStack() as stack:
                original_start = HrBridge.start

                def start(bridge, store, publish):
                    original_start(bridge, store, publish)
                    bridge.turns["cap"] = self.turn
                    bridge.broker.turns["cap"] = {"turn": self.turn, "calls": 0}
                    with store.open() as db:
                        db.execute("INSERT OR IGNORE INTO hr_pairing VALUES ('svc_test',?,1)", (digest("test-key"),))

                stack.enter_context(patch.object(HrBridge, "start", start))
                await stack.enter_async_context(avatar.router.lifespan_context(avatar))
                await stack.enter_async_context(hr.router.lifespan_context(hr))
                client = await stack.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=avatar), base_url="http://avatar",
                ))
                hr_client = await stack.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=hr), base_url="http://hr",
                ))
                loop = asyncio.get_running_loop()

                def callback(path, body, **kwargs):
                    future = asyncio.run_coroutine_threadsafe(client.post(path, json=body), loop)
                    return future.result(5).json()

                async def upstream(bridge, path, body):
                    self.keys.append(body["requestId"])
                    if body["action"] == "review_leave":
                        body = {**body, "notificationSession": self.turn.session_id}
                    if self.hold_forward:
                        self.forward_reached.set()
                        await self.release_forward.wait()
                    response = await hr_client.post(path, json=body)
                    if response.is_error:
                        raise HTTPException(response.status_code, response.json()["message"])
                    if self.drop_response:
                        self.drop_response = False
                        raise HTTPException(502, "simulated lost HR response")
                    return response.json()

                with (patch.object(HrBridge, "request", upstream),
                      patch("hr_service.protocol.post_avatar", callback)):
                    yield client, hr_client

    async def propose(self, client, fields=None, action="request_leave"):
        return await client.post("/internal/hr/tool", json={"capability": "cap", "service": "hr-leave-python",
                                                            "action": action, "input": fields or self.fields})

    async def cards(self, client):
        response = await client.get("/v1/sessions/original", headers=self.marco)
        return response.json()["leaveConfirmations"]

    def counts(self):
        with HrStore(self.hr_dir).connection() as db:
            return db.execute("SELECT COUNT(*) FROM leave_requests").fetchone()[0]


class LeaveConfirmationTest(ConfirmationFixture):
    async def test_only_owner_can_confirm_and_concurrent_confirmations_share_a_leave(self):
        async with self.services() as (client, _hr):
            self.assertEqual((await self.propose(client, {**self.fields, "confirmed": "true"})).status_code, 400)
            self.assertEqual((await self.propose(client, {**self.fields, "startDate": "2026-02-30"})).status_code, 400)
            for _ in range(2):
                result = await self.propose(client)
                self.assertEqual(result.json()["data"]["status"], "pending")
            item, = await self.cards(client)
            url = f"{self.root_path}/{item['operationId']}/confirm"
            self.assertEqual(self.counts(), 0)
            self.assertEqual(self.keys, [])
            self.assertEqual((await client.post(url)).status_code, 401)
            self.assertEqual((await client.post(url, headers=self.admin)).status_code, 404)
            self.hold_forward = True
            first = asyncio.create_task(client.post(url, headers=self.marco, json={"reason": "modified"}))
            await asyncio.wait_for(self.forward_reached.wait(), 5)
            self.assertEqual((await self.cards(client))[0]["status"], "confirmed")
            second = asyncio.create_task(client.post(url, headers=self.marco))
            await asyncio.sleep(0)
            self.release_forward.set()
            replies = await asyncio.gather(first, second)
            self.assertEqual([r.status_code for r in replies], [200, 200])
            leave_id = replies[0].json()["leaveId"]
            self.assertEqual(replies[1].json()["leaveId"], leave_id)
            self.assertEqual(replies[0].json()["input"], self.fields)
            self.assertEqual((await client.post(url, headers=self.marco)).json()["leaveId"], leave_id)
            self.assertEqual(self.counts(), 1)
            self.assertEqual(set(self.keys), {item["operationId"]})

    async def test_restart_before_confirmation_and_after_hr_commit_lost_response(self):
        async with self.services() as (client, _hr):
            await self.propose(client)
            item, = await self.cards(client)
            operation = item["operationId"]
        self.assertEqual(self.counts(), 0)
        url = f"{self.root_path}/{operation}/confirm"
        async with self.services() as (client, _hr):
            self.assertEqual((await self.cards(client))[0], item)
            self.drop_response = True
            self.assertEqual((await client.post(url, headers=self.marco)).status_code, 502)
            self.assertEqual((await self.cards(client))[0]["status"], "confirmed")
            self.assertEqual(self.counts(), 1)
            with HrStore(self.hr_dir).connection(write=True) as db:
                original = db.execute("SELECT id FROM leave_requests WHERE request_key=?", (operation,)).fetchone()[0]
                # Cancel it after HR commits so the active-leave dedup rule cannot mask a changed key.
                db.execute("UPDATE leave_requests SET status='cancelled' WHERE id=?", (original,))
        async with self.services() as (client, _hr):
            self.assertEqual((await client.post(url, headers=self.marco)).json()["leaveId"], original)
            self.assertEqual(self.counts(), 1)
            self.assertEqual(self.keys, [operation, operation])

    async def test_expired_card_and_scoped_hr_user_key(self):
        async with self.services() as (client, hr):
            await self.propose(client)
            item, = await self.cards(client)
            with AvatarStore(self.avatar_dir).open() as db:
                db.execute("UPDATE leave_confirmations SET expires_at=0")
            expired = await client.post(f"{self.root_path}/{item['operationId']}/confirm", headers=self.marco)
            self.assertEqual(expired.status_code, 409)
            body = {"protocolVersion": VERSION, "requestId": item["operationId"], "action": "request_leave",
                    "input": self.fields, "userKey": "scoped-test"}
            # A key obtained for another action must not authorize a direct write to HR.
            other_grant = {"ok": True, "user": {"id": self.owner, "handle": "marco"},
                           "grant": {"action": "list_my_requests", "requestId": item["operationId"]}}
            with patch("hr_service.protocol.post_avatar", return_value=other_grant):
                self.assertEqual((await hr.post("/avatar", json=body)).status_code, 403)
            self.assertEqual(self.counts(), 0)


class ToolTraceTest(ConfirmationFixture):
    async def test_real_mcp_handler_reaches_avatar_and_hr(self):
        import json
        from avatar_service import mcp_bridge

        async with self.services() as (client, _hr):
            loop = asyncio.get_running_loop()

            class Transport:
                def __init__(self, **kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def post(self, url, json):
                    return asyncio.run_coroutine_threadsafe(client.post(url, json=json), loop).result(5)

            with (patch.object(mcp_bridge.httpx, "Client", Transport),
                  patch.dict(os.environ, {"AVATAR_BRIDGE_URL": "/internal/hr/tool", "AVATAR_BRIDGE_CAP": "cap"})):
                result = await asyncio.to_thread(mcp_bridge.handle, {
                    "method": "tools/call", "params": {"name": "call_service", "arguments": {
                        "service": "hr-leave-python", "action": "who_am_i", "input": {},
                    }},
                })
            assert not result["isError"]
            reply = json.loads(result["content"][0]["text"])
            self.assertEqual(reply["data"]["identity"]["handle"], "marco")
            entry, = self.turn.activity.values()
            self.assertTrue(entry["id"].startswith("mcp_"))
            self.assertIn("HR 已响应", entry["detail"])
            self.assertIn(reply["attemptId"], entry["detail"])
            self.assertIn(reply["requestId"], entry["detail"])

    async def test_dsml_text_never_dispatches_and_activity_survives_reload(self):
        from avatar_service.hermes import trace_acp, trace_dsml
        from avatar_service.stream import Runtime

        class FakeHermes:
            async def reply(self, turn, prompt, emit):
                emit("< | DSML")
                emit(" | invoke name=\"request_leave\">")
                trace_dsml(turn)
                trace_acp(turn, {"sessionUpdate": "agent_message_chunk", "toolCallId": "forged"})

        runtime = Runtime(AvatarStore(self.avatar_dir), 0, FakeHermes())
        await runtime.reply(self.turn, "synthetic text")
        self.assertEqual(self.counts(), 0)
        self.assertEqual(self.keys, [])
        async with self.services() as (client, _hr):
            response = await client.get("/v1/sessions/original", headers=self.marco)
            message, = response.json()["messages"]
            entry, = message["activity"]
            self.assertEqual(entry["id"], "dsml")
            self.assertEqual(entry["kind"], "notice")
