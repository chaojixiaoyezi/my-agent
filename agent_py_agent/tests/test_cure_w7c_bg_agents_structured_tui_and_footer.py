"""Workstream W7C: Background Agent anti-burial, oversized message truncation,
structured TUI conversation blocks, lifecycle commands, and footer badge coexistence.
"""
from __future__ import annotations

import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.business_access import BusinessActor
from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.cli.chat_parts import tui_agent_navigation as navigation
from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_background_agents import (
    TuiBackgroundAgents,
    submit_business_input,
)
from agent_py_agent.cli.chat_parts.tui_block_renderer import (
    TuiRenderContext,
    fragments_text,
    render_tui_snapshot,
)
from agent_py_agent.cli.chat_parts.tui_runtime import (
    TuiRuntime,
    TuiTurnEventAdapter,
    _consume_gateway_turn_event,
)


class LoadThreads:
    def __init__(self):
        self.threads = []

    def create(self, *, target, name, daemon):
        thread = threading.Thread(target=target, name=name, daemon=daemon)
        self.threads.append(thread)
        return thread

    def join(self):
        for thread in self.threads:
            thread.join(timeout=5)
            assert not thread.is_alive(), "UI 消息读取没有有界结束"


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    source = store.threads.create(owner_id="owner-w7c", canonical_user_id="user-w7c", cwd=str(tmp_path))
    host = SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(
            owner_id="owner-w7c",
            owner_home_dir=tmp_path,
            owner_kind="main",
            owner_provider="local",
        ),
        config=SimpleNamespace(max_background_agents=10),
        run=lambda *a, **k: pytest.fail("LLM model call forbidden in unit tests"),
    )
    actor = HostChannelActor("owner-w7c", source.thread_id, "entry", "local", "local", "user")

    import agent_py_agent.agent.settings.model_profiles as profiles
    import agent_py_agent.agent.settings.thread_model_selection as selection

    monkeypatch.setattr(selection, "default_model_profile_id", lambda h: "model-w7c")
    monkeypatch.setattr(profiles, "selected_model_config", lambda h, **k: SimpleNamespace(model_name="fake-w7c"))
    monkeypatch.setattr(selection, "selected_model_config", profiles.selected_model_config)

    spaces = ModuleType("agent_py_agent.agent.conversation.business_spaces")

    class Spaces:
        def __init__(self, host):
            self.host = host

        def get(self, space_id, *, actor):
            return {"space": {"space_id": space_id, "state": "active", "revision": 1}}

    spaces.BusinessSpaceService = Spaces
    monkeypatch.setitem(pytest.importorskip("sys").modules, "agent_py_agent.agent.conversation.business_spaces", spaces)

    return SimpleNamespace(store=store, host=host, actor=actor, tmp_path=tmp_path, source=source)


# ---------------------------------------------------------------------------
# Requirement 1: Anti-Burial in BackgroundAgentService
# ---------------------------------------------------------------------------
def test_anti_burial_foreground_churn_does_not_bury_active_seats(test_env):
    service = BackgroundAgentService(test_env.host)
    store = test_env.store
    actor = test_env.actor

    # 1. Create 2 real background agent seats
    bg1 = service.create({"space_id": None, "name": "Seat-Alpha"}, actor=actor, idempotency_key="key-alpha-12345678")
    bg2 = service.create({"space_id": None, "name": "Seat-Beta"}, actor=actor, idempotency_key="key-beta-1234567890")
    bg1_id = bg1["agent"]["agent_id"]
    bg2_id = bg2["agent"]["agent_id"]

    # 2. Create 1,600 foreground chat threads directly in store
    # These must get filter_key="__foreground__" and NOT filter_key=""
    for i in range(1600):
        store.threads.create(
            owner_id="owner-w7c",
            canonical_user_id="user-w7c",
            title=f"Chat {i}",
        )

    # 3. Add 1 corrupt .json file to threads_dir
    corrupt_file = store.storage.threads_dir / "corrupt_thread.json"
    corrupt_file.write_text("NOT_VALID_JSON{:::broken", encoding="utf-8")

    # 4. List background agents
    listing = service.list({"limit": 50}, actor=actor)

    # Both background seats MUST be present on page 1
    found_ids = {item["agent_id"] for item in listing["items"]}
    assert bg1_id in found_ids, "Seat-Alpha was buried by foreground threads!"
    assert bg2_id in found_ids, "Seat-Beta was buried by foreground threads!"

    # ZERO foreground threads should pollute the items
    assert all(item["agent_id"].startswith("thread-bg-") for item in listing["items"])

    # Corrupt file must cause coverage="partial"
    assert listing["coverage"] == "partial", "Corrupt thread file must surface partial coverage!"


