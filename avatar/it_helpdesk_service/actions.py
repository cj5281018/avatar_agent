from dataclasses import dataclass
from typing import Annotated, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from avatar_contract import VERSION

SLUG = "it-helpdesk"
CONTRACT_VERSION = "1.1.0"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetTicket(Model):
    ticketId: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64, pattern=r"\S")]


class ListTickets(Model):
    statusGroup: Optional[Literal["active", "finished"]] = Field(default=None, description="省略时查询全部状态")
    limit: int = Field(default=10, ge=1, le=20)

    @classmethod
    def model_json_schema(cls, **kwargs):
        schema = super().model_json_schema(**kwargs)
        field = schema["properties"]["statusGroup"]
        field.update(field.pop("anyOf")[0])
        field.pop("default", None)
        return schema


class CreateTicket(Model):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120, pattern=r"\S")]
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000, pattern=r"\S")]
    location: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120, pattern=r"\S")]


class Ticket(Model):
    ticketId: str
    title: str
    status: Literal["open", "in_progress", "resolved", "closed"]
    updatedAt: AwareDatetime


class GetResult(Model):
    ticket: Ticket


class ListResult(Model):
    tickets: list[Ticket]
    hasMore: bool


def get_ticket(handle, data, store):
    row = next(iter(store.tickets(handle, data.ticketId)), None)
    return GetResult(ticket=Ticket.model_validate_json(row)) if row else None


def list_tickets(handle, data, store):
    states = {"open", "in_progress"} if data.statusGroup == "active" else {"resolved", "closed"}
    owned = map(Ticket.model_validate_json, store.tickets(handle))
    tickets = [ticket for ticket in owned if data.statusGroup is None or ticket.status in states]
    tickets.sort(key=lambda ticket: ticket.ticketId)
    tickets.sort(key=lambda ticket: ticket.updatedAt, reverse=True)
    return ListResult(tickets=tickets[:data.limit], hasMore=len(tickets) > data.limit)


def ticket(number, title, status, day):
    return Ticket.model_validate_json(
        f'{{"ticketId":"IT-{number}","title":"{title}","status":"{status}","updatedAt":"2026-11-{day:02d}T09:00:00+08:00"}}'
    )


TICKETS = (
    ("marco", ticket(101, "办公电脑无法联网", "resolved", 12)),
    ("marco", ticket(103, "会议室投屏故障", "in_progress", 13)),
    ("bob", ticket(202, "打印机驱动异常", "open", 13)),
)


@dataclass(frozen=True)
class Action:
    title: str
    description: str
    input: type[Model]
    output: type[Model]
    handler: object
    examples: tuple[str, ...]
    roles: tuple[str, ...] = ("admin", "member")

    kind: str = "read"

    def manifest(self):
        return {"title": self.title, "description": self.description, "kind": self.kind, "roles": list(self.roles),
                "inputSchema": self.input.model_json_schema(), "outputSchema": self.output.model_json_schema(),
                "examples": list(self.examples),
                **({"confirmation": "required", "recovery": {"statusLookup": True, "replay": "same_operation"}}
                   if self.kind == "write" else {})}


def create_ticket(store, scope, handle, action, operation, data):
    return store.create_ticket(scope, handle, action, operation, data.model_dump())


ACTIONS = {
    "create_ticket": Action("提交 IT 报修", "收集地点和故障，生成报修确认卡；员工点击卡片后才建单。",
                            CreateTicket, GetResult, create_ticket,
                            ("三楼会议室那台电脑连不上网，帮我报修",), kind="write"),
    "get_ticket": Action("按编号查 IT 工单", "查询自己的工单状态。需要明确的工单编号。",
                         GetTicket, GetResult, get_ticket, ("IT-101 修好了吗？",)),
    "list_my_tickets": Action("列出自己的 IT 工单", "查询自己的 IT 报修。未处理或仍在处理中用 active，已结束用 finished。",
                              ListTickets, ListResult, list_tickets, ("我还有哪些报修没处理？",)),
}


def manifest(actions):
    return {"manifestVersion": 1, "protocolVersion": VERSION, "contractVersion": CONTRACT_VERSION,
            "slug": SLUG, "title": "IT 服务台", "description": "查询自己的 IT 报修工单与处理进度",
            "department": "IT", "actions": {name: action.manifest() for name, action in actions.items()}}
