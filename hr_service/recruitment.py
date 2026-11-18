"""Recruiting records owned by the HR service."""

import json
from datetime import date, datetime, timedelta, timezone

from .db import ServiceError, now, setting, set_setting

SHANGHAI = timezone(timedelta(hours=8))
STAGES = ("待筛选", "面试中", "面试完成")
SCHEMA = """
CREATE TABLE IF NOT EXISTS recruitment_jobs (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, location TEXT, duties TEXT NOT NULL,
    requirements TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recruitment_grants (
    actor TEXT NOT NULL, job TEXT NOT NULL REFERENCES recruitment_jobs(id), PRIMARY KEY(actor, job)
);
CREATE TABLE IF NOT EXISTS recruitment_applications (
    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, candidate_name TEXT NOT NULL,
    job TEXT NOT NULL REFERENCES recruitment_jobs(id), owner TEXT NOT NULL,
    next_step TEXT, due_at INTEGER
);
CREATE TABLE IF NOT EXISTS recruitment_events (
    id INTEGER PRIMARY KEY, application TEXT NOT NULL REFERENCES recruitment_applications(id),
    stage TEXT NOT NULL, occurred_at INTEGER NOT NULL, actor TEXT, reason TEXT
);
CREATE INDEX IF NOT EXISTS recruitment_events_application ON recruitment_events(application, occurred_at, id);
CREATE TABLE IF NOT EXISTS recruitment_operations (
    actor TEXT NOT NULL, operation TEXT NOT NULL, application TEXT NOT NULL REFERENCES recruitment_applications(id),
    input TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(actor, operation)
);
"""


def visible_application(db, actor, application):
    row = db.execute("""SELECT a.*, j.title FROM recruitment_applications a
                        JOIN recruitment_jobs j ON j.id=a.job
                        JOIN recruitment_grants g ON g.job=a.job AND g.actor=?
                        WHERE a.id=?""", (actor, application)).fetchone()
    if row is None:
        raise ServiceError("应聘记录不存在或不可访问", 404)
    return row


def latest_event(db, application, before=None):
    return db.execute("""SELECT * FROM recruitment_events WHERE application=?
                         AND (? IS NULL OR occurred_at < ?) ORDER BY occurred_at DESC,id DESC LIMIT 1""",
                      (application, before, before)).fetchone()


def application_view(row, event, as_of):
    return {"applicationId": row["id"], "candidateName": row["candidate_name"], "jobId": row["job"],
            "jobTitle": row["title"], "owner": row["owner"], "nextStep": row["next_step"],
            "dueAt": row["due_at"], "overdue": bool(row["next_step"] and row["due_at"] is not None
                                                    and row["due_at"] < as_of),
            "stage": event["stage"], "revision": event["id"], "updatedAt": event["occurred_at"]}


def applications(db, actor, job, candidate, owner):
    return db.execute("""SELECT a.*, j.title FROM recruitment_applications a
                         JOIN recruitment_jobs j ON j.id=a.job
                         JOIN recruitment_grants g ON g.job=a.job AND g.actor=?
                         WHERE (? IS NULL OR a.job=?) AND (? IS NULL OR a.candidate_name=?)
                         AND (? IS NULL OR a.owner=?) ORDER BY a.id""",
                      (actor, job, job, candidate, candidate, owner, owner)).fetchall()


def list_applications(store, actor, as_of, job=None, candidate=None, owner=None, stage=None):
    with store.connection() as db:
        result = []
        for row in applications(db, actor, job, candidate, owner):
            event = latest_event(db, row["id"])
            if event and (stage is None or event["stage"] == stage):
                result.append(application_view(row, event, as_of))
        return result


def job_facts(store, actor, job):
    with store.connection() as db:
        row = db.execute("""SELECT j.* FROM recruitment_jobs j JOIN recruitment_grants g ON g.job=j.id
                            WHERE j.id=? AND g.actor=?""", (job, actor)).fetchone()
        if not row:
            raise ServiceError("岗位不存在或不可访问", 404)
        return dict(row)


def operation_result(store, actor, operation):
    with store.connection() as db:
        previous = db.execute("SELECT * FROM recruitment_operations WHERE actor=? AND operation=?",
                              (actor, operation)).fetchone()
        if previous:
            visible_application(db, actor, previous["application"])
            return json.loads(previous["result"])


