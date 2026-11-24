import asyncio
import json
import os
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from avatar_service.app import create_app
from avatar_service.db import Store as AvatarStore, digest, now
from avatar_contract import manifest_digest
from avatar_service.stream import Turn
from avatar_service.services import ServiceBroker
from it_helpdesk_service.actions import ACTIONS, Action, GetResult, GetTicket, get_ticket, manifest
from it_helpdesk_service.app import create_app as create_it
from it_helpdesk_service.store import Store, set_setting


class ItFixture(unittest.IsolatedAsyncioTestCase):
    allowed_actions = ["get_ticket", "list_my_tickets", "alias_lookup"]
    async def asyncSetUp(self):
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.stack.aclose)
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.actions = dict(ACTIONS)
        self.actions["alias_lookup"] = Action("别名查询", "额外注册测试", GetTicket, GetResult, get_ticket, ())
        self.desired = manifest(self.actions)
        config = root / "plugins.json"
        config.write_text(json.dumps({"version": 1, "services": [{
            "slug": "it-helpdesk", "baseUrl": "http://it", "serviceKeyEnv": "PY_AVATAR_PLUGIN_IT_KEY",
            "enabled": True, "allowedActions": self.allowed_actions,
            "contractVersion": self.desired["contractVersion"], "manifestSha256": manifest_digest(self.desired),
        }]}))
        self.stack.enter_context(patch.dict(os.environ, {
            "IT_AVATAR_BASE_URL": "http://avatar", "IT_PUBLIC_URL": "http://it",
            "PY_AVATAR_PUBLIC_URL": "http://avatar", "PY_AVATAR_HERMES_BIN": "",
            "PY_AVATAR_SERVICES_FILE": str(config), "PY_AVATAR_PLUGIN_IT_KEY": "x" * 32,
        }))
        store = AvatarStore(root / "avatar")
        store.seed_demo()
        with store.open() as db:
            owner = db.execute("SELECT id FROM users WHERE handle='marco'").fetchone()[0]
            db.execute("INSERT INTO logins SELECT ?,id,? FROM users WHERE handle='marco'", (digest("member"), now()+60000))
            db.execute("INSERT INTO logins SELECT ?,id,? FROM users WHERE handle='admin'", (digest("admin"), now()+60000))
        with Store(root / "it").connection(write=True) as db:
            set_setting(db, "it.code", Store.seal("test-code"))
        app = create_app(root / "avatar")
        await self.stack.enter_async_context(app.router.lifespan_context(app))
        self.avatar = await self.stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://avatar"))
        it = create_it(root / "it", actions=self.actions)
        await self.stack.enter_async_context(it.router.lifespan_context(it))
        self.it = await self.stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=it), base_url="http://it"))
        with store.open() as db:
            db.execute("INSERT INTO sessions VALUES ('fixture',?,'test',0,0,0,1,1)", (owner,))
        self.store = store
        self.it_store = Store(root / "it")
        self.turn = Turn("fixture", owner, digest("member"))
        self.calls = []
        self.bodies = []
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.mode = None
        original = ServiceBroker.caller
        loop = asyncio.get_running_loop()
        test = self

        def caller(broker, body):
            test.broker = broker
            if broker.turns.get("cap", {}).get("id") != test.turn.turn_id:
                broker.turns["cap"] = {"turn": test.turn, "calls": 0, "id": test.turn.turn_id}
            return original(broker, body)

        async def upstream(broker, service, route, body):
            from fastapi import HTTPException
            test.calls.append(route)
            if route == "/operations/status" and test.mode == "status_timeout":
                raise HTTPException(504, "simulated status timeout")
            if route == "/avatar" and body.get("operationId"):
                test.bodies.append(dict(body))
                if test.mode == "before_commit":
                    from fastapi import HTTPException
                    raise HTTPException(504, "simulated timeout before commit")
            if test.mode == "timeout" and route == "/avatar":
                await asyncio.sleep(1)
            if route == "/manifest":
                response = await test.it.get(route, headers={"Authorization": "Bearer " + service.key})
            else:
                if test.mode == "changed":
                    body = dict(body, manifestSha256="old")
                response = await test.it.post(route, json=body)
            if response.is_error:
                raise HTTPException(response.status_code, "服务拒绝请求",
                                    headers={"X-Avatar-Error-Code": response.json()["code"]})
            result = response.json()
            if route == "/avatar" and body.get("operationId"):
                if test.mode == "hold":
                    test.reached.set()
                    await test.release.wait()
                if test.mode == "lost":
                    raise HTTPException(504, "simulated timeout after commit")
            if route == "/operations/status" and test.mode == "status_bad":
                result.update(state="succeeded", data={"wrong": True})
            if route == "/manifest" and test.mode == "changed":
                result["manifest"]["description"] = "已升级"
            if route == "/avatar" and test.mode == "bad_output":
                result["data"] = {"ticket": {"unexpected": True}}
            return result

        original_client = httpx.Client

        class Callback:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, url, json):
                return asyncio.run_coroutine_threadsafe(test.avatar.post(url, json=json), loop).result(5)

        def client(**options):
            return Callback() if options.get("timeout") == 5 else original_client(**options)

        self.stack.enter_context(patch.object(ServiceBroker, "caller", caller))
        self.stack.enter_context(patch.object(ServiceBroker, "read_response", upstream))
        self.stack.enter_context(patch("it_helpdesk_service.app.httpx.Client", client))
        self.admin = {"Authorization": "Bearer admin"}
        paired = await self.avatar.post("/v1/admin/plugins/it-helpdesk/setup",
                                        json={"setupCode": "test-code"}, headers=self.admin)
        self.assertEqual(paired.status_code, 200, paired.text)

    async def tool(self, **body):
        return await self.avatar.post("/internal/services/tool", json={"capability": "cap", "service": "it-helpdesk",
                                                                  "mcpTraceId": "mcp_" + "a"*32, **body})


