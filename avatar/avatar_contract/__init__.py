import hashlib
import json
import re
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

VERSION = "avatar-service/v1"
ACTION = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
ROLES = {"admin", "member"}


def base_url(value):
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ValueError("配置地址必须为 HTTP(S) origin，例如 http://127.0.0.1:8788")
    return value


def manifest_digest(manifest):
    encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def manifest_check(manifest, slug, version, digest):
    if (not isinstance(manifest, dict) or manifest_digest(manifest) != digest
            or manifest.get("manifestVersion") != 1 or manifest.get("protocolVersion") != VERSION
            or manifest.get("slug") != slug or manifest.get("contractVersion") != version):
        raise ValueError("清单摘要、版本或服务身份不匹配")
    if any(not isinstance(manifest.get(k), str) or not 1 <= len(manifest[k].strip()) <= 500
           for k in ("title", "description")):
        raise ValueError("服务说明无效")
    actions = manifest.get("actions")
    if not isinstance(actions, dict) or not actions or len(actions) > 50:
        raise ValueError("动作定义无效")
    for name, action in actions.items():
        if (not ACTION.fullmatch(name) or not isinstance(action, dict) or action.get("kind") not in ("read", "write")
                or any(not isinstance(action.get(k), str) or not 1 <= len(action[k].strip()) <= 500
                       for k in ("title", "description"))
                or not isinstance(action.get("roles"), list) or not action["roles"]
                or any(role not in ROLES for role in action["roles"])):
            raise ValueError("动作类型或角色定义无效")
        if action["kind"] == "write" and (action.get("confirmation") != "required"
                or action.get("recovery") != {"statusLookup": True, "replay": "same_operation"}):
            raise ValueError("写操作需要确认与恢复契约")
        for kind in ("inputSchema", "outputSchema"):
            schema = action[kind]
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("动作 Schema 必须为对象")
            if kind == "inputSchema" and schema.get("additionalProperties") is not False:
                raise ValueError("输入必须拒绝额外字段")
            # Schema resolution stays local, including Pydantic's generated definitions.
            check_refs(schema)
            Draft202012Validator.check_schema(schema)
    return manifest


def check_refs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("$id", "$dynamicRef", "$schema") or (key == "$ref" and
                    (not isinstance(value, str) or not value.startswith("#/$defs/"))):
                raise ValueError("仅支持本地 definitions 引用")
            check_refs(value)
    elif isinstance(node, list):
        for value in node:
            check_refs(value)


def schema_error(schema, value):
    return next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value), None)
