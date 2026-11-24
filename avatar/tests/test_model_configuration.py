import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import yaml

from avatar_service import mcp_bridge
from avatar_service.db import Store, append
from avatar_service.hermes import Hermes
from avatar_service.hr_bridge import HrBridge
from avatar_service.receiver import Receiver
from avatar_service.stream import Runtime, Turn
from test_leave_confirmation import ConfirmationFixture


class ModelConfigurationTest(ConfirmationFixture):
    def test_legacy_profile_and_invalid_internal_settings(self):
        store = Store(self.avatar_dir)
        with patch.dict(os.environ, {"PY_AVATAR_MODEL_PROVIDER": "", "PY_AVATAR_MODEL_BASE_URL": "",
                                     "PY_AVATAR_HERMES_HOME_ROOT": str(store.path.parent / "hermes")}):
            hermes = Hermes(store, None, None)
        home, _, _ = hermes.environment(self.owner)
        config = home / "config.yaml"
        self.assertEqual(yaml.safe_load(config.read_text(encoding="utf-8"))["model"]["provider"], "deepseek")
        original = "model: editor-model\nstreaming:\n  enabled: true\n"
        config.write_text(original, encoding="utf-8")
        hermes.environment(self.owner)
        self.assertEqual(config.read_text(encoding="utf-8"), original)
        for url in ("", "ftp://model.invalid/v1", "https://user@model.invalid/v1", "https://model.invalid/v1?token=x"):
            with (self.subTest(url=url),
                  patch.dict(os.environ, {"PY_AVATAR_MODEL_PROVIDER": "custom",
                                          "PY_AVATAR_MODEL": "internal", "PY_AVATAR_MODEL_BASE_URL": url}),
                  self.assertRaisesRegex(ValueError, "PY_AVATAR_MODEL_BASE_URL")):
                Hermes(store, None, None)

    async def test_switch_existing_conversation_and_deliver_hr_notification(self):
        store = Store(self.avatar_dir)
        with store.open() as db:
            db.execute("""CREATE TABLE acp_sessions (
                session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, acp_id TEXT NOT NULL
            )""")
            db.execute("INSERT INTO acp_sessions VALUES ('original','legacy-cloud-session')")
            admin_owner = db.execute("SELECT id FROM users WHERE handle='admin'").fetchone()[0]
            append(db, "original", "user", "existing desktop message")
        for owner in (self.owner, "r_" + admin_owner):
            home = store.path.parent / "hermes" / owner
            home.mkdir(parents=True)
            (home / "config.yaml").write_text("model: {default: old-cloud, provider: deepseek}\n"
                                               "streaming: {enabled: true}\n", encoding="utf-8")

        calls, sessions = [], []
        bridges = []
        broker = SimpleNamespace(turns={})
        start_bridge = HrBridge.start

        def capture_bridge(bridge, store, publish):
            start_bridge(bridge, store, publish)
            bridges.append(bridge)

        bridge_patch = patch.object(HrBridge, "start", capture_bridge)
        bridge_patch.start()
        self.addCleanup(bridge_patch.stop)

        def respond(request):
            payload = json.loads(request.content)
            calls.append((str(request.url), payload["model"], request.headers["authorization"]))
            if payload.get("tools"):
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "tool-test",
                    "type": "function", "function": {"name": "call_service", "arguments": json.dumps({
                        "service": "hr-leave-python", "action": "who_am_i", "input": {},
                    })}}]}}]})
            reply = "收到请假通知 leave_demo" if "leave_demo" in payload["messages"][0]["content"] else "你好"
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

        async with self.services() as (avatar, _hr):
            loop = asyncio.get_running_loop()

            def post_tool(_client, url, json):
                return asyncio.run_coroutine_threadsafe(avatar.post(url, json=json), loop).result(5)

            class FakeACP:
                def __init__(self, config, env):
                    self.config, self.env = config, env
                    self.tools = []

                async def request(self, method, params, **kwargs):
                    if method == "initialize":
                        return {}
                    if method in ("session/load", "session/new"):
                        self.tools = params["mcpServers"]
                        self.session = params.get("sessionId", f"mock-session-{len(sessions)}")
                        sessions.append((method, self.session))
                        return {"sessionId": self.session}
                    assert method == "session/prompt"
                    payload = {"model": self.config["default"], "messages": [{"role": "user",
                        "content": params["prompt"][0]["text"]}]}
                    if self.tools and "who_am_i" in params["prompt"][0]["text"]:
                        payload["tools"] = [{"type": "function", "function": {"name": "call_service"}}]
                    url = self.config.get("base_url", self.env["DEEPSEEK_BASE_URL"])
                    key = self.env[self.config.get("key_env", "DEEPSEEK_API_KEY")]
                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as model:
                        response = await model.post(url + "/chat/completions", json=payload,
                                                    headers={"Authorization": "Bearer " + key})
                    message = response.json()["choices"][0]["message"]
                    if message.get("tool_calls"):
                        function = message["tool_calls"][0]["function"]
                        tool_env = {item["name"]: item["value"] for item in self.tools[0]["env"]}
                        with (patch.dict(os.environ, tool_env), patch.object(mcp_bridge.httpx.Client, "post", post_tool)):
                            result = await asyncio.to_thread(mcp_bridge.handle, {"method": "tools/call", "params": {
                                "name": function["name"], "arguments": json.loads(function["arguments"]),
                            }})
                        assert not result["isError"], result
                        message["content"] = json.loads(result["content"][0]["text"])["data"]["identity"]["handle"]
                    self.update({"sessionId": self.session, "update": {"sessionUpdate": "agent_message_chunk",
                                                                      "content": {"text": message["content"]}}})
                    return {"stopReason": "end_turn"}

                async def close(self):
                    pass

            async def spawn(_hermes, home, work, env):
                config = yaml.safe_load((Path(env["HERMES_HOME"]) / "config.yaml").read_text(encoding="utf-8"))
                assert config["streaming"]["enabled"]
                return FakeACP(config["model"], env)

            cloud = {"PY_AVATAR_MODEL_PROVIDER": "deepseek", "PY_AVATAR_MODEL": "cloud-demo",
                     "PY_AVATAR_MODEL_BASE_URL": "", "DEEPSEEK_BASE_URL": "http://cloud.invalid/v1",
                     "DEEPSEEK_API_KEY": "demo-cloud-key", "PY_AVATAR_HERMES_HOME_ROOT": str(store.path.parent / "hermes")}
            internal = {**cloud, "PY_AVATAR_MODEL_PROVIDER": "custom", "PY_AVATAR_MODEL": "internal-demo",
                        "PY_AVATAR_MODEL_BASE_URL": "http://internal.invalid/v1/",
                        "AVATAR_INTERNAL_LLM_API_KEY": "demo-internal-key"}
            with patch.object(Hermes, "spawn", spawn):
                for settings, expected in ((cloud, "你好"), (internal, "marco"), (internal, "marco"), (cloud, "你好")):
                    with patch.dict(os.environ, settings):
                        hermes = Hermes(store, bridges[0], broker)
                        turn = Turn("original", self.owner, self.turn.login_key)
                        prompt = "who_am_i" if expected == "marco" else "你好"
                        await hermes.reply(turn, prompt, lambda chunk: setattr(turn, "text", turn.text + chunk))
                        self.assertEqual(turn.text, expected)
                with patch.dict(os.environ, internal):
                    receiver = Receiver(Runtime(store, 0, Hermes(store, bridges[0], broker)))
                    delivery = await receiver.deliver(self.turn, "leave_demo", "request", {
                        "handle": "admin", "title": "请假通知", "text": "请审批 leave_demo",
                    })
                    self.assertEqual(delivery["mode"], "receiver")

        self.assertEqual([method for method, _ in sessions],
                         ["session/new", "session/new", "session/load", "session/new", "session/new"])
        self.assertEqual(sessions[1][1], sessions[2][1])
        self.assertEqual(calls, [
            ("http://cloud.invalid/v1/chat/completions", "cloud-demo", "Bearer demo-cloud-key"),
            ("http://internal.invalid/v1/chat/completions", "internal-demo", "Bearer demo-internal-key"),
            ("http://internal.invalid/v1/chat/completions", "internal-demo", "Bearer demo-internal-key"),
            ("http://cloud.invalid/v1/chat/completions", "cloud-demo", "Bearer demo-cloud-key"),
            ("http://internal.invalid/v1/chat/completions", "internal-demo", "Bearer demo-internal-key"),
        ])
        with store.open() as db:
            self.assertEqual(db.execute("SELECT text FROM messages WHERE session_id='original'").fetchone()[0],
                             "existing desktop message")
            self.assertEqual(db.execute("SELECT mode FROM hr_deliveries").fetchone()[0], "receiver")
        for path in (store.path.parent / "hermes").glob("*/config.yaml"):
            self.assertNotIn("demo-internal-key", path.read_text(encoding="utf-8"))