class ItContractTest(ItFixture):
    async def test_health_remains_read_only_during_a_write(self):
        with self.it_store.connection(write=True):
            response = await self.it.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "paired": True})

    async def test_discovery_fields_ownership_and_extra_action(self):
        catalog = (await self.tool(operation="list", query="别名")).json()["services"]
        self.assertEqual(set(catalog[0]["actions"]), {"alias_lookup"})
        self.assertEqual((await self.tool(action="unregistered")).status_code, 403)
        for args in ({}, {"ticket_id": "IT-101"}, {"ticketId": "IT-101", "employeeId": "bob"}):
            before = len(self.calls)
            result = await self.tool(action="get_ticket", input=args)
            self.assertEqual((result.status_code, result.json()["code"]), (400, "INVALID_INPUT"))
            self.assertEqual(len(self.calls), before)
        allowed = await self.tool(action="alias_lookup", input={"ticketId": "IT-101"})
        self.assertEqual(allowed.json()["data"]["ticket"]["status"], "resolved")
        for key in ("IT-202", "IT-999"):
            reply = await self.tool(action="get_ticket", input={"ticketId": key})
            self.assertEqual((reply.status_code, reply.json()["code"]), (404, "NOT_FOUND"))
        audit = (await self.avatar.get("/v1/admin/plugin-audit",
                                    params={"turnId": self.turn.turn_id}, headers=self.admin)).json()
        self.assertEqual([r["code"] for r in audit], ["INVALID_INPUT"]*3 + [None, "NOT_FOUND", "NOT_FOUND"])
        self.assertTrue(all(r["mcp_id"] == "mcp_"+"a"*32 for r in audit))
        for args in ({"statusGroup": "open"}, {"limit": 21}, {"limit": True}):
            before = len(self.calls)
            self.assertEqual((await self.tool(action="list_my_tickets", input=args)).json()["code"], "INVALID_INPUT")
            self.assertEqual(len(self.calls), before)
        result = await self.tool(action="list_my_tickets", input={"statusGroup": "active"})
        self.assertEqual([t["ticketId"] for t in result.json()["data"]["tickets"]], ["IT-103"])

    async def test_timeout_output_drift_and_recovery(self):
        for mode, code in (("timeout", "UPSTREAM_TIMEOUT"), ("bad_output", "OUTPUT_SCHEMA_MISMATCH"),
                           ("changed", "CONTRACT_MISMATCH")):
            self.mode = mode
            self.turn.turn_id = "fixture-" + mode
            with patch("avatar_service.services.REQUEST_DEADLINE", 0.15 if mode == "timeout" else 5):
                response = await self.tool(action="get_ticket", input={"ticketId": "IT-101"})
            self.assertEqual(response.json()["code"], code, response.text)
        self.assertEqual((await self.tool(operation="list")).json(), {"services": []})
        self.mode = None
        self.assertEqual((await self.tool(action="get_ticket", input={"ticketId": "IT-101"})).status_code, 200)

    def test_list_uses_ownership_defaults_and_status(self):
        action = ACTIONS["list_my_tickets"]
        self.assertEqual([t.ticketId for t in action.handler("marco", action.input(), self.it_store).tickets], ["IT-103", "IT-101"])
        result = action.handler("marco", action.input(statusGroup="active", limit=1), self.it_store)
        self.assertEqual([t.ticketId for t in result.tickets], ["IT-103"])
        self.assertFalse(result.hasMore)
