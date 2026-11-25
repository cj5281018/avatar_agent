"""Sample department plugin added for the phase-1 close-out.

It exists only to prove that a brand-new department can be discovered, called,
rendered by the generic card and fail safely through the shared gateway, with no
change to the dispatcher, the AI tool handler or the workbench. Registration
lives in ``gateway._catalog`` (two lines); this file is the whole plugin.
"""

from __future__ import annotations

import secrets

from plugins import ActionError, ActionContext, DepartmentPlugin, PluginAction


async def ping(ctx: ActionContext, arguments: dict) -> dict:
    return {
        "type": "sample_note",
        "department": "sample",
        "action": "ping",
        "pluginVersion": "0.1.0",
        "note": "sample plugin read-only result",
    }


async def prepare(ctx: ActionContext, arguments: dict) -> dict:
    operation = "note_" + secrets.token_urlsafe(12).replace("-", "_")
    if ctx.record:
        ctx.record(operation, "sample", "prepare", ctx.user_id, ctx.chat_id)
    return {
        "type": "sample_preview",
        "operationId": operation,
        "department": "sample",
        "action": "prepare",
        "pluginVersion": "0.1.0",
    }


async def describe_adapter(ctx: ActionContext, operation_id: str, chat_id: str) -> dict:
    return {
        "department": "sample",
        "departmentName": "示例部门",
        "action": "prepare",
        "pluginVersion": "0.1.0",
        "operationId": operation_id,
        "title": "示例动作",
        "label": "示例部门动作卡",
        "rows": [
            {"label": "部门", "value": "sample"},
            {"label": "动作", "value": "prepare"},
            {"label": "说明", "value": "只读夹具，未写业务"},
        ],
        "allowed": [],
    }


async def explode(ctx: ActionContext, arguments: dict) -> dict:
    raise ActionError("tool_error", "示例插件执行失败")


SAMPLE_PLUGIN = DepartmentPlugin(
    id="sample",
    display_name="示例部门",
    version="0.1.0",
    actions=(
        PluginAction(
            id="ping",
            tool="sample_describe",
            description="仅用于接入验证的只读示例动作",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=ping,
            policy="read",
        ),
        PluginAction(
            id="prepare",
            tool="sample_prepare",
            description="创建受控示例操作，供通用卡描述验证",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=prepare,
            policy="draft",
            describe=describe_adapter,
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