# ---------------------------------------------------------------------------
# Requirement 2: Oversized Single-Message Preview Truncation
# ---------------------------------------------------------------------------
def test_oversized_message_truncation_without_result_limit_exceeded(test_env):
    store = test_env.store
    actor = test_env.actor

    # Create background thread and task
    service = BackgroundAgentService(test_env.host)
    bg = service.create({"space_id": None, "name": "Observer-Seat"}, actor=actor, idempotency_key="key-obs-1234567890")
    thread_id = bg["agent"]["thread_id"]

    # Create a 400,000 char message containing sensitive API tokens
    # (tokens that expand when redacted)
    original_size = 400_000
    token = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz12345678901234567890"
    part1 = "X" * 200_000
    part2 = "Y" * (original_size - 200_000 - len(token) - 2)
    large_content = f"{part1} {token} {part2}"
    assert len(large_content) == original_size

    store.messages.append({
        "thread_id": thread_id,
        "role": "assistant",
        "content": large_content,
        "channel": "internal",
    })

    # Query via BusinessObservationService._messages directly
    obs_service = BusinessObservationService(test_env.tmp_path, owner_id="owner-w7c", store=store)
    biz_actor = BusinessActor("owner-w7c", "user", obs_service, lambda _action, _facts: True)

    result = obs_service._messages({"target": {"kind": "background_main", "id": thread_id}}, biz_actor)

    assert result["coverage"] in ("complete", "partial")
    assert len(result["items"]) == 1
    item = result["items"][0]

    # Wire schema enforcement: exact 13 MessageView keys, additionalProperties: False
    expected_keys = {
        "message_id",
        "thread_id",
        "task_id",
        "space_id",
        "role",
        "text",
        "occurred_at",
        "source",
        "artifact_refs",
        "association_coverage",
        "association_reason",
        "execution_refs",
        "input_ref",
    }
    assert set(item.keys()) == expected_keys, f"MessageView must match exact 13 keys, got {set(item.keys())}"

    # Text length MUST be <= 262144
    assert len(item["text"]) <= 262144
    assert "单条历史消息过长已截断预览，原长度 400000 字符" in item["text"]


# ---------------------------------------------------------------------------
# Requirement 3: Structured TUI Conversation Blocks & Pagination
# ---------------------------------------------------------------------------
def test_structured_tui_blocks_and_pagination_prepend(monkeypatch):
    threads = LoadThreads()
    monkeypatch.setattr(navigation, "threading", SimpleNamespace(Thread=threads.create, RLock=threading.RLock))

    root = TuiRuntime("test-sess-w7c")
    nav = TuiAgentNavigationState(root)
    thread_id = "thread-bg-structured-01"

    # Setup mock projection
    client = MagicMock()
    projection = TuiBackgroundAgents(client)
    nav.update_business_agents(projection)

    # Fake targets and path
    nav._business_targets["business:" + thread_id] = {"agent_id": thread_id, "thread_id": thread_id}
    nav._path = ["business:agents", "business:" + thread_id]
    active_runtime = nav.active_runtime()

    # Initial page with 4 roles
    initial_messages = [
        {"message_id": "m1", "role": "user", "text": "你好后台Agent", "thread_id": thread_id},
        {"message_id": "m2", "role": "assistant", "text": "# 收到任务\n开始处理数据", "thread_id": thread_id},
        {"message_id": "m3", "role": "tool", "tool_name": "data_parser", "text": '{"parsed": 42}', "thread_id": thread_id},
        {"message_id": "m4", "role": "host", "text": "系统安全检查通过", "thread_id": thread_id},
    ]

    client.call.return_value = {
        "ok": True,
        "data": {
            "items": initial_messages,
            "coverage": "partial",
            "next_cursor": "cur-older-page",
        },
    }

    # Load initial page
    nav._load_business_conversation(thread_id, active_runtime, more=False)
    threads.join()

    snapshot = active_runtime.store.snapshot()
    stable_blocks = snapshot.stable_blocks
    kinds = [b.kind for b in stable_blocks]

    assert "user_message" in kinds
    assert "assistant_completed" in kinds
    assert "tool_completed" in kinds
    assert "system_message" in kinds

    # Test /history (more=True) prepends older page
    older_messages = [
        {"message_id": "m0_old", "role": "user", "text": "最早期的一条消息", "thread_id": thread_id},
    ]
    client.call.return_value = {
        "ok": True,
        "data": {
            "items": older_messages,
            "coverage": "complete",
            "next_cursor": None,
        },
    }

    assert nav.load_more_business_messages()
    threads.join()

    snapshot2 = active_runtime.store.snapshot()
    block_ids = [b.block_id for b in snapshot2.stable_blocks]
    # Older message block MUST appear ahead of newer messages in stable_blocks
    assert f"history:{thread_id}:m0_old:user" in block_ids
    m0_idx = block_ids.index(f"history:{thread_id}:m0_old:user")
    m1_idx = block_ids.index(f"history:{thread_id}:m1:user")
    assert m0_idx < m1_idx, "Older history page must be prepended ahead of newer blocks!"

    # Test coverage=unknown warning notice
    client.call.return_value = {
        "ok": False,
        "error": {"code": "DATA_CORRUPTED"},
        "http_status": 500,
    }
    nav._load_business_conversation(thread_id, active_runtime, more=False)
    threads.join()

    notice_text = active_runtime.notice()
    assert "unknown" in notice_text
    assert "DATA_CORRUPTED" in notice_text
    assert "/history 重试" in notice_text
    assert active_runtime.notice_kind() == "warning"


