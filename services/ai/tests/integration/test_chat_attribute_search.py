"""Integration tests for search tool calls flowing through the chat SSE stream.

Validates that the chat handler correctly maps LLM tool calls to SearchRequest
and passes them to the searcher. Filters are now expressed via inline query
operators (e.g., "status:done in:jira") rather than separate tool parameters.

Uses real DB (testcontainers ParadeDB) for chat/message storage,
mock LLM that emits Anthropic SDK event objects, and a mock searcher
that captures the SearchRequest for assertion.
"""

# 这组集成测试想证明三件事：
# 1. 模型调用 search 工具后，chat 接口能把工具参数组装成 SearchRequest，正确交给搜索服务；
# 2. 搜索结果会作为一个 tool_result 事件推给前端，而且这一轮跑完，模型最后仍会给出文字回答；
# 3. 模型把工具参数写坏了（比如 JSON 缺了括号）时，工具不会被真的执行，
#    只回一条错误结果，让模型下一轮自己改过来。
#
# 测试里用假的组件顶替真依赖：
# - 大模型：假的 provider 直接按 Anthropic SDK 的格式吐事件对象，不用真的联网；
# - 搜索服务：用 AsyncMock 冒充，它会悄悄记下收到的 SearchRequest，方便后面检查；
# - 数据库：用 testcontainers 起一个真的 ParadeDB，聊天、消息、模型这些数据都真实写进去。

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from ulid import ULID

from anthropic.types import (
    InputJSONDelta,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStopEvent,
    ToolUseBlock,
)
from anthropic.types.raw_message_delta_event import Delta

from db import UsersRepository, ChatsRepository, MessagesRepository
import db.connection
from routers import chat_router
from state import AppState
from tools import SearchResponse, SearchResult
from tools.searcher_client import Document
from tests.helpers import create_mock_llm, message_start_event, text_response_events

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Mock searcher
# ---------------------------------------------------------------------------

# 提前编好一份“搜索服务会返回什么”：假装从索引里搜到了两条 Jira issue。
# 只填测试需要检查的字段——文档标题、来源类型和摘要片段（highlights），
# 后面检查 tool_result 事件时，就直接拿这份固定数据去对。
MOCK_SEARCH_RESPONSE = SearchResponse(
    results=[
        SearchResult(
            document=Document(
                id="doc_1",
                title="PROJ-101: Fix login bug",
                content_type="jira_issue",
                url="https://jira.example.com/browse/PROJ-101",
                source_type="jira",
            ),
            highlights=["Users cannot login when priority is High"],
            source_type="jira",
        ),
        SearchResult(
            document=Document(
                id="doc_2",
                title="PROJ-202: Crash on startup",
                content_type="jira_issue",
                url="https://jira.example.com/browse/PROJ-202",
                source_type="jira",
            ),
            highlights=["Application crashes on startup for critical bugs"],
            source_type="jira",
        ),
    ],
    total_count=2,
    query_time_ms=42,
)


def create_mock_searcher():
    """Return a mock SearcherTool that captures the SearchRequest."""
    # 用 AsyncMock 冒充真正的搜索工具：它每被调用一次，就会把收到的参数记下来。
    # 测试跑完后，用 handle.call_args[0][0] 就能把当时那个 SearchRequest 取出来检查。
    searcher = AsyncMock()
    searcher.handle.return_value = MOCK_SEARCH_RESPONSE
    return searcher


