"""Department plugin catalog for the avatar2 action gateway.

A department is a plugin that owns one or more actions. Every action publishes
one external tool name; the gateway routes a tool call through an explicit
``tool -> (department, action)`` mapping instead of growing an if/else per
department. Registration fails loudly on duplicate department ids, duplicate
action ids inside a department, or duplicate external tool names, so a second
department can never silently shadow the first.

The catalog is metadata only: handlers live with the gateway (HR) or with the
department service. Adding a department means adding a ``DepartmentPlugin``
entry and its handler binding - no gateway branch work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class CatalogError(ValueError):
    """Raised when plugin registration would be ambiguous."""


@dataclass(frozen=True)
class PluginAction:
    id: str
    tool: str
    description: str
    input_schema: dict[str, Any]
    handler: str
    read_only: bool = True
    # Fields a generic action card may show when summarising a result.
    summary_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class DepartmentPlugin:
    id: str
    display_name: str
    version: str
    actions: tuple[PluginAction, ...]
    # Optional human web entry (department-owned page) reachable from the
    # workbench; not a sidebar menu.
    web_entry: str | None = None


@dataclass
class Catalog:
    plugins: dict[str, DepartmentPlugin] = field(default_factory=dict)
    _by_tool: dict[str, tuple[DepartmentPlugin, PluginAction]] = field(default_factory=dict)

    def add(self, plugin: DepartmentPlugin) -> None:
        if not plugin.id or plugin.id in self.plugins:
            raise CatalogError(f"duplicate department id: {plugin.id!r}")
        seen_actions: set[str] = set()
        for action in plugin.actions:
            if not action.id or action.id in seen_actions:
                raise CatalogError(f"duplicate action {action.id!r} in department {plugin.id!r}")
            seen_actions.add(action.id)
            if action.tool in self._by_tool:
                owner = self._by_tool[action.tool][0].id
                raise CatalogError(
                    f"tool {action.tool!r} already owned by department {owner!r}"
                )
            self._by_tool[action.tool] = (plugin, action)
        self.plugins[plugin.id] = plugin

    def route(self, tool_name: str) -> tuple[DepartmentPlugin, PluginAction] | None:
        return self._by_tool.get(tool_name)

    def tools(self) -> list[dict[str, Any]]:
        """Tool definitions for tools/list, with department metadata attached."""
        result: list[dict[str, Any]] = []
        for plugin, action in self._by_tool.values():
            result.append(
                {
                    "name": action.tool,
                    "description": action.description,
                    "inputSchema": action.input_schema,
                    "annotations": {"readOnlyHint": action.read_only},
                    "x-avatar2": {
                        "department": plugin.id,
                        "departmentName": plugin.display_name,
                        "pluginVersion": plugin.version,
                        "action": action.id,
                        "handler": action.handler,
                        "readOnly": action.read_only,
                        "summaryFields": list(action.summary_fields),
                        "webEntry": plugin.web_entry,
                    },
                }
            )
        return result

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": plugin.id,
                "displayName": plugin.display_name,
                "version": plugin.version,
                "webEntry": plugin.web_entry,
                "actions": [
                    {"id": action.id, "tool": action.tool, "readOnly": action.read_only}
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
