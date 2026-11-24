import asyncio
import json
import os
import tempfile
import unittest
from contextlib import AsyncExitStack, ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from avatar_service.app import create_app
from avatar_service.db import Store, digest, now
from avatar_service.hr_bridge import HrBridge
from avatar_service.services import MAX_RESPONSE, ServiceBroker
from avatar_service.stream import Turn

read_response = ServiceBroker.read_response


class PluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cleanup = ExitStack()
        self.addCleanup(cleanup.close)
        async_cleanup = AsyncExitStack()
        self.addAsyncCleanup(async_cleanup.aclose)
        directory = cleanup.enter_context(tempfile.TemporaryDirectory())
        manifest = Path(directory, "plugins.json")
        self.keys = {"it-helpdesk": "it-key-" + "1" * 32, "ops-catalog": "ops-key-" + "2" * 32}
        actions = {
            "get_ticket": {"kind": "read", "description": "Query by ticketId", "roles": ["admin", "member"]},
            "queue_summary": {"kind": "read", "description": "Query queue", "roles": ["admin"]},
        }
        env = {"PY_AVATAR_SERVICES_FILE": str(manifest), "PY_AVATAR_HERMES_BIN": ""}
        rows = []
        for slug, key in self.keys.items():
            name = "PY_AVATAR_PLUGIN_" + slug.upper().replace("-", "_") + "_KEY"
            env[name] = key
            rows.append({"slug": slug, "name": slug, "description": "test service", "baseUrl": "http://" + slug,
                         "serviceKeyEnv": name, "actions": actions})
        manifest.write_text(json.dumps(rows), encoding="utf-8")
        cleanup.enter_context(patch.dict(os.environ, env))
        self.store = Store(directory)
        self.store.seed_demo()
        with self.store.open() as db:
            self.owner = db.execute("SELECT id FROM users WHERE handle='marco'").fetchone()["id"]
            admin = db.execute("SELECT id FROM users WHERE handle='admin'").fetchone()["id"]
            db.executemany("INSERT INTO logins VALUES (?,?,?)",
                           [(digest("admin"), admin, now() + 60000), (digest("member"), self.owner, now() + 60000)])
        self.app = create_app(directory)
        await async_cleanup.enter_async_context(self.app.router.lifespan_context(self.app))
        self.client = await async_cleanup.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://avatar"))
        self.turn = Turn("session", self.owner, digest("member"))
        self.state = {"turn": self.turn, "calls": 0}
        cleanup.enter_context(patch.object(ServiceBroker, "caller", self.bind(self.state)))
        self.completed = []
        self.held = None
        cleanup.enter_context(patch.object(ServiceBroker, "read_response", self.upstream))

    @staticmethod
    def bind(state):
        caller = ServiceBroker.caller

        def bound(broker, body):
            broker.turns["cap"] = state
            return caller(broker, body)

        return bound

    @property
    def upstream(self):
        async def response(broker, service, path, body):
            if path == "/setup":
                reply = await self.client.post("/internal/services/servicecheck",
                                               json=body["credentials"])
                assert reply.json()["service"]["id"] == body["service"]["id"]
                return {"ok": True, "protocolVersion": body["protocolVersion"],
                        "invoke": {"transport": "http", "target": service.origin + "/avatar"}}
            other = "ops-catalog" if service.slug == "it-helpdesk" else "it-helpdesk"
            credentials = {"serviceKey": service.key, "userKey": body["userKey"]}
            valid = await self.client.post("/internal/services/identitycheck", json=credentials)
            assert valid.json()["user"]["id"] == self.owner
            invalid = await self.client.post("/internal/services/identitycheck",
                                             json={**credentials, "serviceKey": self.keys[other]})
            assert invalid.status_code == 401
            self.completed.append(credentials)
            if self.held:
                await self.held.wait()
            return {"ok": True, "data": {"ticketId": "IT-101", "echo": [body["userKey"], service.key]}}

        return response

    async def pair(self, slug, actor="admin"):
        return await self.client.post(f"/v1/admin/plugins/{slug}/setup", json={"setupCode": "setup-code"},
                                      headers={"Authorization": f"Bearer {actor}"})

    async def tool(self, **body):
        return await self.client.post("/internal/services/tool", json={"capability": "cap", **body})

    async def test_discovery_identity_audit_and_role_boundaries(self):
        self.assertEqual((await self.pair("it-helpdesk", "member")).status_code, 403)
        self.assertEqual((await self.tool(operation="list")).json(), {"services": []})
        for slug in self.keys:
            self.assertEqual((await self.pair(slug)).status_code, 200)
        services = (await self.tool(operation="list")).json()["services"]
        self.assertEqual({s["slug"] for s in services}, set(self.keys))
        self.assertTrue(all(set(s["actions"]) == {"get_ticket"} for s in services))
        self.assertEqual((await self.tool(service="it-helpdesk", action="queue_summary")).status_code, 403)
        response = await self.tool(service="it-helpdesk", action="get_ticket", input={"ticketId": "IT-101"})
        result = response.json()
        self.assertEqual(result["data"], {"ticketId": "IT-101", "echo": ["[已隐藏]", "[已隐藏]"]})
        self.assertEqual((await self.client.post("/internal/services/identitycheck",
                                                json=self.completed[0])).status_code, 401)
        params = {"turnId": self.turn.turn_id}
        self.assertEqual((await self.client.get("/v1/admin/plugin-audit", params=params,
                                               headers={"Authorization": "Bearer member"})).status_code, 403)
        entries = (await self.client.get("/v1/admin/plugin-audit", params=params,
                                         headers={"Authorization": "Bearer admin"})).json()
        self.assertEqual([(r["request_id"], r["action"], r["outcome"]) for r in entries],
                         [(result["requestId"], "get_ticket", "success")])
        self.assertNotIn("IT-101", json.dumps(entries))
        self.assertNotIn(self.completed[0]["userKey"], json.dumps(entries))
        self.assertEqual((await self.tool(capability="wrong", operation="list")).status_code, 401)
        self.turn.stop.set()
        self.assertEqual((await self.tool(operation="list")).status_code, 401)

    async def test_timeout_is_not_retried_and_releases_slot(self):
        self.assertEqual((await self.pair("it-helpdesk")).status_code, 200)
        self.held = asyncio.Event()
        with patch("avatar_service.services.REQUEST_DEADLINE", 0.02):
            response = await self.tool(service="it-helpdesk", action="get_ticket")
        self.assertEqual(response.status_code, 504)
        self.assertTrue(response.headers["X-Avatar-Request-Id"].startswith("srq_"))
        self.assertEqual(len(self.completed), 1)
        self.held.set()
        self.assertEqual((await self.tool(service="it-helpdesk", action="get_ticket")).status_code, 200)
        with self.store.open() as db:
            self.assertEqual([tuple(row) for row in db.execute("SELECT outcome,status FROM avatar_service_audit ORDER BY rowid")],
                             [("unknown", 504), ("success", 200)])

    async def test_concurrency_and_turn_budget(self):
        self.assertEqual((await self.pair("it-helpdesk")).status_code, 200)
        self.held = asyncio.Event()
        with patch("avatar_service.services.MAX_CALLS_PER_SERVICE", 1), patch("avatar_service.services.MAX_CALLS_PER_TURN", 2):
            first = asyncio.create_task(self.tool(service="it-helpdesk", action="get_ticket"))
            try:
                async def wait_for_first_call():
                    while not self.completed:
                        await asyncio.sleep(0)
                await asyncio.wait_for(wait_for_first_call(), timeout=2)
                self.assertEqual((await self.tool(service="it-helpdesk", action="get_ticket")).status_code, 429)
            finally:
                self.held.set()
                reply = await first
            self.assertEqual(reply.status_code, 200)
            self.assertEqual((await self.tool(service="it-helpdesk", action="get_ticket")).status_code, 200)
            self.assertEqual((await self.tool(service="it-helpdesk", action="get_ticket")).status_code, 429)

    async def test_http_transport_enforces_response_limit_and_no_redirects(self):
        broker = ServiceBroker(HrBridge())
        service = SimpleNamespace(origin="http://service")
        real_client = httpx.AsyncClient
        requests = []

        def upstream(request):
            requests.append(str(request.url))
            if request.url.path == "/redirect":
                return httpx.Response(307, headers={"Location": "http://other/avatar"})
            return httpx.Response(200, content=b"x" * (MAX_RESPONSE + 1))

        with patch("avatar_service.services.httpx.AsyncClient",
                   side_effect=lambda **kw: real_client(transport=httpx.MockTransport(upstream), **kw)):
            for path in ("/redirect", "/avatar"):
                with self.assertRaises(HTTPException) as error:
                    await read_response(broker, service, path, {"userKey": "test"})
                self.assertEqual(error.exception.status_code, 502)
        self.assertEqual(requests, ["http://service/redirect", "http://service/avatar"])