def create_mock_llm_with_invalid_tool_json(raw_json: str, response_text: str):
    """Return a mock LLM that emits malformed tool arguments, then a text retry."""
    # 这个假模型会“演”两轮戏，模仿真模型发现参数写错后自我纠正的过程：
    # 第 1 轮：按 Anthropic 的流式格式发起 search 工具调用，但参数是一段没写完的 JSON。
    #          预期 chat 层解析失败，于是不执行工具，只回一条错误结果；
    # 第 2 轮：模型收到错误结果后不再调工具，直接输出文字答案。
    # 用 call_count 记下被调用了几次，好决定这次该走哪一轮。
    call_count = 0

    async def stream_response(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 先开一条 assistant 消息，里面声明一个名叫 search 的工具调用块（参数先空着）。
            yield message_start_event()
            yield RawContentBlockStartEvent(
                type="content_block_start",
                index=0,
                content_block=ToolUseBlock(
                    type="tool_use",
                    id="toolu_invalid_json",
                    name="search",
                    input={},
                ),
            )
            # 工具参数是一小段一小段流式传过来的。这里故意只给半截 JSON，
            # chat 层要等全部片段收齐才会解析，到那时自然就失败了。
            yield RawContentBlockDeltaEvent(
                type="content_block_delta",
                index=0,
                delta=InputJSONDelta(
                    type="input_json_delta",
                    partial_json=raw_json,
                ),
            )
            yield RawContentBlockStopEvent(type="content_block_stop", index=0)
            # stop_reason="tool_use" 等于告诉下游：“这轮模型是要调工具，不是说完了。”
            yield RawMessageDeltaEvent(
                type="message_delta",
                delta=Delta(stop_reason="tool_use", stop_sequence=None),
                usage=MessageDeltaUsage(output_tokens=30),
            )
            yield RawMessageStopEvent(type="message_stop")
        else:
            for evt in text_response_events(response_text):
                yield evt

    provider = AsyncMock()
    provider.stream_response = stream_response
    provider.health_check.return_value = True
    return provider


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


def parse_sse_events(body: str) -> list[tuple[str, str]]:
    """Parse SSE text into list of (event_type, data) tuples."""
    # SSE 协议里，一个事件就是几行文字，事件和事件之间空一行隔开：
    #   event: message
    #   data: {...}
    #   （空行）
    # 这个函数把整段响应文字还原成 [(事件类型, 数据), ...]，测试就能按类型挑事件。
    # 如果文本最后没有空行收尾，也要把最后那个事件补上，别漏掉。
    events = []
    current_event = None
    current_data_lines: list[str] = []

    for line in body.split("\n"):
        if line.startswith("event: "):
            current_event = line[len("event: ") :]
        elif line.startswith("data: "):
            current_data_lines.append(line[len("data: ") :])
        elif line == "" and current_event is not None:
            events.append((current_event, "\n".join(current_data_lines)))
            current_event = None
            current_data_lines = []

    if current_event is not None:
        events.append((current_event, "\n".join(current_data_lines)))

    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_model(db_pool) -> str:
    """Create a model provider and model in the DB, return the model row ID."""
    # 这里直接写 SQL 而不走 repository，图的就是省事：
    # chat 流跑起来只需要库里有一个 provider 和一个 model，没别的依赖。
    # 返回的 model_id 后面会交给 _build_app 里的 provider_cache 使用。
    async with db_pool.acquire() as conn:
        provider_id = str(ULID())
        await conn.execute(
            "INSERT INTO model_providers (id, name, provider_type, config) VALUES ($1, $2, $3, $4)",
            provider_id,
            "Test Provider",
            "anthropic",
            "{}",
        )
        model_id = str(ULID())
        await conn.execute(
            "INSERT INTO models (id, model_provider_id, model_id, display_name, is_default) VALUES ($1, $2, $3, $4, $5)",
            model_id,
            provider_id,
            "test-model",
            "Test Model",
            False,
        )
    return model_id


@pytest.fixture
async def chat_with_message(db_pool, test_model):
    """Create a user, chat, and user message in the real DB, return (chat_id, user_id, model_id)."""
    # 在真正请求 /chat/{id}/stream 之前，库里得先有用户、会话和至少一条用户消息。
    # 这些记录都通过 repository 写入，顺便保证走的就是服务平时用的那条持久化路径。
    users_repo = UsersRepository(pool=db_pool)
    user = await users_repo.create(
        email=f"{ULID()}@test.local",
        password_hash="not-a-real-hash",
        full_name="Test User",
    )

    chats_repo = ChatsRepository(pool=db_pool)
    chat = await chats_repo.create(user_id=user.id, model_id=test_model)

    messages_repo = MessagesRepository(pool=db_pool)
    await messages_repo.create(
        chat_id=chat.id,
        message={"role": "user", "content": "Find all high-priority bugs"},
    )
    return chat.id, user.id, test_model


@pytest.fixture
def _patch_db_pool(db_pool, monkeypatch):
    """Point the global _db_pool at the test pool so ChatsRepository()/MessagesRepository() work."""
    # router 里面创建 repository 时不传连接池，而是去读 db.connection 里那个全局池。
    # 所以这里用 monkeypatch 把全局池临时换成测试用的连接池；
    # 测试一结束 monkeypatch 会自动还原，不会把测试连接留给别的用例。
    monkeypatch.setattr(db.connection, "_db_pool", db_pool)


async def _stream_chat(app: FastAPI, chat_id: str) -> str:
    """Hit the SSE endpoint and return the full response body."""
    # ASGITransport 让请求不用真的开 HTTP 服务器，而是在当前进程里直接调用应用。
    # 因为是 SSE 流式响应，这里干脆一次性把整段响应读完，再交给 parse_sse_events 拆开。
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/chat/{chat_id}/stream", timeout=30)
        assert resp.status_code == 200
        return resp.text


def _build_app(llm_provider, searcher_tool, model_id: str) -> FastAPI:
    # 搭一个只挂载 chat_router 的测试应用，并把 AppState 里的真实依赖换成假的：
    # - provider_cache 的三个解析方法统统返回同一个假模型，免得去读真实的模型配置；
    # - 搜索工具换成前面那个会记录调用参数的假替身。
    app = FastAPI()
    app.state = AppState()
    from provider_cache import ResolvedModel

    async def _resolve_for_model(mid: str):
        return ResolvedModel(provider=llm_provider, model_record_id=mid, model_name=mid)

    async def _resolve_default():
        return ResolvedModel(
            provider=llm_provider, model_record_id=model_id, model_name=model_id
        )

    async def _resolve_secondary_or_default():
        return ResolvedModel(
            provider=llm_provider, model_record_id=model_id, model_name=model_id
        )

    app.state.provider_cache.resolve_for_model = _resolve_for_model
    app.state.provider_cache.resolve_default = _resolve_default
    app.state.provider_cache.resolve_secondary_or_default = (
        _resolve_secondary_or_default
    )
    app.state.searcher_tool = searcher_tool
    app.include_router(chat_router)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_query_operators_flow_to_searcher(
    db_pool, chat_with_message, _patch_db_pool
):
    """Inline query operators are passed through to the searcher as the query string."""
    # 现在的约定是：过滤条件直接写成查询里的内联操作符（比如 status:done、in:jira），
    # 不再单独占一个工具参数。所以这里检查两件事：
    # - query 原封不动地交给了搜索服务；
    # - attribute_filters 和 source_types 都是空的，说明 chat 层没有偷偷加过滤条件。
    chat_id, _, model_id = chat_with_message
    tool_call_json = {
        "query": "status:done in:jira high priority bugs",
    }
    searcher = create_mock_searcher()
    app = _build_app(create_mock_llm(tool_call_json), searcher, model_id)

    await _stream_chat(app, chat_id)

    searcher.handle.assert_called_once()
    captured_request = searcher.handle.call_args[0][0]
    assert captured_request.query == "status:done in:jira high priority bugs"
    assert captured_request.attribute_filters is None
    assert captured_request.source_types is None


@pytest.mark.asyncio
async def test_simple_query_sends_no_filters(
    db_pool, chat_with_message, _patch_db_pool
):
    """A plain query without operators sends no filters."""
    # 对照组：一个普普通通的查询，本来就该什么过滤条件都不带。
    # 用来防止 chat 层把默认值误当成过滤条件，塞进 SearchRequest。
    chat_id, _, model_id = chat_with_message
    tool_call_json = {"query": "recent documents"}
    searcher = create_mock_searcher()
    app = _build_app(create_mock_llm(tool_call_json), searcher, model_id)

    await _stream_chat(app, chat_id)

    searcher.handle.assert_called_once()
    captured_request = searcher.handle.call_args[0][0]
    assert captured_request.attribute_filters is None
    assert captured_request.source_types is None


@pytest.mark.asyncio
# 这是最接近日常使用的一条路径：假模型第 1 轮调 search 工具，第 2 轮给出文字回答。
# 检查三件事：
# 1. SSE 流里有 message_id 开头、end_of_stream 结尾，说明流能正常开始、正常结束；
# 2. tool_result 事件把搜索结果送回了前端（is_error 为 False，并且带着 source_type）；
# 3. 模型最后的回答确实以文字片段的形式推给了前端。
async def test_stream_completes_with_tool_results(
    db_pool, chat_with_message, _patch_db_pool
):
    """Full SSE stream contains tool call events, save_message, tool_result, text, and end_of_stream."""
    chat_id, _, model_id = chat_with_message
    tool_call_json = {
        "query": "status:open in:jira high priority bugs",
    }
    response_text = "I found 2 high-priority bugs."
    searcher = create_mock_searcher()
    app = _build_app(create_mock_llm(tool_call_json, response_text), searcher, model_id)

    body = await _stream_chat(app, chat_id)
    events = parse_sse_events(body)
    event_types = [e[0] for e in events]

    assert "message_id" in event_types
    assert "end_of_stream" in event_types

    tool_result_events = [
        (t, d) for t, d in events if t == "message" and "tool_result" in d
    ]
    assert len(tool_result_events) >= 1
    tool_result_data = json.loads(tool_result_events[0][1])
    assert tool_result_data["type"] == "tool_result"
    assert tool_result_data["is_error"] is False
    search_results = [
        block
        for block in tool_result_data["content"]
        if block["type"] == "search_result"
    ]
    assert search_results[0]["source_type"] == "jira"

    text_deltas = [d for t, d in events if t == "message" and response_text in d]
    assert len(text_deltas) >= 1


@pytest.mark.asyncio
# 出错路径：工具参数是一段残缺的 JSON。这时 chat 层应该做到：
# 1. 不去调用搜索服务（searcher.handle 一次都没被调用）；
# 2. 回一条 is_error=True 的 tool_result，并在文字里说明是 JSON 解析失败；
# 3. 流程不能中断——让模型收到错误后再试一次，最终仍然输出文字答案。
async def test_malformed_tool_input_is_not_executed(
    db_pool, chat_with_message, _patch_db_pool
):
    chat_id, _, model_id = chat_with_message
    response_text = "Retried after receiving the JSON parse error."
    searcher = create_mock_searcher()
    app = _build_app(
        create_mock_llm_with_invalid_tool_json(
            '{"query": "missing brace"', response_text
        ),
        searcher,
        model_id,
    )

    body = await _stream_chat(app, chat_id)
    events = parse_sse_events(body)

    searcher.handle.assert_not_called()
    tool_result_events = [
        json.loads(data)
        for event_type, data in events
        if event_type == "message" and "tool_result" in data
    ]
    assert tool_result_events
    assert tool_result_events[0]["is_error"] is True
    assert "Invalid JSON in tool input" in tool_result_events[0]["content"][0]["text"]
    assert any(
        response_text in data for event_type, data in events if event_type == "message"
    )
