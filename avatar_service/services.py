import asyncio
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from jsonschema import SchemaError
from fastapi import APIRouter, Depends, HTTPException, Query

from .db import digest, now, uid, user_view
from .hr_bridge import SLUG as HR_SLUG, VERSION

from avatar_contract import manifest_check, schema_error

HR_DEPARTMENT = "hr"
DEFAULT_HR_CONFIG = Path(__file__).resolve().parents[1] / "deploy" / "hr-contract.json"

log = logging.getLogger("uvicorn.error")
CODES = {400:"INVALID_INPUT",403:"FORBIDDEN",404:"NOT_FOUND",409:"CONTRACT_MISMATCH",
         422:"INVALID_INPUT",429:"RATE_LIMITED",502:"UPSTREAM_ERROR",504:"UPSTREAM_TIMEOUT"}

MAX_INPUT = 64 * 1024
MAX_RESPONSE = 256 * 1024
MAX_CALLS_PER_TURN = 12
MAX_CALLS_PER_SERVICE = 4
REQUEST_DEADLINE = 15


@dataclass(frozen=True)
class Service:
    slug: str
    name: str
    description: str
    origin: str
    actions: dict
    key: str
    enabled: bool = True
    version: str = ""
    checksum: str = ""
    allowed: tuple = ()
    compatible: tuple = ()

    @property
    def contract(self):
        return {"contractVersion": self.version, "manifestSha256": self.checksum} if self.version else {}

    @property
    def id(self):
        return "svc_" + self.slug

    @property
    def registration(self):
        return digest(VERSION + "\n" + self.origin + "\n" + self.key)

    def view(self):
        return {"id": self.id, "slug": self.slug, "name": self.name}

    def actions_for(self, role):
        return {name: action["description"] for name, action in self.actions.items() if role in action["roles"]}


