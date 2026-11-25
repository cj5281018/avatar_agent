"""Department plugin catalog and action contract for the avatar2 gateway.

A department is a plugin that owns actions. Each action publishes exactly one
external tool name, declares who triggers it and how it is approved, and carries
an executable handler. The gateway never grows a branch per department: both the
MCP entry and the internal tool entry dispatch through the same catalog.

Registration is atomic and strict. A duplicate department, a duplicate action
inside a department, a duplicate tool name, a missing handler, or an unknown
approval policy makes ``build_catalog`` raise before anything is exposed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

ApprovalPolicy = Literal["read", "draft", "write"]
POLICIES: set[str] = {"read", "draft", "write"}


class CatalogError(ValueError):
    """Registration would be ambiguous or unsafe."""


class ActionError(Exception):
    """A structured, user-safe failure raised by an action handler."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class ActionContext:
    """Everything a handler may use; identity is never taken from arguments."""

    bridge: Any
    store: Any
    user_id: str
    login_key: str
    chat_id: str | None = None
    employee: dict[str, Any] | None = None


Handler = Callable[[ActionContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PluginAction:
    id: str
    tool: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    # read: no side effects; draft: creates a pending proposal only;
    # write: changes business state and is never exposed without human action.
    policy: ApprovalPolicy
    summary_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class DepartmentPlugin:
    id: str
    display_name: str
    version: str
    actions: tuple[PluginAction, ...]
    web_entry: str | None = None


@dataclass
class Catalog:
    plugins: dict[str, DepartmentPlugin] = field(default_factory=dict)
    _by_tool: dict[str, tuple[DepartmentPlugin, PluginAction]] = field(default_factory=dict)

    def add(self, plugin: DepartmentPlugin) -> None:
        if not plugin.id or plugin.id in self.plugins:
            raise CatalogError(f"duplicate department id: {plugin.id!r}")
        if not plugin.actions:
            raise CatalogError(f"department {plugin.id!r} has no actions")
        seen_actions: set[str] = set()
        staged: dict[str, tuple[DepartmentPlugin, PluginAction]] = {}
        for action in plugin.actions:
            if not action.id or action.id in seen_actions:
                raise CatalogError(f"duplicate action {action.id!r} in department {plugin.id!r}")
            seen_actions.add(action.id)
            if action.tool in self._by_tool or action.tool in staged:
                owner = (self._by_tool.get(action.tool) or staged[action.tool])[0].id
                raise CatalogError(f"tool {action.tool!r} already owned by department {owner!r}")
            if action.policy not in POLICIES:
                raise CatalogError(f"unknown approval policy {action.policy!r} for {action.tool!r}")
            if not callable(action.handler):
                raise CatalogError(f"action {action.tool!r} has no executable handler")
            staged[action.tool] = (plugin, action)
        # Commit only after the whole plugin validated.
        self._by_tool.update(staged)
        self.plugins[plugin.id] = plugin

    def route(self, tool_name: str) -> tuple[DepartmentPlugin, PluginAction] | None:
        return self._by_tool.get(tool_name)

    def protocol_tools(self) -> list[dict[str, Any]]:
        """Standards-shaped tools for MCP clients; no vendor metadata."""
        return [
            {
                "name": action.tool,
                "description": action.description,
                "inputSchema": action.input_schema,
                "annotations": {"readOnlyHint": action.policy == "read"},
            }
            for _, action in self._by_tool.values()
        ]

    def metadata(self) -> dict[str, dict[str, Any]]:
        """Trace-only metadata, served separately from the tool protocol."""
        return {
            action.tool: {
                "department": plugin.id,
                "departmentName": plugin.display_name,
                "pluginVersion": plugin.version,
                "action": action.id,
                "policy": action.policy,
                "summaryFields": list(action.summary_fields),
                "webEntry": plugin.web_entry,
            }
            for plugin, action in self._by_tool.values()
        }

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": plugin.id,
                "displayName": plugin.display_name,
                "version": plugin.version,
                "webEntry": plugin.web_entry,
                "actions": [
                    {
                        "id": action.id,
                        "tool": action.tool,
                        "policy": action.policy,
                    }
                    for action in plugin.actions
                ],
            }
            for plugin in self.plugins.values()
        ]


def build_catalog(plugins: list[DepartmentPlugin]) -> Catalog:
    catalog = Catalog()
    for plugin in plugins:
        catalog.add(plugin)
    return catalog
