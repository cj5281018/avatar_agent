"""Pure helpers for turning a tool result into safe trace fields.

Kept free of service imports so the rules can be unit-tested in isolation.
"""
from __future__ import annotations

import json
import re
from typing import Any

RETRIEVAL_TOOLS = {"search"}
OPERATION_ID = re.compile(r"^lop_[A-Za-z0-9_-]{1,80}$")
LEAVE_ID = re.compile(r"^leave_[A-Za-z0-9_-]{1,80}$")


MAX_TITLES = 5

# Stable failure categories. The tool's raw error text is never persisted: it
# can echo upstream bodies, request content or credentials. Matching only reads
# the text to pick a category.
SAFE_REASON = {
    "invalid_arguments": "工具参数无效",
    "binding_required": "需要先绑定或续期员工身份",
    "upstream_unavailable": "部门工具暂时不可用",
    "timeout": "工具调用超时",
    "tool_error": "工具执行失败",
}


def _classify_failure(text: str) -> str:
    lowered = text.lower()
    if "未知工具" in text or "参数" in text or "invalid" in lowered or "validation" in lowered:
        return "invalid_arguments"
    if "绑定" in text or "binding" in lowered:
        return "binding_required"
    if "超时" in text or "timeout" in lowered:
        return "timeout"
    if "不可用" in text or "unavailable" in lowered or "502" in text or "503" in text:
        return "upstream_unavailable"
    return "tool_error"


def _extract_refs(content: list[Any], *, is_error: bool) -> dict[str, Any]:
    """Pull stable, user-safe identifiers out of a tool result.

    Only identifiers, counts, result titles and the tool's own error text are
    kept; the tool input (which may contain a leave reason) and the raw result
    payload are not recorded.
    """
    refs: dict[str, Any] = {}
    hits = 0
    titles: list[str] = []
    failure_text = ""
    for block in content or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "search_result":
            hits += 1
            title = block.get("title")
            if isinstance(title, str) and title and len(titles) < MAX_TITLES:
                titles.append(title[:120])
            continue
        if block_type != "text":
            continue
        text = block.get("text")
        if is_error and not failure_text and isinstance(text, str):
            failure_text = text
        if not isinstance(text, str) or not text.startswith("{"):
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        for key, target, pattern in (
            ("operationId", "operationId", OPERATION_ID),
            ("leaveId", "leaveId", LEAVE_ID),
        ):
            value = parsed.get(key)
            if isinstance(value, str) and pattern.match(value):
                refs[target] = value
        if parsed.get("type") == "leave_preview":
            refs.setdefault("kind", "leave_preview")
    if hits:
        refs["hits"] = hits
    if titles:
        refs["titles"] = titles
    if is_error:
        # A failed step keeps only a stable code and a safe sentence: no
        # operation/leave identifiers, titles, counts or result text.
        code = ""
        if failure_text.startswith("{"):
            try:
                parsed = json.loads(failure_text)
                if isinstance(parsed, dict) and isinstance(parsed.get("errorCode"), str):
                    code = parsed["errorCode"][:40]
            except ValueError:
                code = ""
        if code not in SAFE_REASON:
            code = _classify_failure(failure_text)
        return {"reasonCode": code, "reason": SAFE_REASON[code]}
    return refs