def advance(store, actor, operation, params, now):
    with store.connection(write=True) as db:
        row = visible_application(db, actor, params["applicationId"])
        encoded = json.dumps(params, sort_keys=True, ensure_ascii=False)
        previous = db.execute("SELECT * FROM recruitment_operations WHERE actor=? AND operation=?",
                              (actor, operation)).fetchone()
        if previous:
            if previous["input"] != encoded:
                raise ServiceError("同一操作的参数已变化", 409)
            return json.loads(previous["result"])
        latest = latest_event(db, row["id"])
        if (latest is None or latest["id"] != params["revision"] or latest["stage"] != params["fromStage"]
                or row["title"] != params["jobTitle"]):
            raise ServiceError("记录已更新，请重新查看", 409)
        index = STAGES.index(latest["stage"])
        if index + 1 >= len(STAGES) or STAGES[index + 1] != params["toStage"]:
            raise ServiceError("只能推进到下一阶段", 409)
        at = max(now, latest["occurred_at"])
        db.execute("INSERT INTO recruitment_events(application,stage,occurred_at,actor,reason) VALUES (?,?,?,?,?)",
                   (row["id"], params["toStage"], at, actor, params["reason"]))
        db.execute("UPDATE recruitment_applications SET next_step=NULL,due_at=NULL WHERE id=?", (row["id"],))
        row = visible_application(db, actor, row["id"])
        result = application_view(row, latest_event(db, row["id"]), at)
        db.execute("INSERT INTO recruitment_operations VALUES (?,?,?,?,?)",
                   (actor, operation, row["id"], encoded, json.dumps(result, ensure_ascii=False)))
        return result


def weekly_report(store, actor, monday, now, job=None):
    start = datetime.combine(monday, datetime.min.time(), SHANGHAI)
    if start.weekday() != 0:
        raise ServiceError("请选择该周的周一日期")
    lower = int(start.timestamp() * 1000)
    upper = int((start + timedelta(days=7)).timestamp() * 1000)
    if lower > now:
        raise ServiceError("统计周尚未开始")
    cutoff = min(upper, now + 1)
    totals = dict.fromkeys(STAGES, 0)
    interviews = 0
    overdue = []
    with store.connection() as db:
        jobs = [r[0] for r in db.execute("""SELECT job FROM recruitment_grants WHERE actor=?
                                          AND (? IS NULL OR job=?) ORDER BY job""", (actor, job, job))]
        if job and not jobs:
            raise ServiceError("岗位不存在或不可访问", 404)
        for row in applications(db, actor, job, None, None):
            event = latest_event(db, row["id"], cutoff)
            if event is None:
                continue
            totals[event["stage"]] += 1
            entered = db.execute("""SELECT 1 FROM recruitment_events WHERE application=? AND stage=?
                                     AND occurred_at>=? AND occurred_at<? LIMIT 1""",
                                 (row["id"], "面试中", lower, cutoff)).fetchone()
            interviews += int(entered is not None)
            current = application_view(row, latest_event(db, row["id"]), now)
            if current["overdue"]:
                overdue.append(current)
    return {"jobIds": jobs, "startAt": lower, "endAt": upper, "cutoffExclusive": cutoff,
            "stageCounts": totals, "enteredInterview": interviews, "currentOverdue": overdue,
            "currentOverdueAsOf": now, "hasRecords": bool(sum(totals.values()))}


def seed(store):
    with store.connection(write=True) as db:
        if setting(db, "recruitment.demo.seeded"):
            return
        db.executemany("INSERT INTO recruitment_jobs VALUES (?,?,?,?,?)", (
            ("JOB-PY", "Python 实习生", None, "协助维护 Python 服务", "能编写和测试 Python 程序"),
            ("JOB-OPS", "运营实习生", "上海", "整理运营数据", "能使用表格核对数据"),
        ))
        db.executemany("INSERT INTO recruitment_grants VALUES (?,?)", (
            ("bob", "JOB-PY"), ("carol", "JOB-PY"), ("carol", "JOB-OPS"),
        ))
        day = 24 * 60 * 60 * 1000
        current = now()
        db.executemany("INSERT INTO recruitment_applications VALUES (?,?,?,?,?,?,?)", (
            ("APP-01", "CAND-01", "小林", "JOB-PY", "bob", "完成初筛", current - day),
            ("APP-02", "CAND-02", "小周", "JOB-PY", "bob", "记录面试反馈", current + day),
            ("APP-03", "CAND-01", "小林", "JOB-OPS", "carol", "安排下一步", None),
        ))
        db.executemany("INSERT INTO recruitment_events(application,stage,occurred_at) VALUES (?,?,?)", (
            ("APP-01", STAGES[0], current - 2 * day),
            ("APP-02", STAGES[0], current - 3 * day),
            ("APP-02", STAGES[1], current - day),
            ("APP-03", STAGES[0], current - day),
        ))
        set_setting(db, "recruitment.demo.seeded", "1")