def load_services():
    filename = os.environ.get("PY_AVATAR_SERVICES_FILE")
    rows = json.loads(Path(filename).read_text(encoding="utf-8-sig")) if filename else []
    versioned = isinstance(rows, dict) and rows.get("version") == 1
    if versioned:
        rows = rows.get("services")
    if not isinstance(rows, list):
        raise ValueError("PY_AVATAR_SERVICES_FILE 必须包含服务数组")
    services = {}
    for row in rows:
        slug = row["slug"]
        if (not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", slug) or slug in (HR_SLUG, HR_DEPARTMENT) or slug in services):
            raise ValueError("部门服务 slug 无效、保留或重复")
        origin = row["baseUrl"].rstrip("/")
        url = urlsplit(origin)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.query or url.fragment or url.path):
            raise ValueError(f"{slug} 的 baseUrl 必须是 HTTP(S) 源地址")
        key_env = row["serviceKeyEnv"]
        if not re.fullmatch(r"PY_AVATAR_PLUGIN_[A-Z0-9_]+_KEY", key_env):
            raise ValueError(f"{slug} 的密钥环境变量须为 PY_AVATAR_PLUGIN_*_KEY")
        key = os.environ.get(key_env, "")
        if len(key) < 32 or len(key) > 200 or key in {service.key for service in services.values()}:
            raise ValueError(f"{slug} 必须配置独立的服务密钥，长度为 32–200")
        if versioned:
            allowed = row.get("allowedActions")
            version, checksum, enabled = row.get("contractVersion"), row.get("manifestSha256"), row.get("enabled")
            if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", version)
                    or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                    or not isinstance(enabled, bool) or not isinstance(allowed, list) or not allowed
                    or any(not isinstance(a, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", a) for a in allowed)
                    or len(set(allowed)) != len(allowed)):
                raise ValueError(f"{slug} 的契约配置无效")
            services[slug] = Service(slug, slug, "", origin, {}, key, enabled, version, checksum, tuple(allowed))
            continue
        actions = row["actions"]
        if not isinstance(actions, dict) or not actions:
            raise ValueError(f"{slug} 缺少动作定义")
        if any(not isinstance(row[field], str) or not 1 <= len(row[field].strip()) <= bound
               for field, bound in (("name", 80), ("description", 400))):
            raise ValueError(f"{slug} 的服务名称或描述无效")
        for name, action in actions.items():
            if (not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", name)
                    or not isinstance(action, dict)
                    or action.get("kind") != "read"
                    or not isinstance(action.get("description"), str)
                    or not 1 <= len(action["description"].strip()) <= 500
                    or not isinstance(action.get("roles"), list) or not action["roles"]
                    or any(role not in ("admin", "member") for role in action["roles"])):
                raise ValueError(f"{slug} 的动作定义无效")
        services[slug] = Service(slug, row["name"], row["description"], origin, actions, key)
    return services


class ServiceBroker:
    def __init__(self, hr):
        self.hr = hr
        hr.broker = self
        self.turns = {}
        self.calls = {}
        self.pairing = set()
        self.active = {}

    def start(self, store):
        self.services = load_services()
        config_path = Path(os.environ.get("PY_AVATAR_HR_CONTRACT_FILE") or DEFAULT_HR_CONFIG)
        self.hr_config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        self.manifests = {}
        self.contract_errors = {}
        self.contract_locks = {slug: asyncio.Lock() for slug in (*self.services, HR_DEPARTMENT)}
        self.store = store
        with store.open() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS avatar_service_pairings (
                    slug TEXT PRIMARY KEY, registration TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS avatar_service_audit (
                    request_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    service TEXT NOT NULL, action TEXT NOT NULL, started_at INTEGER NOT NULL,
                    elapsed_ms INTEGER, outcome TEXT NOT NULL, status INTEGER
                );
                CREATE INDEX IF NOT EXISTS avatar_service_audit_turn
                ON avatar_service_audit(turn_id, started_at);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(avatar_service_audit)")}
            for column in ("mcp_id", "code"):
                if column not in columns:
                    db.execute(f"ALTER TABLE avatar_service_audit ADD COLUMN {column} TEXT")
            db.execute("UPDATE avatar_service_audit SET outcome='unknown' WHERE outcome='started'")

    def hr_service(self):
        with self.store.open() as db:
            pairing = db.execute("SELECT id, key_hash FROM hr_pairing WHERE ready=1").fetchone()
        if pairing:
            return Service(HR_DEPARTMENT, "人力资源", "", self.hr.hr_base, {}, pairing["key_hash"],
                           version=self.hr_config["contractVersion"], checksum=self.hr_config["manifestSha256"],
                           allowed=tuple(self.hr_config["allowedActions"]),
                           compatible=tuple(self.hr_config["compatibleActions"]))

    def hr_identity(self, pairing, user_key):
        if not isinstance(user_key, str):
            raise HTTPException(401, "服务或用户身份校验失败")
        call = self.calls.get(user_key)
        turn = call[1] if call and call[0] == HR_DEPARTMENT else None
        user = self.store.login_user(turn.login_key) if turn and not turn.stop.is_set() else None
        service = self.hr_service()
        if not user or user["id"] != turn.owner or not service or service.key != pairing["key_hash"]:
            raise HTTPException(401, "服务或用户身份校验失败")
        return {"ok": True, "service": service.view(), "user": user_view(user),
                "grant": {"action": call[2], "requestId": call[3], **(call[4] if len(call) > 4 else {})}}

    def ready(self, service):
        if service.slug == HR_DEPARTMENT:
            current = self.hr_service()
            return bool(current and current.registration == service.registration)
        with self.store.open() as db:
            row = db.execute("SELECT registration FROM avatar_service_pairings WHERE slug=?", (service.slug,)).fetchone()
        return bool(row and hmac.compare_digest(row["registration"], service.registration))

    async def verified(self, service):
        if not service.enabled or not self.ready(service):
            return False
        if not service.version:
            return True
        slug = service.slug
        async with self.contract_locks[slug]:
            if slug in self.manifests:
                return True
            try:
                data = await self.post(service, "/manifest", None)
                manifest = manifest_check(data.get("manifest"), slug, service.version, service.checksum)
                if not set(service.allowed) <= manifest["actions"].keys():
                    raise ValueError("允许项不在清单中")
            except (HTTPException, ValueError, KeyError, TypeError, SchemaError, RecursionError):
                self.contract_errors[slug] = "CONTRACT_MISMATCH"
                return False
            self.contract_errors.pop(slug, None)
            self.manifests[slug] = manifest
            return True

    def actions_for(self, service, role):
        if not service.version:
            return service.actions_for(role)
        actions = self.manifests[service.slug]["actions"]
        return {name: actions[name] for name in service.allowed if role in actions[name]["roles"]}

    def identify(self, body):
        key = body.get("serviceKey")
        if isinstance(key, str) and 32 <= len(key) <= 200:
            key_hash = digest(key)
            for service in self.services.values():
                if service.enabled and hmac.compare_digest(digest(service.key), key_hash):
                    return service
        raise HTTPException(401, "部门服务身份校验失败")

    def caller(self, body):
        cap = body.get("capability")
        state = self.turns.get(cap) if isinstance(cap, str) else None
        turn = state["turn"] if state else None
        user = self.store.login_user(turn.login_key) if turn and not turn.stop.is_set() else None
        if not user or user["id"] != turn.owner:
            raise HTTPException(401, "当前聊天回合或登录已失效")
        return state, user

    async def post(self, service, path, body):
        try:
            return await asyncio.wait_for(self.read_response(service, path, body), timeout=REQUEST_DEADLINE)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise HTTPException(504, "部门服务响应超时，操作结果待核实") from None
        except (httpx.HTTPError, ValueError, RecursionError):
            raise HTTPException(502, "部门服务连接失败或响应无效") from None

    async def read_response(self, service, path, body):
        async with httpx.AsyncClient(timeout=None, trust_env=False, follow_redirects=False) as client:
            method = "GET" if path == "/manifest" else "POST"
            options = {"json": body} if method == "POST" else {}
            if method == "GET" and service.slug != HR_DEPARTMENT:
                options["headers"] = {"Authorization": "Bearer " + service.key}
            async with client.stream(method, service.origin + path, **options) as response:
                status = response.status_code
                chunks = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    if len(chunks) + len(chunk) > MAX_RESPONSE:
                        raise HTTPException(502, "部门服务响应过大")
                    chunks.extend(chunk)
        try:
            data = json.loads(chunks, parse_constant=reject_constant)
        except ValueError:
            if 200 <= status < 300:
                raise
            data = None
        if not 200 <= status < 300:
            code = data.get("code") if isinstance(data, dict) else None
            allowed = {"INVALID_INPUT": 400, "FORBIDDEN": 403, "NOT_FOUND": 404, "CONTRACT_MISMATCH": 409,
                       "IDEMPOTENCY_CONFLICT": 409, "STATE_CONFLICT": 409, "OUTPUT_SCHEMA_MISMATCH": 502}
            if (isinstance(code, str) and allowed.get(code) == status
                    and (path != "/avatar" and path != "/operations/status"
                         or data.get("requestId") == body.get("requestId"))):
                headers = {"X-Avatar-Error-Code": code}
            else:
                headers = None
            message = "未找到记录" if status == 404 else "部门服务拒绝了请求"
            if code == "STATE_CONFLICT" and headers:
                message = "HR 记录已更新或当前状态不允许办理，请重新查询"
            raise HTTPException(status if status in (400,403,404,409,422,429) else 502, message, headers=headers)
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise HTTPException(502, "部门服务响应不符合契约")
        return data

    async def pair(self, service, setup_code):
        if self.ready(service):
            if not await self.verified(service):
                raise HTTPException(409, "部门服务契约需要重新核验")
            return
        if service.slug in self.pairing:
            raise HTTPException(409, "服务正在配对")
        self.pairing.add(service.slug)
        try:
            base = self.hr.origin
            result = await self.post(service, "/setup", {
                "protocolVersion": VERSION,
                "setupCode": setup_code,
                "service": service.view(),
                "avatar": {"baseUrl": base, "serviceCheckUrl": base + "/internal/services/servicecheck",
                           "identityCheckUrl": base + "/internal/services/identitycheck"},
                "credentials": {"serviceKey": service.key},
            })
            if (result.get("protocolVersion") != VERSION
                    or result.get("invoke") != {"transport": "http", "target": service.origin + "/avatar"}):
                raise HTTPException(502, "部门服务返回的协议或调用地址与配置不一致")
            with self.store.open() as db:
                db.execute("""INSERT INTO avatar_service_pairings VALUES (?,?)
                              ON CONFLICT(slug) DO UPDATE SET registration=excluded.registration""",
                           (service.slug, service.registration))
            if not await self.verified(service):
                raise HTTPException(409, "部门服务契约需要重新核验")
        finally:
            self.pairing.discard(service.slug)

    async def invoke(self, service, action, params, state, user, mcp_id=None):
        turn = state["turn"]
        count = self.active.get(service.slug, 0)
        if count >= MAX_CALLS_PER_SERVICE or state["calls"] >= MAX_CALLS_PER_TURN:
            raise HTTPException(429, "部门服务并发或本轮工具调用次数已达上限")
        request_id, start = uid("srq"), time.monotonic()
        title = f"MCP → Avatar · {service.slug}/{action}"
        detail = f"回合 {turn.turn_id}\nMCP 调用 ID {mcp_id or '未提供'}\n请求 ID {request_id}"
        with self.store.open() as db:
            db.execute("""INSERT INTO avatar_service_audit
                          (request_id,turn_id,user_id,service,action,started_at,outcome,mcp_id)
                          VALUES (?,?,?,?,?,?,'started',?)""",
                       (request_id, turn.turn_id, user["id"], service.slug, action, now(), mcp_id))
        if mcp_id:
            turn.trace(mcp_id, title, detail, "in_progress")
        user_key = uid("suk")
        self.calls[user_key] = (service.slug, turn, action, request_id)
        state["calls"] += 1
        self.active[service.slug] = count + 1
        outcome, status, code = "unknown", None, None
        try:
            try:
                encoded = json.dumps(params, ensure_ascii=True, allow_nan=False)
            except (ValueError, RecursionError):
                encoded = ""
            if not isinstance(params, dict) or not encoded or len(encoded) > MAX_INPUT:
                raise HTTPException(400, "服务参数必须是 64 KiB 以内的 JSON 对象")
            contract = self.manifests[service.slug]["actions"][action] if service.version else None
            if contract:
                error = schema_error(contract["inputSchema"], params)
                if error:
                    # Use declared field names only. An arbitrary submitted key can contain private data.
                    name = next(iter(error.absolute_path), None)
                    fields = contract["inputSchema"].get("properties", {})
                    suffix = f"：{name}" if isinstance(name, str) and name in fields else ""
                    raise HTTPException(400, "参数不符合工具 Schema" + suffix)
            if service.version and service.slug in state.get("timed_out", set()):
                raise HTTPException(504, "部门服务暂时无法查询，请稍后重试")
            log.info("plugin.dispatch turn=%s mcp=%s request=%s service=%s action=%s",
                     turn.turn_id, mcp_id, request_id, service.slug, action)
            result = await self.post(service, "/avatar", {
                "protocolVersion": VERSION, "requestId": request_id, "action": action,
                "input": params, "userKey": user_key, **service.contract,
            })
            if contract and (result.get("requestId") != request_id or
                    schema_error(contract["outputSchema"], result.get("data"))):
                code = "OUTPUT_SCHEMA_MISMATCH"
                raise HTTPException(502, "服务返回不符合已核验的契约")
            data = redact(result.get("data"), (service.key, user_key))
            outcome, status = "success", 200
            return {"ok": True, "requestId": request_id, "service": service.view(),
                    "data": data}
        except RecursionError:
            status = 502
            raise HTTPException(status, "部门服务响应嵌套过深",
                                headers={"X-Avatar-Request-Id": request_id}) from None
        except HTTPException as error:
            status = error.status_code
            outcome = "rejected" if status < 500 else "unknown"
            code = code or (error.headers or {}).get("X-Avatar-Error-Code") or CODES.get(status, "UPSTREAM_ERROR")
            if service.version and status == 504:
                state.setdefault("timed_out", set()).add(service.slug)
            if service.version and code == "CONTRACT_MISMATCH":
                self.manifests.pop(service.slug, None)
                self.contract_errors[service.slug] = code
            raise HTTPException(status, error.detail, headers={"X-Avatar-Request-Id": request_id,
                                                              "X-Avatar-Error-Code": code}) from None
        finally:
            self.calls.pop(user_key, None)
            remaining = self.active[service.slug] - 1
            if remaining:
                self.active[service.slug] = remaining
            else:
                self.active.pop(service.slug)
            with self.store.open() as db:
                elapsed = int((time.monotonic() - start) * 1000)
                db.execute("UPDATE avatar_service_audit SET elapsed_ms=?,outcome=?,status=?,code=? WHERE request_id=?",
                           (elapsed, outcome, status, code, request_id))
            if mcp_id:
                turn.trace(mcp_id, title, detail + f"\n{code or outcome} · {elapsed}ms",
                           "completed" if outcome == "success" else "failed")
            log.info("plugin.result request=%s outcome=%s code=%s elapsed_ms=%s", request_id, outcome, code, elapsed)

    def routes(self, current):
        router = APIRouter()

        async def admin(auth=Depends(current)):
            if auth[0]["role"] != "admin":
                raise HTTPException(403, "仅管理员可管理部门服务")
            return auth

        @router.get("/v1/admin/plugins")
        async def list_plugins(_auth=Depends(admin)):
            items = []
            for service in self.services.values():
                ready = await self.verified(service)
                reason = "DISABLED" if not service.enabled else self.contract_errors.get(service.slug, "UNPAIRED")
                items.append({**service.view(), **service.contract, "ready": ready, "reason": None if ready else reason})
            return items

        @router.post("/v1/admin/plugins/{slug}/setup")
        async def pair(slug: str, body: dict, _auth=Depends(admin)):
            service = self.services.get(slug)
            if not service or not service.enabled:
                raise HTTPException(404, "部门服务不存在")
            code = body.get("setupCode")
            if not isinstance(code, str) or not 1 <= len(code.strip()) <= 200:
                raise HTTPException(400, "请提供有效的配对码")
            await self.pair(service, code.strip())
            return {"ok": True, "service": service.view()}

        @router.get("/v1/admin/plugin-audit")
        async def audit(turnId: str, limit: int = Query(50, ge=1, le=100), _auth=Depends(admin)):
            with self.store.open() as db:
                return [dict(row) for row in db.execute("""SELECT * FROM avatar_service_audit
                        WHERE turn_id=? ORDER BY started_at, rowid LIMIT ?""", (turnId, limit))]

        @router.post("/internal/services/servicecheck")
        async def servicecheck(body: dict):
            service = self.identify(body)
            if service.slug not in self.pairing and not self.ready(service):
                raise HTTPException(401, "部门服务尚未配对")
            return {"ok": True, "service": service.view()}

        @router.post("/internal/services/identitycheck")
        async def identitycheck(body: dict):
            service = self.identify(body)
            user_key = body.get("userKey")
            call = self.calls.get(user_key) if isinstance(user_key, str) else None
            turn = call[1] if call and call[0] == service.slug else None
            user = self.store.login_user(turn.login_key) if turn and not turn.stop.is_set() else None
            if not user or user["id"] != turn.owner:
                raise HTTPException(401, "服务或用户身份校验失败")
            return {"ok": True, "service": service.view(), "user": user_view(user),
                    "grant": {"action": call[2], "requestId": call[3], **(call[4] if len(call) > 4 else {})}}

        @router.post("/internal/services/tool")
        async def tool(body: dict):
            state, user = self.caller(body)
            if body.get("operation") == "list":
                with self.store.open() as db:
                    hr_ready = db.execute("SELECT 1 FROM hr_pairing WHERE ready=1").fetchone()
                items = [{"id": "svc_" + HR_SLUG, "slug": HR_SLUG, "name": "Python HR Daily",
                          "description": "HR 请假、审批与查询", "actions": {
                              "list_my_requests": "查询自己的请假申请和进度，输入为空对象",
                              "list_pending_approvals": "查询分配给自己的待审批请假单，输入为空对象",
                          }}] if hr_ready else []
                selected, query = body.get("service"), body.get("query", "")
                if (not isinstance(query, str) or len(query) > 200
                        or (selected is not None and not isinstance(selected, str))):
                    raise HTTPException(400, "目录查询无效")
                for service in self.services.values():
                    if (selected and service.slug != selected) or not await self.verified(service):
                        continue
                    actions = self.actions_for(service, user["role"])
                    if actions:
                        manifest = self.manifests.get(service.slug, {})
                        items.append({**service.view(), "name": manifest.get("title", service.name),
                                      "description": manifest.get("description", service.description),
                                      "department": manifest.get("department"), "actions": actions})
                hr = self.hr_service()
                if hr and selected != HR_SLUG and items:
                    actions = dict(self.hr_config["leaveActions"])
                    description = items[0]["description"]
                    if await self.verified(hr):
                        manifest = self.manifests[hr.slug]
                        description = manifest["description"]
                        actions = dict(manifest["leaveActions"])
                        actions.update(self.actions_for(hr, user["role"]))
                    items[0] = {**hr.view(), "department": "HR", "description": description,
                                "actions": actions}
                return {"services": filter_catalog(items, selected, query)}
            slug, action, params = body.get("service"), body.get("action"), body.get("input", {})
            service = (self.hr_service() if slug == HR_DEPARTMENT else
                       self.services.get(slug) if isinstance(slug, str) else None)
            if not service or not service.enabled or not self.ready(service):
                raise HTTPException(404, "部门服务不存在或尚未配对")
            if not await self.verified(service):
                raise HTTPException(409, "部门服务契约需要重新核验")
            if not isinstance(action, str) or action not in self.actions_for(service, user["role"]):
                raise HTTPException(403, "当前用户不可调用该服务动作")
            mcp_id = body.get("mcpTraceId")
            if not isinstance(mcp_id, str) or not re.fullmatch(r"mcp_[0-9a-f]{32}", mcp_id):
                mcp_id = None
            contract = self.actions_for(service, user["role"])[action]
            if isinstance(contract, dict) and contract["kind"] == "write":
                if state["calls"] >= MAX_CALLS_PER_TURN:
                    raise HTTPException(429, "本轮工具调用次数已达上限")
                state["calls"] += 1
                if not isinstance(params, dict):
                    raise HTTPException(400, "写操作参数必须为对象")
                normalized = {name: value.strip() if isinstance(value, str) else value
                              for name, value in params.items()}
                try:
                    valid_size = len(json.dumps(normalized, ensure_ascii=True, allow_nan=False)) <= MAX_INPUT
                except (ValueError, RecursionError):
                    valid_size = False
                if not valid_size or schema_error(contract["inputSchema"], normalized):
                    raise HTTPException(400, "参数不符合工具 Schema")
                turn = state["turn"]
                item = self.operations.propose(turn, service, action, contract, normalized, mcp_id)
                if mcp_id:
                    turn.trace(mcp_id, f"MCP → Avatar · {slug}/{action}",
                               f"回合 {turn.turn_id}\n提案 {item['operationId']}，正式执行需用户操作确认卡")
                return {"ok": True, "operationId": item["operationId"], "status": item["status"],
                        "message": "请用户在桌面卡片核对并确认，聊天消息不会提交"}
            return await self.invoke(service, action, params, state, user, mcp_id)

        return router


def filter_catalog(items, selected, query):
    words = query.casefold().split()
    result = []
    for item in items:
        if selected and item["slug"] != selected:
            continue
        service_text = " ".join(str(item.get(key) or "") for key in ("slug", "name", "description", "department"))
        actions = {name: action for name, action in item["actions"].items()
                   if all(word in (service_text + " " + name + " " + json.dumps(
                       action, ensure_ascii=False)).casefold() for word in words)}
        if actions:
            result.append({**item, "actions": actions})
    return result


def redact(value, keys):
    if isinstance(value, str):
        for key in keys:
            value = value.replace(key, "[已隐藏]")
    elif isinstance(value, dict):
        return {redact(name, keys): redact(item, keys) for name, item in value.items()}
    elif isinstance(value, list):
        return [redact(item, keys) for item in value]
    return value


def reject_constant(value):
    raise ValueError(f"无效的 JSON 常量 {value}")
