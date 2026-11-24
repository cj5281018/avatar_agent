import asyncio
import json
import logging
import time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException


from .db import now, uid
from .services import HR_DEPARTMENT, MAX_CALLS_PER_SERVICE, redact
from avatar_contract import VERSION, manifest_digest, schema_error

log = logging.getLogger("uvicorn.error")
CONFIRM_MS = 30 * 60 * 1000
REPLAY_MS = 24 * 60 * 60 * 1000


class Operations:
    def __init__(self, broker, publish):
        self.broker = broker
        self.store = broker.store
        self.publish = publish
        with self.store.open() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    turn TEXT NOT NULL, mcp TEXT, service TEXT NOT NULL, action TEXT NOT NULL,
                    title TEXT NOT NULL, input TEXT NOT NULL, input_digest TEXT NOT NULL,
                    registration TEXT NOT NULL, version TEXT NOT NULL, checksum TEXT NOT NULL,
                    expires INTEGER NOT NULL, replay_until INTEGER, status TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, request TEXT, result TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS operations_session ON operations(session);
                CREATE TABLE IF NOT EXISTS operation_attempts (
                    request TEXT PRIMARY KEY, operation TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
                    purpose TEXT NOT NULL, started INTEGER NOT NULL, elapsed INTEGER, outcome TEXT
                );
                UPDATE operations SET status='unknown',revision=revision+1,
                    error='上次操作中断，请核实结果' WHERE status='executing';
                UPDATE operation_attempts SET outcome='INTERRUPTED' WHERE outcome IS NULL;
            """)

    @staticmethod
    def view(row):
        status = row["status"]
        if status == "pending" and row["expires"] <= now():
            status = "expired"
        actions = {"pending": ["confirm", "cancel"], "unknown": ["reconcile"],
                   }.get(status, [])
        if status == "unknown" and (row["replay_until"] or 0) > now():
            actions.append("retry")
        return {"operationId": row["id"], "sessionId": row["session"], "service": row["service"],
                "action": row["action"], "title": row["title"], "fields": json.loads(row["input"]),
                "status": status, "revision": row["revision"], "expiresAt": row["expires"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": row["error"], "requestId": row["request"], "availableActions": actions}

    def emit(self, row):
        item = self.view(row)
        self.publish(row["owner"], {"type": "operation.updated", "sessionId": row["session"], "operation": item})
        return item

    def propose(self, turn, service, action, contract, params, mcp):
        checksum = manifest_digest(params)
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=? AND user_id=?",
                              (turn.session_id, turn.owner)).fetchone():
                raise HTTPException(404, "会话不存在")
            row = db.execute("""SELECT * FROM operations WHERE owner=? AND session=? AND turn=?
                AND service=? AND action=? AND input_digest=? AND (status!='pending' OR expires>?)
                AND status NOT IN ('cancelled','expired') ORDER BY rowid DESC LIMIT 1""",
                             (turn.owner, turn.session_id, turn.turn_id, service.slug, action, checksum, now())).fetchone()
            if row is None:
                operation = uid("op")
                db.execute("""INSERT INTO operations
                    (id,owner,session,turn,mcp,service,action,title,input,input_digest,registration,version,checksum,expires,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')""",
                           (operation, turn.owner, turn.session_id, turn.turn_id, mcp, service.slug, action,
                            contract["title"], json.dumps(params, ensure_ascii=False), checksum,
                            service.registration, service.version, service.checksum, now()+CONFIRM_MS))
                row = self.get(db, turn.owner, turn.session_id, operation)
        return self.emit(row)

    @staticmethod
    def get(db, owner, session, operation):
        row = db.execute("SELECT * FROM operations WHERE id=? AND owner=? AND session=?",
                         (operation, owner, session)).fetchone()
        if row is None:
            raise HTTPException(404, "操作不存在")
        return row

    def list_for(self, db, owner, session):
        return [self.view(row) for row in db.execute(
            "SELECT * FROM operations WHERE owner=? AND session=? ORDER BY rowid", (owner, session))]

    async def authorize(self, row, auth):
        service = (self.broker.hr_service() if row["service"] == HR_DEPARTMENT
                   else self.broker.services.get(row["service"]))
        fingerprint = (row["version"], row["checksum"])
        if (service is None or not service.enabled or service.registration != row["registration"]
                or not await self.broker.verified(service)):
            raise HTTPException(409, "请恢复匹配的服务配对及契约后再操作")
        if (service.version, service.checksum) != fingerprint:
            action = self.broker.manifests.get(service.slug, {}).get("actions", {}).get(row["action"])
            compatible = action and {
                "contractVersion": row["version"], "manifestSha256": row["checksum"],
                "action": row["action"], "actionSha256": manifest_digest(action),
            } in service.compatible
            if not compatible:
                raise HTTPException(409, "操作使用的旧契约或动作不兼容")
        user = self.store.login_user(auth[1])
        if not user or user["id"] != row["owner"]:
            raise HTTPException(403, "当前登录无法操作")
        contract = self.broker.actions_for(service, user["role"]).get(row["action"])
        if not isinstance(contract, dict) or contract["kind"] != "write":
            raise HTTPException(403, "当前身份或配置未允许该动作")
        return service, contract

    async def attempt(self, row, auth, service, contract, purpose):
        request = uid("srq")
        if self.broker.active.get(service.slug, 0) >= MAX_CALLS_PER_SERVICE:
            raise HTTPException(429, "部门服务正在处理其他请求，请稍后再试")
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self.get(db, row["owner"], row["session"], row["id"])
            if latest["revision"] != row["revision"] or self.view(latest)["status"] not in ("pending", "unknown"):
                return latest
            if purpose == "execute" and row["status"] == "unknown" and (row["replay_until"] or 0) <= now():
                return latest
            db.execute("""UPDATE operations SET status='executing',request=?,error=NULL,revision=revision+1,
                          replay_until=COALESCE(replay_until,?) WHERE id=?""",
                       (request, now()+REPLAY_MS, row["id"]))
            db.execute("INSERT INTO operation_attempts VALUES (?,?,?,?,NULL,NULL)", (request, row["id"], purpose, now()))
            claimed = self.get(db, row["owner"], row["session"], row["id"])
        self.broker.active[service.slug] = self.broker.active.get(service.slug, 0) + 1
        self.emit(claimed)
        key = uid("suk")
        start = time.monotonic()
        grant = {"operationId": row["id"], "purpose": purpose}
        # This authorization is owned by the authenticated HTTP attempt, independent of the model turn.
        actor = SimpleNamespace(owner=row["owner"], login_key=auth[1], stop=asyncio.Event())
        self.broker.calls[key] = (service.slug, actor, row["action"], request, grant)
        body = {"protocolVersion": VERSION, "requestId": request, "action": row["action"],
                "userKey": key, "operationId": row["id"],
                "contractVersion": row["version"], "manifestSha256": row["checksum"]}
        if purpose == "execute":
            body["input"] = json.loads(row["input"])
        status, outcome, result, error = "unknown", "INTERRUPTED", None, None
        log.info("operation.dispatch turn=%s mcp=%s operation=%s request=%s service=%s action=%s purpose=%s",
                 row["turn"], row["mcp"], row["id"], request, service.slug, row["action"], purpose)
        try:
            reply = await self.broker.post(service, "/avatar" if purpose == "execute" else "/operations/status", body)
            if reply.get("requestId") != request or reply.get("operationId") != row["id"]:
                raise HTTPException(502, "响应标识不匹配")
            if purpose == "status" and reply.get("state") == "not_recorded":
                outcome, error = "NOT_RECORDED", "暂未找到提交记录，可在有效期内用原参数重试"
            else:
                result = reply.get("data")
                if ((purpose == "status" and reply.get("state") != "succeeded")
                        or schema_error(contract["outputSchema"], result)):
                    result = None
                    raise HTTPException(502, "响应不符合正式结果契约",
                                        headers={"X-Avatar-Error-Code": "OUTPUT_SCHEMA_MISMATCH"})
                result = redact(result, (service.key, key))
                status, outcome = "succeeded", "OK"
        except HTTPException as exc:
            outcome = (exc.headers or {}).get("X-Avatar-Error-Code", "UPSTREAM_TIMEOUT" if exc.status_code == 504 else "UNKNOWN")
            if outcome == "CONTRACT_MISMATCH":
                self.broker.manifests.pop(service.slug, None)
                self.broker.contract_errors[service.slug] = outcome
            if row["status"] == "pending" and outcome in ("INVALID_INPUT", "FORBIDDEN", "NOT_FOUND", "STATE_CONFLICT"):
                status = "rejected"
            error = f"{exc.detail}（{request}）"
        finally:
            self.broker.calls.pop(key, None)
            self.broker.active[service.slug] -= 1
            if not self.broker.active[service.slug]:
                self.broker.active.pop(service.slug)
            with self.store.open() as db:
                db.execute("""UPDATE operations SET status=?,result=?,error=?,revision=revision+1
                    WHERE id=? AND request=? AND status='executing'""",
                           (status, json.dumps(result, ensure_ascii=False) if status == "succeeded" else None,
                            error or ("上次请求结果待核实" if status == "unknown" else None), row["id"], request))
                db.execute("UPDATE operation_attempts SET elapsed=?,outcome=? WHERE request=?",
                           (int((time.monotonic()-start)*1000), outcome, request))
                finished = self.get(db, row["owner"], row["session"], row["id"])
            log.info("operation.result operation=%s request=%s outcome=%s", row["id"], request, outcome)
            self.emit(finished)
        return finished

    async def change(self, owner, session, operation, mode, auth):
        with self.store.open() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get(db, owner, session, operation)
            status = self.view(row)["status"]
            if status == "expired" and row["status"] == "pending":
                db.execute("UPDATE operations SET status='expired',revision=revision+1 WHERE id=?", (operation,))
                row = self.get(db, owner, session, operation)
            if mode == "cancel" and status == "pending":
                db.execute("UPDATE operations SET status='cancelled',revision=revision+1 WHERE id=?", (operation,))
                row = self.get(db, owner, session, operation)
        if mode == "cancel":
            return self.emit(row) if status == "pending" else self.view(row)
        if (mode == "confirm" and status != "pending") or (mode != "confirm" and status != "unknown"):
            return self.view(row)
        try:
            service, contract = await self.authorize(row, auth)
        except HTTPException as exc:
            if status != "unknown":
                raise
            with self.store.open() as db:
                db.execute("""UPDATE operations SET error=?,revision=revision+1
                    WHERE id=? AND revision=? AND status='unknown'""", (exc.detail, operation, row["revision"]))
                row = self.get(db, owner, session, operation)
            return self.emit(row)
        row = await self.attempt(row, auth, service, contract, "execute" if mode == "confirm" else "status")
        with self.store.open() as db:
            status_attempt = db.execute("SELECT outcome FROM operation_attempts WHERE request=? AND purpose='status'",
                                        (row["request"],)).fetchone() if mode == "retry" else None
        if row["status"] == "unknown" and status_attempt and status_attempt["outcome"] == "NOT_RECORDED":
            # Verify the current login again after the network status request.
            try:
                service, contract = await self.authorize(row, auth)
            except HTTPException:
                return self.view(row)
            row = await self.attempt(row, auth, service, contract, "execute")
        return self.view(row)


def routes(get_operations, current):
    router = APIRouter()
    base = "/v1/sessions/{session}/operations/{operation}"

    @router.get(base)
    async def status(session: str, operation: str, auth=Depends(current)):
        operations = get_operations()
        with operations.store.open() as db:
            return operations.view(operations.get(db, auth[0]["id"], session, operation))

    @router.post(base + "/{mode}")
    async def change(session: str, operation: str, mode: str, auth=Depends(current)):
        if mode not in ("confirm", "cancel", "reconcile", "retry"):
            raise HTTPException(404, "未知操作")
        return await get_operations().change(auth[0]["id"], session, operation, mode, auth)

    return router