def string(**options):
    return {"type": "string", "minLength": 1, "maxLength": 200, **options}


def action(title, description, properties, required=(), write=False):
    return {"title": title, "description": description, "kind": "write" if write else "read",
            "roles": ["member", "admin"], "inputSchema": {"type": "object", "additionalProperties": False,
                                                          "properties": properties, "required": list(required)},
            "outputSchema": {"type": "object"},
            **({"confirmation": "required", "recovery": {"statusLookup": True, "replay": "same_operation"}}
               if write else {})}


ADVANCE = "recruitment.advance"
ACTIONS = {
    "recruitment.list": action("查招聘应聘进度与待办", "查询授权岗位的应聘进度、负责人、截止及下一步。推进前先查，按候选人姓名寻找匹配。",
                               {"jobId": string(), "candidateName": string(), "owner": string(),
                                "stage": string(enum=list(STAGES))}),
    "recruitment.jd": action("生成招聘 JD 草稿", "需要岗位 ID。用返回的岗位事实和模板组织可复制 JD，标出 missingFields，勿补写未记录信息。",
                             {"jobId": string()}, ("jobId",)),
    ADVANCE: action("确认推进应聘阶段", "先查询应聘进度，只有一个明确匹配才提案。原样使用记录返回的岗位标题、版本和阶段；" 
                    "有多个匹配时先问清岗位。正式修改需要用户在桌面端确认。",
                    {"applicationId": string(), "jobTitle": string(), "revision": {"type": "integer", "minimum": 1},
                     "fromStage": string(enum=list(STAGES)), "toStage": string(enum=list(STAGES)), "reason": string()},
                    ("applicationId", "jobTitle", "revision", "fromStage", "toStage", "reason"), write=True),
    "recruitment.weekly": action("生成招聘周报", "返回北京时间的统计区间、各阶段存量、当周进面数和当前逾期。按返回数字写摘要。"
                                 "省略 weekStart 表示本周，指定时提供该周周一日期。",
                                 {"jobId": string(), "weekStart": string(format="date")}),
}

LEGACY_ACTIONS = {
    "list_my_requests": "查询本人的请假申请与进度",
    "list_pending_approvals": "查询分配给本人的待审批请假单",
    "get_approvers": "查询可选的请假审批人",
    "request_leave": "准备请假申请，使用桌面端确认卡提交",
    "review_leave": "准备审批已有假单，使用桌面端确认卡提交",
}


def manifest():
    return {"manifestVersion": 1, "protocolVersion": "avatar-service/v1", "contractVersion": "1.0.0",
            "slug": "hr", "title": "人力资源", "description": "人力资源部门的请假和招聘服务",
            "department": "HR", "actions": ACTIONS, "leaveActions": LEGACY_ACTIONS}


def execute(store, actor, name, params, operation=None):
    current = now()
    if name == ADVANCE:
        return advance(store, actor, operation, params, current)
    if name == "recruitment.jd":
        facts = job_facts(store, actor, params["jobId"])
        return {"facts": facts, "template": ["岗位名称", "工作地点", "岗位职责", "任职要求"],
                "missingFields": ["工作地点"] if not facts["location"] else []}
    if name == "recruitment.weekly":
        today = datetime.fromtimestamp(current / 1000, SHANGHAI).date()
        monday = date.fromisoformat(params["weekStart"]) if "weekStart" in params else today - timedelta(days=today.weekday())
        return weekly_report(store, actor, monday, current, params.get("jobId"))
    if name == "recruitment.list":
        if "jobId" in params:
            job_facts(store, actor, params["jobId"])
        return {"applications": list_applications(store, actor, current, params.get("jobId"),
                                                   params.get("candidateName"), params.get("owner"), params.get("stage"))}
    raise ServiceError("招聘动作不存在", 404)