# ---------------------------------------------------------------------------
# Requirement 4: submit_business_input Structured Enqueue & Slash Commands
# ---------------------------------------------------------------------------
def test_submit_business_input_lifecycle_and_prompt_enqueue():
    root = TuiRuntime("test-sess-submit")
    nav = TuiAgentNavigationState(root)
    thread_id = "thread-bg-input-01"

    client = MagicMock()
    client.call.return_value = {"ok": True, "data": {"agent": {"agent_id": thread_id, "revision": 2}}}
    client.send.return_value = {"ok": True, "state": "completed", "data": {"revision": 3}}
    client.prepare.return_value = SimpleNamespace(payload=lambda: {"action": "background_agents.suspend"})

    projection = TuiBackgroundAgents(client)
    nav.update_business_agents(projection)
    nav._business_targets["business:" + thread_id] = {"agent_id": thread_id, "thread_id": thread_id}
    nav._path = ["business:agents", "business:" + thread_id]
    active_runtime = nav.active_runtime()

    buffer = MagicMock()
    app = MagicMock()
    background_tasks = []
    app.create_background_task.side_effect = lambda coro: background_tasks.append(coro)

    event = SimpleNamespace(app=app)
    params = SimpleNamespace(agent_navigation=nav, input_area=SimpleNamespace(buffer=buffer))

    # 1. Normal message: creates structured user_message block via enqueue_prompt
    assert submit_business_input(event, params, "请帮我分析日志")
    snapshot = active_runtime.store.snapshot()
    user_blocks = [b for b in (*snapshot.stable_blocks, *snapshot.active_blocks) if b.role == "user"]
    assert any(b.text == "请帮我分析日志" for b in user_blocks)

    # 2. /clear & /new: resets conversation view while keeping seat intact
    nav._business_messages[thread_id] = {"m1", "m2"}
    nav._business_history[thread_id] = {"next_cursor": "cur-1"}
    assert submit_business_input(event, params, "/clear")
    assert thread_id not in nav._business_messages
    assert thread_id not in nav._business_history
    assert "已重置当前后台会话本地视图" in active_runtime.notice()

    # 3. /pause: lifecycle suspend
    assert submit_business_input(event, params, "/pause")
    assert len(background_tasks) >= 1
    # Execute all scheduled background tasks cleanly
    import asyncio
    while background_tasks:
        asyncio.run(background_tasks.pop(0))
    assert client.prepare.called


# ---------------------------------------------------------------------------
# Requirement 5: runtime_notice yellow styling & badge coexistence
# ---------------------------------------------------------------------------
def test_runtime_notice_yellow_styling_and_badge_coexistence():
    runtime = TuiRuntime("test-notice-sess")
    adapter = TuiTurnEventAdapter(runtime, "req-notice-01")

    # Dispatch runtime_notice event
    notice_event = {
        "level": "warning",
        "notice_kind": "warning",
        "error_code": "POST_TOOL_BOOKKEEPING_WARNING",
        "text": "⚠ 后台归档记录写入失败，当前工具结果已生效",
    }
    handled = _consume_gateway_turn_event(adapter, {"kind": "runtime_notice", "runtime_notice": notice_event})
    assert handled is True
    assert runtime.notice() == "⚠ 后台归档记录写入失败，当前工具结果已生效"
    assert runtime.notice_kind() == "warning"

    # Verify yellow styling in footer
    snapshot = runtime.store.snapshot()
    context = TuiRenderContext(
        width=120,
        notice=runtime.notice(),
        notice_kind=runtime.notice_kind(),
        background_agent_count=3,
        background_agent_running_count=1,
    )
    frame = render_tui_snapshot(snapshot, context)
    text = fragments_text(frame.footer)

    # Notice text and yellow style
    assert "⚠ 后台归档记录写入失败" in text
    has_warning_style = any(style == "class:tui-context-warning" for style, _ in frame.footer)
    assert has_warning_style, "Warning notice must have class:tui-context-warning style!"

    # Coexistence with right-aligned badge on wide terminal (width=120 >= 24 remaining)
    assert "后台Agent：运行 1 / 总 3" in text

    # On narrow terminal (width=25), badges are dropped to keep notice readable
    context_narrow = TuiRenderContext(
        width=25,
        notice="短通知",
        notice_kind="warning",
        background_agent_count=3,
        background_agent_running_count=1,
    )
    frame_narrow = render_tui_snapshot(snapshot, context_narrow)
    text_narrow = fragments_text(frame_narrow.footer)
    assert "短通知" in text_narrow
    assert "后台Agent：" not in text_narrow
