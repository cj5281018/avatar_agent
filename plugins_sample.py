"""Sample department plugin added for the phase-1 close-out.

It exists only to prove that a brand-new department can be discovered, called and
fail safely through the shared gateway, with no change to the dispatcher, the AI
tool handler or the workbench. Registration lives in ``gateway._catalog`` (two
lines); this file is the whole plugin implementation.
"""

from __future__ import annotations

from plugins import ActionError, ActionContext, DepartmentPlugin, PluginAction


async def describe(ctx: ActionContext, arguments: dict) -> dict:
    return {
        "type": "sample_note",
        "department": "sample",
        "action": "describe",
        "pluginVersion": "0.1.0",
        "note": "sample plugin read-only result",
    }


async def explode(ctx: ActionContext, arguments: dict) -> dict:
    raise ActionError("tool_error", "示例插件执行失败")


SAMPLE_PLUGIN = DepartmentPlugin(
    id="sample",
    display_name="示例部门",
    version="0.1.0",
    actions=(
        PluginAction(
            id="describe",
            tool="sample_describe",
            description="仅用于接入验证的只读示例动作",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=describe,
            policy="read",
        ),
        PluginAction(
            id="explode",
            tool="sample_explode",
            description="仅用于故障隔离验证的只读动作，执行时必定失败",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=explode,
            policy="read",
        ),
    ),
)
