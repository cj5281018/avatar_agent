"""聊天流式 API 路由。

从早期单体模块里抽出来的“薄路由层”：只负责请求准备工作
（查会话、装配工具注册表、处理压缩、解析模型），
真正的流式生成与持久化都交给 ``services/ai/streaming/`` 下的模块完成。
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from dataclasses import dataclass
from typing import Any, cast

import httpx
from anthropic.types import (
    ContentBlockParam,
    MessageParam,
    TextBlockParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agents.executor import _build_source_filter
from agents.models import Agent
from agents.repository import AgentRepository, AgentRunRepository
from attachments import expand_mentions, expand_uploads
from config import (
    AGENT_MAX_ITERATIONS,
    CONNECTOR_MANAGER_URL,
    DEFAULT_MAX_TOKENS,
    SANDBOX_URL,
)
from db import (
    ChatsRepository,
    CompactionsRepository,
    MessagesRepository,
    ProjectAttachmentsRepository,
    ProjectsRepository,
    SkillsRepository,
)
from db.configuration import ConfigurationRepository
from db.documents import DocumentsRepository
from db.groups import GroupRepository
from db.models import (
    Chat,
    ProjectAttachmentType,
    Source,
    UserConfiguration,
    filter_sources_for_user,
)
from db.tool_approvals import (
    ToolApproval,
    ToolApprovalsRepository,
    ToolApprovalStatus,
    ToolApprovalType,
)
from db.uploads import UploadsRepository
from db.usage import UsageRepository
from db.users import UsersRepository
from memory import (
    MemoryMode,
    agent_key,
    resolve_memory_mode,
    user_key,
)
from prompts import build_agent_chat_system_prompt, build_chat_system_prompt
from provider_cache import ResolvedModel
from providers import LLMProvider
from schemas.api import SteeringMessageRequest
from services.compaction import ConversationCompactor
from services.title_generation import generate_title_for_conversation
from services.usage import UsageContext, UsagePurpose, UsageTracker, track_usage
from state import AppState
from streaming.generate import (
    active_path_tool_call_ids,
    drop_empty_assistant_messages,
    message_content_blocks,
    prepare_and_stream_chat,
    repair_interrupted_tool_calls,
    resumable_batch_ids_for_interventions,
    unanswered_tool_calls,
)
from streaming.persist import (
    EndOfStreamReason,
    end_of_stream,
    persist_and_transform,
)
from streaming.run import (
    _CANCEL_TTL,
    _RUN_LOCK_TTL,
    SSE_HEADERS,
    _run_tasks_by_chat,
    cancel_key,
    clear_producer_task,
    consume_run,
    enqueue_steering_message,
    run_lock_key,
    run_producer,
    set_producer_task,
    steering_queue_has_pending,
    stream_key,
)
from tools import (
    ChatHistoryToolHandler,
    ConnectorToolHandler,
    DocumentToolHandler,
    PeopleSearchHandler,
    SearchToolHandler,
    ToolContext,
    ToolHandler,
    ToolRegistry,
    WebToolHandler,
)
from tools.connector_handler import (
    SearchOperator,
    ToolsetSummary,
    fetch_active_sources_from_connector_manager,
)
from tools.mcp_capability_handler import McpCapabilityHandler
from tools.meta_handler import MetaToolHandler, OnLoad, exact_tool_names_for_query
from tools.omni_tool_result import OAuthRequiredPayload
from tools.sandbox_handler import SandboxToolHandler
from tools.search_handler import fetch_operator_values
from tools.skill_handler import SkillHandler
from tools.avatar2_handler import Avatar2ToolHandler
from tools.traced_handler import TracedToolHandler
from tools.turn_builder import build_turn_tools
from tracing.tee import traced_turn

router = APIRouter(tags=["chat"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模型 Provider 解析辅助函数
#
# 这一组只负责“从 provider_cache 里拿到本次要用的模型”。真正调用模型发生在
# streaming/generate.py；这里的职责是尽早发现配置问题，并给出明确的错误，
# 而不是带着一个坏掉的配置继续往下跑。
# ---------------------------------------------------------------------------


async def _resolve_llm_provider(state: AppState, chat: Chat) -> ResolvedModel:
    """从数据库解析这个会话要用的模型 provider。

    如果会话配置的模型已经不存在，这里会“大声失败”（返回 409，让用户重新选模型），
    而不是悄悄换成另一个模型继续回答——否则用户会以为用的还是原来的模型，
    输出风格、上下文长度等行为都可能对不上。
    """
    if not chat.model_id:
        raise HTTPException(status_code=503, detail="Chat has no model configured")

    resolved = await state.provider_cache.resolve_for_model(chat.model_id)
    if resolved is None:
        raise HTTPException(
            status_code=409,
            detail="This chat's model is no longer available. Please select a new model.",
        )
    return resolved


async def _resolve_secondary_provider(state: AppState) -> ResolvedModel:
    """解析次要（轻量）模型 provider，用于压缩对话、生成标题等辅助任务。"""
    resolved = await state.provider_cache.resolve_secondary_or_default()
    if resolved is None:
        raise HTTPException(
            status_code=503, detail="No models configured — cannot proceed"
        )
    return resolved


# ---------------------------------------------------------------------------
# 工具注册表装配辅助函数
#
# 一次聊天能调用哪些工具，取决于三件事：用户能看到哪些数据源（sources）、
# 配置了哪些可选能力（MCP / 沙箱 / 联网 / 技能）、以及历史对话里已经“加载”
# 过哪些连接器工具。下面这些函数就是把这些信息拼成一个 ToolRegistry。
# ---------------------------------------------------------------------------


@dataclass
class RegistryResult:
    """工具注册表装配结果。

    除了 registry 本身，还带上装配过程产生的“副产品”：
    - always_on_handlers：每轮都要暴露给模型的工具（搜索、文档、历史等）；
    - connector_handler / toolsets：连接器工具及其分组，供 meta 工具和提示词使用；
    - sources / sources_fetch_succeeded：可见数据源，以及“到底有没有拉取成功”——
      后者用来区分“确实没有这个数据源”和“只是这次没拉到”，避免误删审批项；
    - search_operators：连接器声明的搜索操作符，用于 query 里的内联过滤语法。
    """

    registry: ToolRegistry
    always_on_handlers: list[ToolHandler]
    connector_handler: ConnectorToolHandler | None
    toolsets: list[ToolsetSummary]
    sources: list[Source]
    search_operators: list[SearchOperator]
    sources_fetch_succeeded: bool = False


def _loaded_tools_from_history(
    messages: list[MessageParam], connector_handler: ConnectorToolHandler
) -> set[str]:
    """从历史消息还原“当前已经加载过哪些连接器工具”。

    连接器工具是按需加载的：模型先调用 tool_search / load_tool，
    之后才会出现真正的业务工具调用。续聊时如果不还原这个状态，
    模型会“忘记”自己之前加载过什么，表现为工具突然消失。
    做法：先按 id 收集所有 tool_use，再看每条成功的 tool_result 对应哪个调用；
    如果那个调用是 meta 工具，就按它的参数换算出被加载的工具名。
    失败的调用（is_error）不计入。
    """
    tool_calls: dict[str, ToolUseBlockParam] = {}
    loaded: set[str] = set()

    for message in messages:
        for block in message_content_blocks(message):
            match block["type"]:
                case "tool_use":
                    tool_use = cast(ToolUseBlockParam, block)
                    tool_calls[tool_use["id"]] = tool_use
                case "tool_result":
                    tool_result = cast(ToolResultBlockParam, block)
                    if tool_result.get("is_error", False):
                        continue
                    call = tool_calls.get(tool_result["tool_use_id"])
                    if call is None:
                        continue
                    loaded.update(
                        _loaded_tools_from_meta_call(
                            call["name"], call["input"], connector_handler
                        )
                    )
                case _:
                    continue

    return loaded


def _loaded_tools_from_meta_call(
    tool_name: str,
    tool_input: dict[str, object],
    connector_handler: ConnectorToolHandler,
) -> set[str]:
    """把一次 meta 工具调用翻译成“它到底加载了哪些工具”。

    - tool_search：按查询词做精确匹配，返回匹配到的工具名；
    - load_tool：加载指定工具；
    - load_tool_set：按 source_id 或 source_type 加载整组工具。
    其他工具调用一律返回空集合。
    """
    if tool_name == "tool_search":
        query = tool_input.get("query")
        if isinstance(query, str):
            return exact_tool_names_for_query(query, connector_handler.actions)
    if tool_name == "load_tool":
        requested = tool_input.get("tool_name")
        if isinstance(requested, str) and requested in connector_handler.actions:
            return {requested}
    if tool_name == "load_tool_set":
        source_id = tool_input.get("source_id")
        if isinstance(source_id, str) and source_id:
            return {
                name
                for name, action in connector_handler.actions.items()
                if action.source_id == source_id
            }
        source_type = tool_input.get("source_type")
        if isinstance(source_type, str) and source_type:
            return {
                name
                for name, action in connector_handler.actions.items()
                if action.source_type == source_type
            }
    return set()


def _loaded_source_ids(
    loaded_tool_names: set[str], connector_handler: ConnectorToolHandler | None
) -> set[str]:
    """把已加载的工具名反查成 source_id，供系统提示词描述数据源使用。"""
    if connector_handler is None:
        return set()
    return {
        action.source_id
        for tool_name, action in connector_handler.actions.items()
        if tool_name in loaded_tool_names
    }


def _inaccessible_intervention_ids(
    interventions: list[ToolApproval],
    visible_source_ids: set[str],
    *,
    sources_fetch_succeeded: bool,
) -> set[str]:
    """找出本次响应里应该隐藏的、绑定了数据源的审批项。

    数据源拉取失败时按“不可见”处理（fail-closed），但这只能说明“这次没查到”，
    不能证明数据源真的被删了，所以调用方不能因此把这些审批项标记为过期——
    否则一次网络抖动就会把用户尚未处理的审批悄悄作废。
    """
    return {
        intervention.id
        for intervention in interventions
        if intervention.source_id is not None
        and (
            not sources_fetch_succeeded
            or intervention.source_id not in visible_source_ids
        )
    }


def _intervention_ids_to_expire(
    interventions: list[ToolApproval],
    visible_source_ids: set[str],
    *,
    sources_fetch_succeeded: bool,
) -> set[str]:
    """找出可以安全标记为“已过期”的审批项。

    只有确认数据源列表拉取成功时，才能断定某个绑定了数据源的审批项已经失效；
    拉取失败时一律返回空集合，宁可让审批项多留一会儿，也不要误作废。
    """
    if not sources_fetch_succeeded:
        return set()
    return _inaccessible_intervention_ids(
        interventions,
        visible_source_ids,
        sources_fetch_succeeded=True,
    )


async def _fetch_sources_from_connector_manager() -> list[Source] | None:
    """从 connector-manager 拉取当前活跃的数据源。

    失败返回 None 而不是空列表：调用方需要区分“用户确实没有数据源”
    （可以安全清理）和“这次请求失败了”（不能据此做清理）。
    """
    try:
        return await fetch_active_sources_from_connector_manager(CONNECTOR_MANAGER_URL)
    except Exception as e:
        logger.warning(f"Failed to fetch sources from connector manager: {e}")
        return None


async def _noop_on_load(_: set[str]) -> None:
    """MetaToolHandler 的默认“工具已加载”回调：不需要额外动作时用它占位。"""
    return None


def _last_user_message_id(chat_messages: list) -> str | None:
    """本轮问答所回答的用户消息，用于把执行记录挂回那条提问。"""
    for entry in reversed(chat_messages):
        message = getattr(entry, "message", None)
        if isinstance(message, dict) and message.get("role") == "user":
            return entry.id
    return None


async def _build_registry(
    request: Request,
    chat: Chat,
    is_admin: bool,
    loaded_toolsets: set[str],
    on_load: OnLoad | None = None,
) -> RegistryResult:
    """为普通（非 agent）会话装配完整的工具注册表。

    装配顺序大致按“用户能感知到的能力”排列：连接器（业务数据）→ meta（按需发现工具）
    → MCP → 搜索 → 联网 → 人员 → 聊天历史 → 文档 → 沙箱 → 技能。
    always_on_handlers 里的工具每轮都会暴露给模型；其余工具按条件注册。
    loaded_toolsets 是历史里已经加载过的连接器工具集合，用来恢复 meta 工具的状态。
    """
    registry = ToolRegistry()
    always_on_handlers: list[ToolHandler] = []

    avatar2_handler = await Avatar2ToolHandler.discover(chat.user_id)
    if avatar2_handler is not None:
        # 记录工具调用起点与耗时；注册表与主循环不感知追踪。
        traced_avatar2_handler = TracedToolHandler(avatar2_handler)
        registry.register(traced_avatar2_handler)
        always_on_handlers.append(traced_avatar2_handler)

    fetched_sources = await _fetch_sources_from_connector_manager()
    sources_fetch_succeeded = fetched_sources is not None
    sources = filter_sources_for_user(fetched_sources or [], chat.user_id)

    connector_handler: ConnectorToolHandler | None = None
    toolsets: list[ToolsetSummary] = []
    search_operators: list[SearchOperator] = []

    connector_handler = ConnectorToolHandler(
        connector_manager_url=CONNECTOR_MANAGER_URL,
        user_id=chat.user_id,
        prefetched_sources=sources,
        documents_repo=DocumentsRepository(),
        sandbox_url=SANDBOX_URL,
        is_admin=is_admin,
    )
    await connector_handler._ensure_initialized()
    registry.register(connector_handler)

    if connector_handler.actions:
        toolsets = connector_handler.list_toolsets()

    if connector_handler.search_operators:
        search_operators = connector_handler.search_operators

    if connector_handler is not None and toolsets:
        meta_handler = MetaToolHandler(
            connector_handler=connector_handler,
            loaded=loaded_toolsets,
            on_load=on_load or _noop_on_load,
            searcher_client=request.app.state.searcher_tool.client,
        )
        await meta_handler.publish_tool_capabilities()
        registry.register(meta_handler)
        always_on_handlers.append(meta_handler)

    mcp_handler = McpCapabilityHandler(
        connector_manager_url=CONNECTOR_MANAGER_URL,
        searcher_client=request.app.state.searcher_tool.client,
        prefetched_sources=sources,
        prefetched_connectors=connector_handler.connector_catalog,
        user_id=chat.user_id,
    )
    await mcp_handler.refresh()
    if mcp_handler.has_capabilities():
        await mcp_handler.publish_capabilities()
        registry.register(mcp_handler)
        always_on_handlers.append(mcp_handler)

    active_sources = [s for s in sources if s.is_active and not s.is_deleted]
    connected_source_types = list({s.source_type for s in active_sources})
    operator_values: dict[str, list[str]] = {}
    if search_operators:
        operator_values = await fetch_operator_values(
            request.app.state.searcher_tool.client,
            search_operators,
            redis_client=request.app.state.redis_client,
        )

    search_handler = SearchToolHandler(
        searcher_tool=request.app.state.searcher_tool,
        search_operators=search_operators,
        connected_source_types=connected_source_types,
        operator_values=operator_values,
    )
    registry.register(TracedToolHandler(search_handler))
    always_on_handlers.append(search_handler)

    web_search_provider = getattr(request.app.state, "web_search_provider", None)
    if web_search_provider is not None:
        web_handler = WebToolHandler(
            search_provider=web_search_provider,
            fetch_provider=getattr(request.app.state, "web_fetch_provider", None),
        )
        registry.register(web_handler)
        always_on_handlers.append(web_handler)

    people_handler = PeopleSearchHandler(searcher_tool=request.app.state.searcher_tool)
    registry.register(people_handler)
    always_on_handlers.append(people_handler)

    chat_history_handler = ChatHistoryToolHandler(
        chats_repo=ChatsRepository(),
        messages_repo=MessagesRepository(),
    )
    registry.register(chat_history_handler)
    always_on_handlers.append(chat_history_handler)

    content_storage = getattr(request.app.state, "content_storage", None)
    document_handler = DocumentToolHandler(
        content_storage=content_storage,
        documents_repo=DocumentsRepository(),
        sandbox_url=SANDBOX_URL,
        connector_manager_url=CONNECTOR_MANAGER_URL,
    )
    registry.register(document_handler)
    always_on_handlers.append(document_handler)

    if SANDBOX_URL:
        sandbox_handler = SandboxToolHandler(sandbox_url=SANDBOX_URL)
        registry.register(sandbox_handler)
        always_on_handlers.append(sandbox_handler)

    skills_dir = pathlib.Path(__file__).resolve().parent.parent / "skills"
    skill_handler = SkillHandler(
        skills_dir=skills_dir,
        searcher_client=request.app.state.searcher_tool.client,
        connector_manager_url=CONNECTOR_MANAGER_URL,
        skills_repository=SkillsRepository(),
        skill_user_id=chat.user_id,
        allowed_source_ids={
            source.id for source in sources if source.is_active and not source.is_deleted
        },
    )
    await skill_handler.refresh_library_skills()
    await skill_handler.refresh_connector_skills()
    if skill_handler.has_skills():
        await skill_handler.publish_skill_capabilities()
        registry.register(skill_handler)
        always_on_handlers.append(skill_handler)

    return RegistryResult(
        registry=registry,
        always_on_handlers=always_on_handlers,
        connector_handler=connector_handler,
        toolsets=toolsets,
        sources=sources,
        sources_fetch_succeeded=sources_fetch_succeeded,
        search_operators=search_operators,
    )


async def _load_project_context(
    chat: Chat,
) -> tuple[str | None, list[str] | None]:
    """解析项目会话的项目指令与上下文引用。

    返回 (instructions, context_lines)。当会话不属于任何项目，或项目不存在、
    不属于当前用户时，两者都是 None——此时聊天照常进行，只是没有项目上下文，
    而不是直接报错，避免项目被删后整个会话不可用。
    """
    if not chat.project_id:
        return None, None

    project = await ProjectsRepository().get(chat.project_id)
    if not project or project.user_id != chat.user_id:
        return None, None

    attachments = await ProjectAttachmentsRepository().list_for_project(project.id)
    document_ids = [
        a.document_id
        for a in attachments
        if a.attachment_type == ProjectAttachmentType.DOCUMENT and a.document_id
    ]
    context_lines: list[str] = []
    if document_ids:
        documents = await DocumentsRepository().get_by_ids(document_ids)
        for document_id in document_ids:
            document = documents.get(document_id)
            if document:
                title = document.title or "Untitled document"
                context_lines.append(f"{title} [_ref:{document.id}]")

    return project.instructions, (context_lines or None)


async def _build_agent_chat_registry(
    request: Request, agent: Agent, is_admin: bool, user_id: str
) -> RegistryResult:
    """为 agent 会话装配工具注册表。

    与普通会话的区别：不注册 meta 工具（agent 的工具集在创建时就固定了），
    并且 user 类型的 agent 会把 source_filter 传给连接器和 MCP，
    保证 agent 只能访问它被授权的那部分数据源。
    """
    registry = ToolRegistry()
    always_on_handlers: list[ToolHandler] = []

    fetched_sources = await _fetch_sources_from_connector_manager()
    sources_fetch_succeeded = fetched_sources is not None
    sources = filter_sources_for_user(fetched_sources or [], user_id)

    source_filter = _build_source_filter(agent) if agent.agent_type == "user" else None

    search_operators: list[SearchOperator] = []
    connector_handler = ConnectorToolHandler(
        connector_manager_url=CONNECTOR_MANAGER_URL,
        user_id=user_id,
        prefetched_sources=sources,
        source_filter=source_filter,
        documents_repo=DocumentsRepository(),
        is_admin=is_admin,
    )
    await connector_handler._ensure_initialized()
    if connector_handler.search_operators:
        search_operators = connector_handler.search_operators

    mcp_handler = McpCapabilityHandler(
        connector_manager_url=CONNECTOR_MANAGER_URL,
        searcher_client=request.app.state.searcher_tool.client,
        prefetched_sources=sources,
        prefetched_connectors=connector_handler.connector_catalog,
        source_filter=source_filter,
        user_id=user_id,
    )
    await mcp_handler.refresh()
    if mcp_handler.has_capabilities():
        await mcp_handler.publish_capabilities()
        registry.register(mcp_handler)
        always_on_handlers.append(mcp_handler)

    active_sources = [s for s in sources if s.is_active and not s.is_deleted]
    connected_source_types = list({s.source_type for s in active_sources})
    operator_values: dict[str, list[str]] = {}
    if search_operators:
        operator_values = await fetch_operator_values(
            request.app.state.searcher_tool.client,
            search_operators,
            redis_client=request.app.state.redis_client,
        )

    search_handler = SearchToolHandler(
        searcher_tool=request.app.state.searcher_tool,
        search_operators=search_operators,
        connected_source_types=connected_source_types,
        operator_values=operator_values,
    )
    registry.register(TracedToolHandler(search_handler))
    always_on_handlers.append(search_handler)

    web_search_provider = getattr(request.app.state, "web_search_provider", None)
    if web_search_provider is not None:
        web_handler = WebToolHandler(
            search_provider=web_search_provider,
            fetch_provider=getattr(request.app.state, "web_fetch_provider", None),
        )
        registry.register(web_handler)
        always_on_handlers.append(web_handler)

    people_handler = PeopleSearchHandler(searcher_tool=request.app.state.searcher_tool)
    registry.register(people_handler)
    always_on_handlers.append(people_handler)

    chat_history_handler = ChatHistoryToolHandler(
        chats_repo=ChatsRepository(),
        messages_repo=MessagesRepository(),
    )
    registry.register(chat_history_handler)
    always_on_handlers.append(chat_history_handler)

    content_storage = getattr(request.app.state, "content_storage", None)
    document_handler = DocumentToolHandler(
        content_storage=content_storage,
        documents_repo=DocumentsRepository(),
        sandbox_url=SANDBOX_URL,
        connector_manager_url=CONNECTOR_MANAGER_URL,
    )
    registry.register(document_handler)
    always_on_handlers.append(document_handler)

    if SANDBOX_URL:
        sandbox_handler = SandboxToolHandler(sandbox_url=SANDBOX_URL)
        registry.register(sandbox_handler)
        always_on_handlers.append(sandbox_handler)

    skills_dir = pathlib.Path(__file__).resolve().parent.parent / "skills"
    skill_handler = SkillHandler(
        skills_dir=skills_dir,
        searcher_client=request.app.state.searcher_tool.client,
        connector_manager_url=CONNECTOR_MANAGER_URL,
        skills_repository=SkillsRepository(),
        skill_user_id=agent.user_id,
        allowed_source_ids={
            source.id for source in sources if source.is_active and not source.is_deleted
        },
    )
    await skill_handler.refresh_library_skills()
    await skill_handler.refresh_connector_skills()
    if skill_handler.has_skills():
        await skill_handler.publish_skill_capabilities()
        registry.register(skill_handler)
        always_on_handlers.append(skill_handler)

    return RegistryResult(
        registry=registry,
        always_on_handlers=always_on_handlers,
        connector_handler=None,
        toolsets=[],
        sources=sources,
        sources_fetch_succeeded=sources_fetch_succeeded,
        search_operators=search_operators,
    )


# ---------------------------------------------------------------------------
# 标题生成辅助函数
# ---------------------------------------------------------------------------


def _extract_text_for_title(
    content: str | list[ContentBlockParam] | None,
) -> str | None:
    """从一条消息的 content 里提取纯文本，供生成会话标题使用。

    兼容两种形态：纯字符串，以及内容块列表（只取 type == "text" 的块）。
    没有任何有效文本时返回 None，让调用方明确跳过标题生成。
    """
    if isinstance(content, str):
        text = content.strip()
        return text if text else None

    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())

    if not parts:
        return None
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 路由：查询流状态
#
# 前端在刷新、断线重连或展示“审批 / 插话”入口时先调这个接口，
# 再决定是直接连 /stream、从 last-event-id 续传，还是先处理审批。
# ---------------------------------------------------------------------------


@router.get("/chat/{chat_id}/stream/status")
async def stream_status(
    request: Request, chat_id: str = Path(..., description="Chat thread ID")
):
    redis_client = getattr(request.app.state, "redis_client", None)
    messages_repo = MessagesRepository()
    approvals_repo = ToolApprovalsRepository()
    active_tool_call_ids_set = await active_path_tool_call_ids(messages_repo, chat_id)
    pending_approval = bool(
        await approvals_repo.list_for_chat(
            chat_id=chat_id,
            approval_type=ToolApprovalType.APPROVAL,
            statuses={ToolApprovalStatus.PENDING},
            active_tool_call_ids=active_tool_call_ids_set,
        )
    )
    pending_oauth = bool(
        await approvals_repo.list_for_chat(
            chat_id=chat_id,
            approval_type=ToolApprovalType.OAUTH,
            statuses={ToolApprovalStatus.PENDING},
            active_tool_call_ids=active_tool_call_ids_set,
        )
    )
    if redis_client is None:
        raise HTTPException(status_code=500, detail="Redis client is not initialized")

    return {
        "running": bool(await redis_client.exists(run_lock_key(chat_id))),
        "resumable": bool(await redis_client.exists(stream_key(chat_id))),
        "pending_approval": pending_approval,
        "pending_oauth": pending_oauth,
        "pending_steering": await steering_queue_has_pending(redis_client, chat_id),
    }


# ---------------------------------------------------------------------------
# 路由：运行途中插话（steering）
#
# 模型还在生成时，用户可以把新消息塞进队列；当前这一轮循环会在安全点取出来，
# 作为新的用户指令继续执行，而不是等这一轮结束再另起一轮对话。
# ---------------------------------------------------------------------------


@router.post("/chat/{chat_id}/steering")
async def enqueue_chat_steering(
    request: Request,
    payload: SteeringMessageRequest,
    chat_id: str = Path(..., description="Chat thread ID"),
):
    """把一条新消息原子地放进正在运行的 chat 队列（steering）。

    返回结果直接映射成不同的 HTTP 语义，前端据此提示用户：
    - accepted → 202，已排队；
    - persisted → 运行已经结束，但消息已落库，下一轮会带上；
    - closing → 409，运行正在收尾，暂时不能插话；
    - inactive → 当前没有运行中的对话。
    """
    if await ChatsRepository().get(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat thread not found")

    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis client is not initialized")

    message = payload.message
    result = await enqueue_steering_message(
        redis_client,
        chat_id,
        payload.message_id,
        message,
    )
    if result == "accepted":
        return JSONResponse(
            {"status": "queued", "queued": True, "message_id": payload.message_id},
            status_code=202,
        )
    if result.startswith("persisted:"):
        persisted_message_id = result.removeprefix("persisted:")
        if not persisted_message_id:
            raise HTTPException(status_code=500, detail="Invalid persisted steering result")
        return {
            "status": "persisted",
            "persisted": True,
            "message_id": persisted_message_id,
            "client_message_id": payload.message_id,
        }
    if result == "closing":
        return JSONResponse(
            {"status": "closing"},
            status_code=409,
            headers={"X-Chat-Run-Closing": "true"},
        )
    if result == "inactive":
        return {"status": "inactive"}
    raise HTTPException(status_code=500, detail="Invalid steering enqueue result")


# ---------------------------------------------------------------------------
# 路由：聊天流（主处理器）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 聊天流主处理器
#
# 这个类是 /chat/{id}/stream 的核心：先做校验、续传判断、数据准备，
# 然后把真正的生成工作交给 streaming/ 下的生成器与生产者。
# ---------------------------------------------------------------------------


class StreamChatHandler:
    def __init__(self, request: Request, chat_id: str, auto_start: bool) -> None:
        self.request = request
        self.chat_id = chat_id
        self.auto_start = auto_start

    async def handle(self) -> StreamingResponse:
        """用 Server-Sent Events（SSE）把 AI 的回答流式推给前端。

    整体顺序：
    1. 校验依赖与资源（searcher、chat）；
    2. 处理“续传 / 挂到已有 run”的快路径；
    3. 按 agent 会话或普通会话准备工具、记忆、系统提示词；
    4. 修复历史里的中断调用、处理审批批次、展开附件与提及；
    5. 交给 prepare_and_stream_chat 生成事件流，并选择直连或通过 Redis 生产者交付。
    """
        request = self.request
        chat_id = self.chat_id
        auto_start = self.auto_start

        if not request.app.state.searcher_tool:
            raise HTTPException(status_code=500, detail="Searcher tool not initialized")

        chats_repo = ChatsRepository()
        chat = await chats_repo.get(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Chat thread not found")

        llm_provider = await _resolve_llm_provider(request.app.state, chat)
        redis_client = request.app.state.redis_client

        # 续传快路径：用户刷新页面或断线重连时，不该重新生成一遍。
        # - 如果这个会话已经有运行中的 producer（run lock 存在），直接挂上去读；
        # - 如果带了 last-event-id 且 Redis 里还有事件流，就从那个位置重放；
        # - 两者都没有，说明流已结束且过期，返回 not_resumable 让前端重新发起。
        last_event_id = request.headers.get(
            "last-event-id"
        ) or request.query_params.get("last_event_id")
        if redis_client is not None:
            run_active = await redis_client.exists(run_lock_key(chat_id))
            if run_active:
                return StreamingResponse(
                    consume_run(redis_client, chat_id, last_event_id or "0"),
                    media_type="text/event-stream",
                    headers=SSE_HEADERS,
                )

            if last_event_id is not None:
                if await redis_client.exists(stream_key(chat_id)):
                    return StreamingResponse(
                        consume_run(redis_client, chat_id, last_event_id),
                        media_type="text/event-stream",
                        headers=SSE_HEADERS,
                    )

                async def _not_resumable_response():
                    yield "event: not_resumable\ndata: \n\n"

                return StreamingResponse(
                    _not_resumable_response(),
                    media_type="text/event-stream",
                    headers=SSE_HEADERS,
                )

        messages_repo = MessagesRepository()
        approvals_repo = ToolApprovalsRepository()
        chat_messages = await messages_repo.get_active_path(chat_id)

        # 这些变量在 agent 会话和普通会话里都会用到，所以先统一初始化；
        # 后面各自的分支再按需填值，避免出现“某个变量在某条分支里未定义”。
        memory_provider = None
        effective_mode = MemoryMode.OFF
        memories: list[str] = []
        memory_write_key: str | None = None
        pending_interventions: list[ToolApproval] = []

        if chat.agent_id:
            # ---- Agent 会话：先做权限校验，再按 agent 的配置装配工具 ----
            agent_repo = AgentRepository()
            agent = await agent_repo.get_agent(chat.agent_id)
            if not agent:
                raise HTTPException(status_code=404, detail="Agent not found")

            users_repo = UsersRepository()
            chat_user = await users_repo.find_by_id(chat.user_id)
            if not chat_user:
                raise HTTPException(status_code=404, detail="Chat user not found")

            if agent.agent_type == "org":
                if chat_user.role != "admin":
                    raise HTTPException(
                        status_code=403,
                        detail="Admin access required for org agent chats",
                    )
            elif agent.user_id != chat.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Only the agent owner can chat with this agent",
                )

            is_org_agent = agent.agent_type == "org"
            tool_user_id = None if is_org_agent else agent.user_id
            tool_skip_perm = is_org_agent

            user_email = chat_user.email
            user_name = chat_user.full_name
            user_configuration = chat_user.configuration

            if not chat_messages:
                if auto_start:
                    chat_messages = []
                else:
                    raise HTTPException(
                        status_code=404, detail="No messages found for chat"
                    )

            build_result = await _build_agent_chat_registry(
                request,
                agent,
                is_admin=chat_user.role == "admin",
                user_id=chat.user_id,
            )
            registry = build_result.registry
            loaded_toolsets: set[str] = set()
            pending_interventions = []

            run_repo = AgentRunRepository()
            runs = await run_repo.list_runs(agent.id, limit=20)
            active_sources = [
                s for s in build_result.sources if s.is_active and not s.is_deleted
            ]

            memory_provider = request.app.state.memory_provider
            effective_mode = MemoryMode.OFF
            memories = []
            if memory_provider is not None:
                config_repo = ConfigurationRepository()
                org_default = (
                    await config_repo.get_global_configuration()
                ).memory_mode_default
                if is_org_agent:
                    effective_mode = org_default
                elif user_configuration is not None:
                    effective_mode = resolve_memory_mode(
                        user_configuration.memory_mode, org_default
                    )
                memory_namespace = agent_key(agent.id)
                if effective_mode >= MemoryMode.CHAT and chat_messages:
                    last_user_text = ""
                    for msg in reversed(chat_messages):
                        m = msg.message
                        if m.get("role") == "user":
                            content = m.get("content", "")
                            if isinstance(content, str):
                                last_user_text = content
                            elif isinstance(content, list):
                                last_user_text = " ".join(
                                    b.get("text", "")
                                    for b in content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            break
                    if last_user_text:
                        hits = await memory_provider.search(
                            query=last_user_text, key=memory_namespace, limit=5
                        )
                        memories = [h.record.text for h in hits if h.record.text]

            system_prompt = build_agent_chat_system_prompt(
                agent,
                runs,
                active_sources,
                user_name=user_name,
                user_email=user_email,
                memories=memories if memories else None,
                user_configuration=user_configuration,
                include_web_search=getattr(
                    request.app.state, "web_search_provider", None
                )
                is not None,
                include_fetch_web_page=getattr(
                    request.app.state, "web_fetch_provider", None
                )
                is not None,
            )

            messages: list[MessageParam] = [
                cast(MessageParam, dict(msg.message)) for msg in chat_messages
            ]
            needs_start = not messages or messages[-1].get("role") != "user"
            if auto_start and needs_start:
                messages.append({"role": "user", "content": "Go."})

        else:
            # ---- 普通会话：按用户身份装配工具，并恢复历史里的审批与已加载工具 ----
            tool_user_id = chat.user_id
            tool_skip_perm = False
            user_email: str | None = None
            user_name: str | None = None
            user_configuration: UserConfiguration | None = None
            is_admin = False
            if chat.user_id:
                users_repo = UsersRepository()
                user = await users_repo.find_by_id(chat.user_id)
                if user:
                    user_email = user.email
                    user_name = user.full_name
                    user_configuration = user.configuration
                    is_admin = user.role == "admin"

            if not chat_messages:
                raise HTTPException(
                    status_code=404, detail="No messages found for chat"
                )

            messages = [cast(MessageParam, dict(msg.message)) for msg in chat_messages]

            loaded_toolsets = set()

            build_result = await _build_registry(
                request,
                chat,
                is_admin=is_admin,
                loaded_toolsets=loaded_toolsets,
            )
            if build_result.connector_handler is not None:
                loaded_toolsets.update(
                    _loaded_tools_from_history(messages, build_result.connector_handler)
                )
            registry = build_result.registry

            active_tool_call_ids_set = {
                tool_use["id"] for tool_use in unanswered_tool_calls(messages)
            }
            pending_interventions = await approvals_repo.list_for_chat(
                chat_id=chat_id,
                statuses={
                    ToolApprovalStatus.PENDING,
                    ToolApprovalStatus.APPROVED,
                    ToolApprovalStatus.DENIED,
                },
                active_tool_call_ids=active_tool_call_ids_set,
            )
            oauth_intervention_statuses = {
                ToolApprovalStatus.PENDING,
                ToolApprovalStatus.APPROVED,
            }
            pending_interventions = [
                intervention
                for intervention in pending_interventions
                if intervention.approval_type == ToolApprovalType.APPROVAL
                or (
                    intervention.approval_type == ToolApprovalType.OAUTH
                    and intervention.status in oauth_intervention_statuses
                )
            ]

            visible_source_ids = {
                source.id
                for source in build_result.sources
                if source.is_active and not source.is_deleted
            }
            inaccessible_intervention_ids = _inaccessible_intervention_ids(
                pending_interventions,
                visible_source_ids,
                sources_fetch_succeeded=build_result.sources_fetch_succeeded,
            )
            for intervention_id in _intervention_ids_to_expire(
                pending_interventions,
                visible_source_ids,
                sources_fetch_succeeded=build_result.sources_fetch_succeeded,
            ):
                await approvals_repo.update_status(
                    intervention_id, ToolApprovalStatus.EXPIRED, chat.user_id
                )
            pending_interventions = [
                intervention
                for intervention in pending_interventions
                if intervention.id not in inaccessible_intervention_ids
            ]

            active_sources = [
                s for s in build_result.sources if s.is_active and not s.is_deleted
            ]

            memory_provider = request.app.state.memory_provider
            memories = []
            effective_mode = MemoryMode.OFF
            if memory_provider is not None and chat.user_id:
                memory_write_key = user_key(chat.user_id)
                config_repo = ConfigurationRepository()
                org_default = (
                    await config_repo.get_global_configuration()
                ).memory_mode_default
                user_memory_mode = (
                    user_configuration.memory_mode if user_configuration else None
                )
                effective_mode = resolve_memory_mode(user_memory_mode, org_default)
                if effective_mode >= MemoryMode.CHAT:
                    last_user_text = ""
                    for msg in reversed(chat_messages):
                        m = msg.message
                        if m.get("role") == "user":
                            content = m.get("content", "")
                            if isinstance(content, str):
                                last_user_text = content
                            elif isinstance(content, list):
                                last_user_text = " ".join(
                                    b.get("text", "")
                                    for b in content
                                    if isinstance(b, dict) and b.get("type") == "text"
                                )
                            break
                    if last_user_text:
                        hits = await memory_provider.search(
                            query=last_user_text,
                            key=user_key(chat.user_id),
                            limit=5,
                        )
                        memories = [h.record.text for h in hits if h.record.text]

            loaded_source_ids = _loaded_source_ids(
                loaded_toolsets, build_result.connector_handler
            )
            project_instructions, project_context_lines = await _load_project_context(
                chat
            )
            system_prompt = build_chat_system_prompt(
                active_sources,
                toolsets=build_result.toolsets,
                loaded_source_ids=loaded_source_ids,
                user_name=user_name,
                user_email=user_email,
                memories=memories if memories else None,
                user_configuration=user_configuration,
                include_web_search=getattr(
                    request.app.state, "web_search_provider", None
                )
                is not None,
                include_fetch_web_page=getattr(
                    request.app.state, "web_fetch_provider", None
                )
                is not None,
                project_instructions=project_instructions,
                project_context_lines=project_context_lines,
            )

        # ---- 两条分支汇合后的公共准备：审批批次、修复中断、清理空消息 ----
        intervention_tool_call_ids = {
            approval.tool_call_id
            for approval in pending_interventions
            if approval.tool_call_id is not None
        }
        preserved_batch_ids = resumable_batch_ids_for_interventions(
            messages, intervention_tool_call_ids
        )
        if intervention_tool_call_ids:
            # 只有“最新一批、且已经终结”的审批记录才可能被恢复执行；
            # 更早的审批已经续不上，留着会造成两个问题：
            # 1. 让“没有新用户消息”的检查被绕过（看起来像还有活要干）；
            # 2. 一直出现在 stream status 的待处理列表里干扰前端。
            # 所以这里把不在可恢复批次里的审批统一标记为过期。
            kept_interventions = [
                approval
                for approval in pending_interventions
                if approval.tool_call_id in preserved_batch_ids
            ]
            for approval in pending_interventions:
                if approval.tool_call_id not in preserved_batch_ids:
                    await approvals_repo.update_status(
                        approval.id, ToolApprovalStatus.EXPIRED, chat.user_id
                    )
            pending_interventions = kept_interventions

        messages, repaired_tool_calls = repair_interrupted_tool_calls(
            messages, preserve_tool_call_ids=preserved_batch_ids
        )
        if repaired_tool_calls:
            logger.warning(
                f"Inserted {repaired_tool_calls} failed tool_result placeholder(s) for interrupted tool calls in chat {chat_id}"
            )
        before = len(messages)
        messages = drop_empty_assistant_messages(messages)
        dropped = before - len(messages)
        if dropped:
            logger.warning(
                f"Dropped {dropped} empty assistant message(s) from history for chat {chat_id}"
            )

        # 在展开 uploads / mentions 之前先判断“有没有活要干”：
        # 如果最后一条不是用户消息、也没有待处理审批或排队中的插话，
        # 说明这只是一次完成后的重连，直接返回 NO_NEW_MESSAGE 结束，
        # 避免白白重新下载文件、重新解析提及。
        # 反过来，只要还有排队中的 steering 消息，就值得再跑一轮。
        last_message_role = messages[-1].get("role") if messages else None
        pending_steering = (
            redis_client is not None
            and await steering_queue_has_pending(redis_client, chat_id)
        )
        if not pending_interventions and not pending_steering and last_message_role != "user":
            logger.info(
                f"Last message is not from user, no processing needed. Chat ID: {chat_id}"
            )

            async def empty_generator():
                yield end_of_stream(
                    EndOfStreamReason.NO_NEW_MESSAGE,
                    message="No new user message to process.",
                ).encode()

            return StreamingResponse(
                empty_generator(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        storage = request.app.state.content_storage
        if storage is not None:
            messages = await expand_uploads(
                messages,
                chat_id=chat_id,
                storage=storage,
                uploads_repo=UploadsRepository(),
                sandbox_url=SANDBOX_URL,
                user_id=chat.user_id,
                supports_vision=llm_provider.supports_vision,
            )

        user_groups = None
        if user_email and not tool_skip_perm:
            try:
                user_groups = await GroupRepository().find_groups_for_user(user_email)
            except Exception as error:
                logger.error(
                    "Group lookup failed for %s — failing closed. Mentions will not be expanded.",
                    user_email,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Failed to verify permissions for document mentions. Please try again.",
                ) from error

        document_handler = DocumentToolHandler(
            content_storage=request.app.state.content_storage,
            documents_repo=DocumentsRepository(),
            sandbox_url=SANDBOX_URL,
            connector_manager_url=CONNECTOR_MANAGER_URL,
        )
        messages = await expand_mentions(
            messages,
            chat_id=chat_id,
            doc_handler=document_handler,
            user_id=tool_user_id,
            user_email=user_email,
            skip_permission_check=tool_skip_perm,
            user_groups=user_groups,
        )

        async def transform_steering_message(
            raw_message: dict[str, object],
        ) -> MessageParam:
            transformed = [cast(MessageParam, raw_message)]
            if storage is not None:
                transformed = await expand_uploads(
                    transformed,
                    chat_id=chat_id,
                    storage=storage,
                    uploads_repo=UploadsRepository(),
                    sandbox_url=SANDBOX_URL,
                    user_id=chat.user_id,
                )
            transformed = await expand_mentions(
                transformed,
                chat_id=chat_id,
                doc_handler=document_handler,
                user_id=tool_user_id,
                user_email=user_email,
                skip_permission_check=tool_skip_perm,
                user_groups=user_groups,
            )
            if len(transformed) != 1:
                raise ValueError("Steering message expansion returned an invalid message")
            return transformed[0]

        # 压缩：历史太长时用次要模型把旧对话总结掉，腾出上下文窗口。
        # 压缩消耗的 token 单独记到 COMPACTION 用途下，方便用量统计。
        secondary_provider = await _resolve_secondary_provider(request.app.state)

        def _on_compaction_usage(usage):
            track_usage(
                UsageRepository(),
                UsageContext(
                    user_id=chat.user_id,
                    model_id=secondary_provider.model_record_id,
                    model_name=secondary_provider.model_name,
                    provider_type=secondary_provider.provider.provider_type,
                    purpose=UsagePurpose.COMPACTION,
                    chat_id=chat_id,
                ),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
            )

        compactor = ConversationCompactor(
            llm_provider=secondary_provider.provider,
            model_name=secondary_provider.model_name,
            on_usage=_on_compaction_usage,
        )
        initial_tools = build_turn_tools(
            build_result.always_on_handlers,
            build_result.connector_handler,
            loaded_toolsets,
        )

        parent_id = chat_messages[-1].id if chat_messages else None
        gen = prepare_and_stream_chat(
            chat_id=chat_id,
            redis_client=redis_client,
            chat_messages=chat_messages,
            messages=messages,
            compactor=compactor,
            compactions_repo=CompactionsRepository(),
            target_provider=llm_provider.provider,
            initial_tools=initial_tools,
            llm_provider=llm_provider.provider,
            model_record_id=llm_provider.model_record_id,
            model_name=llm_provider.model_name,
            chat_user_id=chat.user_id,
            tool_user_id=tool_user_id,
            user_email=user_email,
            user_groups=user_groups,
            user_configuration=user_configuration,
            tool_skip_perm=tool_skip_perm,
            system_prompt=system_prompt,
            registry=registry,
            always_on_handlers=build_result.always_on_handlers,
            connector_handler=build_result.connector_handler,
            loaded_toolsets=loaded_toolsets,
            memory_provider=memory_provider,
            memory_write_key=memory_write_key,
            effective_mode=effective_mode,
            approvals_repo=approvals_repo,
            pending_interventions=pending_interventions,
            max_output_tokens=DEFAULT_MAX_TOKENS,
            transform_steering_message=transform_steering_message,
        )

        # 本轮执行追踪：包装事件流，记录模型调用、工具调用与轮次结果。
        # 主循环照常产出事件，追踪失败不影响对话本身。
        gen = traced_turn(
            gen,
            chat_id=chat_id,
            user_id=chat.user_id,
            user_message_id=_last_user_message_id(chat_messages),
            model_name=llm_provider.model_name,
        )

        if redis_client is None:
            return StreamingResponse(
                persist_and_transform(gen, chat_id, messages_repo, parent_id),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        # 同一个会话同时只允许一个生产者：用 Redis 的 SET NX 抢锁。
        # 抢不到说明已经有人在跑，客户端直接挂到现有事件流上读即可，不要再生成一遍。
        got_lock = await redis_client.set(
            run_lock_key(chat_id), "open", nx=True, ex=_RUN_LOCK_TTL
        )
        if not got_lock:
            return StreamingResponse(
                consume_run(redis_client, chat_id, last_event_id or "0"),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        await redis_client.delete(stream_key(chat_id))
        await redis_client.delete(cancel_key(chat_id))
        task = asyncio.create_task(
            run_producer(redis_client, chat_id, gen, messages_repo, parent_id)
        )
        set_producer_task(chat_id, task)

        def _cleanup_run_task(
            t: asyncio.Task,
            cid: str = chat_id,  # type: ignore[assignment]
        ) -> None:
            if _run_tasks_by_chat.get(cid) is t:
                clear_producer_task(cid, t)

        task.add_done_callback(_cleanup_run_task)

        return StreamingResponse(
            consume_run(redis_client, chat_id, "0"),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )


# ---------------------------------------------------------------------------
# 路由：聊天流入口
# ---------------------------------------------------------------------------


@router.get("/chat/{chat_id}/stream")
async def stream_chat(
    request: Request,
    chat_id: str = Path(..., description="Chat thread ID"),
    auto_start: bool = Query(
        False, description="Auto-inject initial message for agent chats"
    ),
):
    return await StreamChatHandler(request, chat_id, auto_start).handle()


# 路由：取消生成
# ---------------------------------------------------------------------------


@router.post("/chat/{chat_id}/cancel")
async def cancel_chat_stream(
    request: Request, chat_id: str = Path(..., description="Chat thread ID")
):
    redis_client = request.app.state.redis_client
    try:
        await redis_client.set(cancel_key(chat_id), "1", ex=_CANCEL_TTL)
    except Exception as e:
        logger.error(f"Failed to set cancel flag for chat {chat_id}: {e}")

    task = _run_tasks_by_chat.get(chat_id)
    if task is not None and not task.done():
        # 尽力让“当前进程”里的生成立刻停下；但取消请求可能落在另一个 worker 上，
        # 所以真正的跨进程机制是上面的 Redis 取消标记，producer 会主动检查它。
        task.cancel()

    return {"status": "cancelling"}


# ---------------------------------------------------------------------------
# 路由：生成会话标题
# ---------------------------------------------------------------------------


@router.post("/chat/{chat_id}/generate_title")
async def generate_chat_title(
    request: Request, chat_id: str = Path(..., description="Chat thread ID")
):
    logger.info(f"Generating title for chat: {chat_id}")

    try:
        chats_repo = ChatsRepository()
        chat = await chats_repo.get(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Chat thread not found")

        llm_provider = await _resolve_secondary_provider(request.app.state)

        if chat.title:
            logger.info(f"Chat already has a title: {chat.title}")
            return {"title": chat.title, "status": "existing"}

        messages_repo = MessagesRepository()
        chat_messages = await messages_repo.get_by_chat(chat_id)
        if not chat_messages:
            raise HTTPException(
                status_code=400, detail="Not enough messages to generate title"
            )

        conversation_text = ""
        for msg in chat_messages:
            role = msg.message.get("role", "unknown")
            if role != "user":
                continue
            content = msg.message.get("content")
            text = _extract_text_for_title(content)
            if text is not None:
                conversation_text = f"User: {text}\n"
                break

        if not conversation_text.strip():
            logger.info(
                "Skipping title generation; no user text found",
                extra={"chat_id": chat_id},
            )
            return {"status": "skipped", "reason": "no_user_text"}

        logger.info(f"Extracted conversation text ({len(conversation_text)} chars)")

        title_result = await generate_title_for_conversation(
            llm_provider.provider,
            llm_provider.model_name,
            conversation_text,
            chat_id,
        )
        title = title_result.title

        if title_result.usage is not None:
            track_usage(
                UsageRepository(),
                UsageContext(
                    user_id=chat.user_id,
                    model_id=llm_provider.model_record_id,
                    model_name=llm_provider.model_name,
                    provider_type=llm_provider.provider.provider_type,
                    purpose=UsagePurpose.TITLE_GENERATION,
                    chat_id=chat_id,
                ),
                input_tokens=title_result.usage.input_tokens,
                output_tokens=title_result.usage.output_tokens,
                cache_read_tokens=title_result.usage.cache_read_tokens,
                cache_creation_tokens=title_result.usage.cache_creation_tokens,
            )

        logger.info(f"Generated title: {title}")

        updated_chat = await chats_repo.update_title(chat_id, title)
        if not updated_chat:
            raise HTTPException(status_code=500, detail="Failed to update chat title")

        return {"title": title, "status": "generated"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to generate title for chat {chat_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to generate title: {str(e)}"
        )


# ---------------------------------------------------------------------------
# 路由：下载产物文件
#
# 文件实际存放在 sandbox 里，这里做一层代理，并补上缓存与安全响应头。
# ---------------------------------------------------------------------------


@router.get("/chat/{chat_id}/artifacts/{path:path}")
async def download_artifact(
    request: Request,
    chat_id: str = Path(..., description="Chat thread ID"),
    path: str = Path(..., description="Relative file path in the sandbox"),
    v: str | None = Query(None, description="Optional git SHA pinning the file version"),
):
    try:
        params = {"chat_id": chat_id, "path": path}
        if v:
            params["version"] = v
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{SANDBOX_URL}/files/download",
                params=params,
            )

            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="Artifact not found")

            resp.raise_for_status()

            content_type = resp.headers.get("content-type")
            if not content_type:
                raise HTTPException(
                    status_code=502, detail="Artifact response did not include a content type"
                )
            # 带版本号（v 参数）的文件内容永远不变，可以让浏览器长期缓存；
            # 不带版本号的 URL 指向“最新内容”，只能短时间缓存，否则用户看不到更新。
            cache_control = (
                "private, max-age=31536000, immutable" if v else "private, max-age=3600"
            )
            headers = {
                "Cache-Control": cache_control,
                # 产物页面会被放进 sandbox iframe 预览，而 sandbox iframe 是独立源，
                # 所以这里必须允许跨源读取；安全边界由 CSP 和 nosniff 负责兜住。
                "Access-Control-Allow-Origin": "*",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
            if content_type.lower().startswith("text/html"):
                headers["Content-Security-Policy"] = (
                    "default-src 'none'; base-uri 'none'; "
                    "script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "img-src 'none' data: blob:; font-src 'none'; connect-src 'none'; "
                    "media-src 'none'; object-src 'none'; form-action 'none'; "
                    "frame-ancestors 'self'; navigate-to 'none'; "
                    "sandbox allow-scripts allow-downloads"
                )
            return Response(content=resp.content, media_type=content_type, headers=headers)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"Sandbox artifact download failed: {e}")
        raise HTTPException(
            status_code=502, detail="Failed to fetch artifact from sandbox"
        )
    except Exception as e:
        logger.error(f"Artifact download error: {e}")
        raise HTTPException(status_code=500, detail="Internal error fetching artifact")


# 为兼容旧测试而重新导出；这两个符号的正式定义在 ``services/ai/streaming/`` 里。
from streaming.persist import (
    partial_assistant_message as _partial_assistant_message,
)  # noqa: E402, F401
from streaming.run import _run_tasks_by_chat  # noqa: E402, F401

