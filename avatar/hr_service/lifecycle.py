import json
from datetime import date, timedelta

from .db import ServiceError, identifier, now
from .recruitment import action, latest_event, string, visible_application

SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_offers (
    id TEXT PRIMARY KEY, application TEXT NOT NULL UNIQUE REFERENCES recruitment_applications(id),
    version INTEGER NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_offer_versions (
    offer TEXT NOT NULL REFERENCES hr_offers(id), version INTEGER NOT NULL,
    terms TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(offer,version)
);
CREATE TABLE IF NOT EXISTS hr_onboarding (
    id TEXT PRIMARY KEY, offer TEXT NOT NULL UNIQUE REFERENCES hr_offers(id), offer_version INTEGER NOT NULL,
    template TEXT NOT NULL, planned TEXT NOT NULL, actual TEXT, owner TEXT NOT NULL,
    revision INTEGER NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_onboarding_tasks (
    onboarding TEXT NOT NULL REFERENCES hr_onboarding(id), task TEXT NOT NULL,
    title TEXT NOT NULL, owner TEXT NOT NULL, day_offset INTEGER NOT NULL, due TEXT NOT NULL,
    required INTEGER NOT NULL, state TEXT NOT NULL, evidence TEXT, PRIMARY KEY(onboarding,task)
);
CREATE TABLE IF NOT EXISTS hr_lifecycle_operations (
    actor TEXT NOT NULL, operation TEXT NOT NULL, action TEXT NOT NULL, application TEXT NOT NULL,
    input TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(actor,operation)
);
CREATE TABLE IF NOT EXISTS hr_lifecycle_history (
    id INTEGER PRIMARY KEY, application TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    parameters TEXT NOT NULL, occurred_at INTEGER NOT NULL
);
"""

TEMPLATES = {"intern-v1": (
    ("documents", "核对报到材料", -2, 1),
    ("buddy", "确认导师和首日安排", -1, 1),
    ("orientation", "完成入职说明", 3, 1),
)}
REV = {"type": "integer", "minimum": 1}
DAY = string(format="date")
OFFER = {"offerId": string(), "version": REV, "revision": REV}
ONBOARDING = {"onboardingId": string(), "revision": REV}


def definition(title, description, fields, write=False, required=None):
    return action(title, description, fields, tuple(fields) if required is None else required, write)


ACTIONS = {
    "offer.preview": definition("预览 Offer 草稿素材", "按已完成面试的应聘单查岗位及虚构条款。", {"applicationId": string()}),
    "offer.get": definition("查询应聘单的 Offer", "按应聘单号查询 Offer、当前版本、历史版本与入职编号。", {"applicationId": string()}),
    "offer.save_draft": definition("保存虚构 Offer 草稿版本", "先查应聘单与 Offer。第一次保存 expectedRevision 为 0。只保存演示模板与预计到岗日。",
                                   {"applicationId": string(), "expectedRevision": {"type": "integer", "minimum": 0},
                                    "plannedDate": DAY}, True),
    "offer.record_sent": definition("登记 Offer 已发出", "先查询 Offer 并使用当前版本和修订号。此动作登记人工发出。", OFFER, True),
    "offer.record_response": definition("登记 Offer 回复", "接受时填写预计到岗日和模板 intern-v1，系统自动创建入职清单。拒绝需填写原因。",
                                        {**OFFER, "response": string(enum=["accepted", "declined"]), "reason": string(),
                                         "plannedDate": DAY, "template": string(enum=list(TEMPLATES))},
                                        True, (*OFFER, "response", "reason")),
    "onboarding.get": definition("查询入职清单", "按入职编号查询任务、期限、负责人和修订号。", {"onboardingId": string()}),
    "onboarding.list": definition("列出授权岗位的入职", "按负责人、状态或预计到岗日期查询。", {
        "owner": string(), "state": string(enum=["pending_arrival", "in_progress", "completed", "cancelled"]),
        "plannedDate": DAY}, required=()),
    "onboarding.reschedule": definition("修改预计到岗日", "使用当前修订号，重算未完成任务。", {**ONBOARDING, "plannedDate": DAY, "reason": string()}, True),
    "onboarding.record_arrival": definition("登记实际报到", "使用当前修订号与实际报到日。", {**ONBOARDING, "actualDate": DAY}, True),
    "onboarding.update_task": definition("更新入职待办", "使用清单当前修订号并记录办理依据或不适用原因。",
                                         {**ONBOARDING, "taskId": string(), "state": string(enum=["completed", "not_applicable"]),
                                          "evidence": string()}, True),
    "onboarding.close": definition("确认入职结案", "完成报到和必要任务后结案。", ONBOARDING, True),
    "onboarding.cancel": definition("确认取消入职", "登记候选人未到岗、撤回或取消原因。", {**ONBOARDING, "reason": string()}, True),
}


def offer_by_id(db, offer_id):
    row = db.execute("SELECT * FROM hr_offers WHERE id=?", (offer_id,)).fetchone()
    if not row:
        raise ServiceError("Offer 不存在或不可访问", 404)
    return row


def onboarding_by_id(db, onboarding_id):
    row = db.execute("SELECT * FROM hr_onboarding WHERE id=?", (onboarding_id,)).fetchone()
    if not row:
        raise ServiceError("入职记录不存在或不可访问", 404)
    return row


def offer_view(db, row):
    history = [dict(item) for item in db.execute(
        "SELECT version,terms,created_at FROM hr_offer_versions WHERE offer=? ORDER BY version", (row["id"],))]
    for item in history:
        item["terms"] = json.loads(item["terms"])
    onboarding = db.execute("SELECT id FROM hr_onboarding WHERE offer=?", (row["id"],)).fetchone()
    return {"offerId": row["id"], "applicationId": row["application"], "version": row["version"],
            "revision": row["revision"], "state": row["state"], "versions": history,
            "onboardingId": onboarding["id"] if onboarding else None}


def onboarding_view(db, row):
    tasks = [dict(task) for task in db.execute(
        """SELECT task AS taskId,title,owner,due,required,state,evidence FROM hr_onboarding_tasks
           WHERE onboarding=? ORDER BY due,task""", (row["id"],))]
    return {"onboardingId": row["id"], "offerId": row["offer"], "offerVersion": row["offer_version"],
            "owner": row["owner"], "state": row["state"], "plannedDate": row["planned"], "actualDate": row["actual"],
            "revision": row["revision"], "template": row["template"], "tasks": tasks}


def terms(application, planned=None):
    return {"jobTitle": application["title"], "candidateName": application["candidate_name"],
            "location": "上海（演示）", "compensation": "演示津贴 200 元/日", "plannedDate": planned, "fictional": True}


def application_id(db, action_name, params):
    if "applicationId" in params:
        return params["applicationId"]
    if action_name.startswith("offer."):
        return offer_by_id(db, params["offerId"])["application"]
    onboarding = onboarding_by_id(db, params["onboardingId"])
    return offer_by_id(db, onboarding["offer"])["application"]


def require_interview(db, application):
    event = latest_event(db, application)
    if not event or event["stage"] != "面试完成":
        raise ServiceError("请先完成该应聘单的面试", 409)


def due(planned, offset):
    return (date.fromisoformat(planned) + timedelta(days=offset)).isoformat()


def save_draft(db, application, params):
    row = db.execute("SELECT * FROM hr_offers WHERE application=?", (application["id"],)).fetchone()
    if (row["revision"] if row else 0) != params["expectedRevision"] or row and row["state"] == "accepted":
        raise ServiceError("Offer 已变化或已接受", 409)
    require_interview(db, application["id"])
    offer_id = row["id"] if row else identifier("OFR")
    version = row["version"] + 1 if row else 1
    if row:
        db.execute("UPDATE hr_offers SET version=?,revision=revision+1,state='draft' WHERE id=?", (version, offer_id))
    else:
        db.execute("INSERT INTO hr_offers VALUES (?,?,1,1,'draft')", (offer_id, application["id"]))
    db.execute("INSERT INTO hr_offer_versions VALUES (?,?,?,?)",
               (offer_id, version, json.dumps(terms(application, params["plannedDate"]), ensure_ascii=False), now()))
    return offer_view(db, offer_by_id(db, offer_id))


def record_offer(db, actor, action_name, params):
    row = offer_by_id(db, params["offerId"])
    if action_name == "offer.record_response" and params["response"] == "accepted":
        existing = db.execute("SELECT * FROM hr_onboarding WHERE offer=?", (row["id"],)).fetchone()
        if existing:
            if existing["state"] == "cancelled":
                raise ServiceError("入职已经取消，请查询原记录", 409)
            first_accept = db.execute(
                """SELECT parameters FROM hr_lifecycle_history WHERE application=? AND action='offer.record_response'
                   AND json_extract(parameters,'$.response')='accepted' ORDER BY id LIMIT 1""",
                (row["application"],)).fetchone()
            original = json.loads(first_accept["parameters"]) if first_accept else {}
            if all(original.get(key) == params.get(key) for key in ("version", "plannedDate", "template")):
                return offer_view(db, row)
            raise ServiceError("Offer 已交接，请查询原入职记录后改期", 409)
    if row["version"] != params["version"] or row["revision"] != params["revision"]:
        raise ServiceError("Offer 版本或记录已更新", 409)
    if action_name == "offer.record_sent" and row["state"] == "draft":
        state = "sent"
    elif action_name == "offer.record_response" and row["state"] == "sent":
        state = params["response"]
        if state == "accepted":
            planned, template = params.get("plannedDate"), params.get("template")
            if not planned or template not in TEMPLATES:
                raise ServiceError("登记接受需填写预计到岗日与入职模板")
            onboarding = identifier("ONB")
            db.execute("INSERT INTO hr_onboarding VALUES (?,?,?,?,?,NULL,?,1,'pending_arrival')",
                       (onboarding, row["id"], row["version"], template, planned, actor))
            db.executemany("INSERT INTO hr_onboarding_tasks VALUES (?,?,?,?,?,?,?,'pending',NULL)",
                           [(onboarding, key, title, actor, offset, due(planned, offset), required)
                            for key, title, offset, required in TEMPLATES[template]])
    else:
        raise ServiceError("Offer 当前状态不允许该动作", 409)
    db.execute("UPDATE hr_offers SET state=?,revision=revision+1 WHERE id=?", (state, row["id"]))
    return offer_view(db, offer_by_id(db, row["id"]))


def update_onboarding(db, actor, action_name, params):
    row = onboarding_by_id(db, params["onboardingId"])
    key = row["id"]
    if row["owner"] != actor:
        raise ServiceError("仅入职经办人可办理此清单", 403)
    if row["revision"] != params["revision"] or row["state"] not in ("pending_arrival", "in_progress"):
        raise ServiceError("入职记录已变化或已结束", 409)
    if action_name == "onboarding.reschedule":
        if row["state"] != "pending_arrival":
            raise ServiceError("已报到的入职无法改期", 409)
        planned = params["plannedDate"]
        db.execute("UPDATE hr_onboarding SET planned=? WHERE id=?", (planned, key))
        pending = db.execute("SELECT task,day_offset FROM hr_onboarding_tasks WHERE onboarding=? AND state='pending'", (key,))
        for task in pending.fetchall():
            db.execute("UPDATE hr_onboarding_tasks SET due=? WHERE onboarding=? AND task=?",
                       (due(planned, task["day_offset"]), key, task["task"]))
    elif action_name == "onboarding.record_arrival":
        if row["state"] != "pending_arrival":
            raise ServiceError("该员工已登记报到", 409)
        db.execute("UPDATE hr_onboarding SET actual=?,state='in_progress' WHERE id=?", (params["actualDate"], key))
    elif action_name == "onboarding.update_task":
        updated = db.execute("""UPDATE hr_onboarding_tasks SET state=?,evidence=?
                                WHERE onboarding=? AND task=? AND owner=? AND state='pending'""",
                             (params["state"], params["evidence"], key, params["taskId"], actor))
        if not updated.rowcount:
            raise ServiceError("待办已办理或不可访问", 409)
    elif action_name == "onboarding.close":
        pending = db.execute("SELECT 1 FROM hr_onboarding_tasks WHERE onboarding=? AND required=1 AND state='pending'", (key,))
        if row["state"] != "in_progress" or pending.fetchone():
            raise ServiceError("请先登记报到并完成必要待办", 409)
        db.execute("UPDATE hr_onboarding SET state='completed' WHERE id=?", (key,))
    elif action_name == "onboarding.cancel":
        db.execute("UPDATE hr_onboarding SET state='cancelled' WHERE id=?", (key,))
        db.execute("UPDATE hr_onboarding_tasks SET state='cancelled' WHERE onboarding=? AND state='pending'", (key,))
    db.execute("UPDATE hr_onboarding SET revision=revision+1 WHERE id=?", (key,))
    return onboarding_view(db, onboarding_by_id(db, key))


def operation_result(store, actor, action_name, operation):
    with store.connection() as db:
        saved = db.execute("SELECT * FROM hr_lifecycle_operations WHERE actor=? AND operation=? AND action=?",
                           (actor, operation, action_name)).fetchone()
        if saved:
            visible_application(db, actor, saved["application"])
            return json.loads(saved["result"])


def execute(store, actor, name, params, operation=None):
    write = ACTIONS[name]["kind"] == "write"
    with store.connection(write=write) as db:
        if name == "onboarding.list":
            rows = db.execute("""SELECT o.* FROM hr_onboarding o JOIN hr_offers f ON f.id=o.offer
                                 JOIN recruitment_applications a ON a.id=f.application
                                 JOIN recruitment_grants g ON g.job=a.job AND g.actor=? WHERE o.owner=? ORDER BY o.planned,o.id""", (actor, actor))
            filters = {"owner": "owner", "state": "state", "plannedDate": "planned"}
            return {"onboardings": [onboarding_view(db, row) for row in rows
                                    if all(key not in params or params[key] == row[field] for key, field in filters.items())]}
        app_id = application_id(db, name, params)
        application = visible_application(db, actor, app_id)
        if name == "offer.preview":
            require_interview(db, app_id)
            return {"applicationId": app_id, "terms": terms(application)}
        if name == "offer.get":
            row = db.execute("SELECT * FROM hr_offers WHERE application=?", (app_id,)).fetchone()
            return {"offer": offer_view(db, row) if row else None}
        if name == "onboarding.get":
            row = onboarding_by_id(db, params["onboardingId"])
            if row["owner"] != actor:
                raise ServiceError("入职记录不存在或不可访问", 404)
            return onboarding_view(db, row)
        encoded = json.dumps(params, ensure_ascii=False, sort_keys=True)
        saved = db.execute("SELECT * FROM hr_lifecycle_operations WHERE actor=? AND operation=?", (actor, operation)).fetchone()
        if saved:
            if saved["action"] != name or saved["input"] != encoded:
                raise ServiceError("同一操作的动作或参数不同", 409)
            return json.loads(saved["result"])
        if name == "offer.save_draft":
            result = save_draft(db, application, params)
        elif name.startswith("offer."):
            result = record_offer(db, actor, name, params)
        else:
            result = update_onboarding(db, actor, name, params)
        db.execute("INSERT INTO hr_lifecycle_history(application,actor,action,parameters,occurred_at) VALUES (?,?,?,?,?)",
                   (app_id, actor, name, encoded, now()))
        db.execute("INSERT INTO hr_lifecycle_operations VALUES (?,?,?,?,?,?)",
                   (actor, operation, name, app_id, encoded, json.dumps(result, ensure_ascii=False)))
        return result
