"""Ordinary chat integration with the private department tool gateway."""
import json
import logging
import os

import httpx

from tools.registry import ToolResult

logger = logging.getLogger(__name__)


class Avatar2ToolHandler:
    def __init__(self, endpoint, secret, user_id):
        self.endpoint = endpoint.rstrip("/")
        self.headers = {"Authorization": "Bearer " + secret}
        self.user_id = user_id
        self.tools = {}

    async def request(self, path, body):
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            return await client.post(
                self.endpoint + path, headers=self.headers,
                json={"user_id": self.user_id, **body},
            )

    @classmethod
    async def discover(cls, user_id):
        endpoint = os.getenv("AVATAR2_GATEWAY_URL")
        secret = os.getenv("AVATAR2_INTERNAL_TOKEN")
        if not (endpoint and secret and user_id):
            return None
        handler = cls(endpoint, secret, user_id)
        try:
            reply = await handler.request("/internal/tools/list", {})
            if reply.status_code == 403:
                return None
            reply.raise_for_status()
            for tool in reply.json()["tools"]:
                metadata = tool.get("x-avatar2") if isinstance(tool, dict) else None
                handler.tools[tool["name"]] = {
                    "name": tool["name"], "description": tool["description"],
                    "input_schema": tool["inputSchema"],
                    "trace": metadata if isinstance(metadata, dict) else {},
                }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            logger.warning("Avatar2 tool discovery failed")
            return None
        return handler

    def get_tools(self):
        return list(self.tools.values())

    def can_handle(self, name):
        return name in self.tools

    def trace_metadata(self, name):
        tool = self.tools.get(name) or {}
        meta = tool.get("trace") or {}
        allowed = {}
        for key in ("department", "departmentName", "action", "pluginVersion"):
            value = meta.get(key)
            if isinstance(value, str) and len(value) <= 80:
                allowed[key] = value
        return allowed

    def requires_approval(self, name):
        return name not in {"hr_list_my_requests", "hr_preview_leave"}

    async def execute(self, name, arguments, context):
        if context.user_id != self.user_id or not self.can_handle(name):
            return self.error("工具或员工身份不匹配")
        try:
            response = await self.request(
                "/internal/tools/call", {"name": name, "arguments": arguments, "chat_id": context.chat_id}
            )
            if response.status_code == 403:
                return self.error("员工绑定不可用，请检查绑定状态")
            response.raise_for_status()
            data = response.json()
            return ToolResult(content=[{"type": "text", "text": json.dumps(data, ensure_ascii=False)}])
        except (httpx.HTTPError, ValueError):
            return self.error("部门工具暂时不可用")

    @staticmethod
    def error(message):
        return ToolResult(content=[{"type": "text", "text": message}], is_error=True)

