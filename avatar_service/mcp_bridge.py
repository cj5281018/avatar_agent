import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avatar_service.hr_bridge import ACTIONS as LEGACY_HR_ACTIONS

TOOL = {
    "name": "call_service",
    "description": (
        "调用已注册的部门服务。先用 list_services 获取可用服务和动作，按动作说明提供 input。"
        "HR 使用 hr，包含请假和招聘。写操作需在桌面确认卡提交。"
    ),
    "inputSchema": {"type": "object", "properties": {
        "service": {"type": "string", "description": "服务目录中的 slug"},
        "action": {"type": "string"},
        "input": {"type": "object"},
    }, "required": ["service", "action"], "additionalProperties": False},
}
LIST_TOOL = {
    "name": "list_services",
    "description": "列出当前用户可使用的部门服务及已配置的动作说明。",
    "inputSchema": {"type": "object", "properties": {
        "query": {"type": "string", "maxLength": 200, "description": "按业务主题可选搜索，省略时浏览目录"},
        "service": {"type": "string", "description": "可选的服务 slug"},
    }, "additionalProperties": False},
}


def call(args, listing=False):
    service, action = args.get("service"), args.get("action")
    use_hr = not listing and (service == "hr-leave-python" or
                              service == "hr" and isinstance(action, str) and action in LEGACY_HR_ACTIONS)
    endpoint = "AVATAR_BRIDGE_URL" if use_hr else "AVATAR_SERVICES_URL"
    body = {"capability": os.environ["AVATAR_BRIDGE_CAP"]}
    if not listing:
        trace_id = "mcp_" + uuid4().hex
        body["mcpTraceId"] = trace_id
        print(f"mcp.tools_call id={trace_id}", file=sys.stderr, flush=True)
    if listing:
        body["operation"] = "list"
        body.update({name: args[name] for name in ("query", "service") if name in args})
    else:
        body.update(service=args.get("service"), action=args.get("action"), input=args.get("input", {}))
    try:
        with httpx.Client(timeout=200, trust_env=False) as client:
            result = client.post(os.environ[endpoint], json=body)
        value = result.json()
        error = not result.is_success
        request_id = result.headers.get("X-Avatar-Request-Id")
        if error and request_id and isinstance(value, dict):
            value["requestId"] = request_id
    except (httpx.HTTPError, ValueError):
        value, error = {"message": "Avatar 工具连接失败"}, True
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": error}


HR_TOOLS = {
    "prepare_leave_request": {
        "name": "prepare_leave_request",
        "description": "准备请假确认卡片。收齐起止日期、假别和事由再调用；单日请假起止相同。缺失或含糊时追问，不猜测。点击卡片才提交。",
        "inputSchema": {"type": "object", "properties": {
            "startDate": {"type": "string", "format": "date", "description": "开始日期 YYYY-MM-DD"},
            "endDate": {"type": "string", "format": "date", "description": "结束日期 YYYY-MM-DD，包含当天"},
            "leaveType": {"type": "string", "enum": ["annual", "sick", "personal", "compensatory", "other"],
                          "description": "年假、病假、事假、调休、其他"},
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "用户提供的事由"},
        }, "required": ["startDate", "endDate", "leaveType", "reason"], "additionalProperties": False},
    },
    "prepare_leave_review": {
        "name": "prepare_leave_review",
        "description": "用已明确定位的正式请假单及明确决定准备审批卡片。指代不清时查询待审批单，仍有歧义就请用户选择。点击卡片才审批。",
        "inputSchema": {"type": "object", "properties": {
            "requestId": {"type": "string", "pattern": "^leave_[A-Za-z0-9_-]{1,100}$", "description": "正式请假单号"},
            "decision": {"type": "string", "enum": ["approve", "reject"], "description": "approve 同意，reject 拒绝"},
            "note": {"type": "string", "maxLength": 2000, "description": "用户提供的可选审批备注"},
        }, "required": ["requestId", "decision"], "additionalProperties": False},
    },
}
HR_ACTIONS = {"prepare_leave_request": "request_leave", "prepare_leave_review": "review_leave"}


def handle(message):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "avatar-python", "version": "0.1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [LIST_TOOL, TOOL, *HR_TOOLS.values()]}
    if method == "tools/call" and params.get("name") in HR_ACTIONS:
        name = params["name"]
        args = params.get("arguments", {})
        schema = HR_TOOLS[name]["inputSchema"]
        if not isinstance(args, dict) or set(schema["required"]) - args.keys() or args.keys() - schema["properties"].keys():
            fields = "、".join(schema["required"])
            return {"isError": True, "content": [{"type": "text", "text": f"请使用工具定义中的字段，必填：{fields}"}]}
        return call({"service": "hr-leave-python", "action": HR_ACTIONS[name], "input": args})
    if method == "tools/call":
        name = params.get("name")
        if name == "list_services":
            return call(params.get("arguments") or {}, listing=True)
        if name == "call_service":
            return call(params.get("arguments") or {})
    return None


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or "id" not in message:
                continue
            response = {"jsonrpc": "2.0", "id": message["id"]}
            result = handle(message)
            if result is None:
                response["error"] = {"code": -32601, "message": "Unsupported MCP method"}
            else:
                response["result"] = result
        except (ValueError, AttributeError, TypeError):
            continue
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

