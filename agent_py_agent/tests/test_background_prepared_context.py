"""后台上下文和历史种子的准备阶段只读一次，重复投影沿冻结事实。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_context as context_module
from agent_py_agent.agent.conversation import background_history_seed as history_module
from agent_py_agent.agent.conversation.history_seed import (
    seed_provider_history_messages,
    seed_text_messages,
)
from agent_py_agent.agent.conversation.store import ConversationStore


def _setup(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {"canonical_user_id": "owner-prepared", "channel": "tui"}
    )
    config = SimpleNamespace(
        conversation_context_recent_limit=20,
        background_context_max_string_chars=1200,
        background_context_max_list_items=20,
        background_context_max_dict_items=80,
        background_context_max_depth=6,
        background_context_max_total_tokens=8000,
    )
    agent = SimpleNamespace(config=config, root=tmp_path, conversation_store=store)
    return agent, store, thread


def _request(thread, *, task_id="", reason="scheduled_progress_report", wake_signal=None):
    return SimpleNamespace(
        thread_id=thread.thread_id,
        task_id=task_id,
        reason=reason,
        wake_signal=wake_signal,
        conversation_turn_id="prepared-turn",
    )


def test_context_prepares_once_then_renders_without_store_or_progress_replay(
    tmp_path, monkeypatch
):
    agent, store, thread = _setup(tmp_path)
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": "原会话内容"}
    )
    calls = []
    original_state = context_module.task_runtime_state

    def counted_state(**kwargs):
        calls.append(kwargs["task_id"])
        return original_state(**kwargs)

    monkeypatch.setattr(context_module, "task_runtime_state", counted_state)
    prepared = context_module.prepare_background_context(
        agent=agent, store=store, thread=thread,
        request=_request(thread, task_id="task-once"),
    )
    assert calls == ["task-once"]

    def forbidden(*_args, **_kwargs):
        pytest.fail("重复 render 不得重新读 store 或重做进度对账")

    monkeypatch.setattr(store, "context_bundle_report", forbidden)
    monkeypatch.setattr(store.messages, "recent_report", forbidden)
    monkeypatch.setattr(context_module, "task_runtime_state", forbidden)
    first = context_module.render_background_context(prepared)
    assert "原会话内容" in first
    assert context_module.render_background_context(prepared) == first
    assert calls == ["task-once"]


def test_context_freezes_borrowed_request_config_and_store_data(tmp_path):
    agent, store, thread = _setup(tmp_path)
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": "冻结前正文"}
    )
    wake = {"reason": "scheduled_progress_report", "metadata": {"source_id": "source-old"}}
    request = _request(thread, wake_signal=wake)
    prepared = context_module.prepare_background_context(
        agent=agent, store=store, thread=thread, request=request,
    )
    baseline = context_module.render_background_context(prepared)

    wake["metadata"]["source_id"] = "source-new"
    request.reason = "audit_finding"
    agent.config.background_context_max_string_chars = 2
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": "冻结后正文"}
    )
    assert context_module.render_background_context(prepared) == baseline
    assert "source-old" in baseline
    assert "source-new" not in baseline
    assert "冻结后正文" not in baseline


def test_narrow_audit_render_excludes_prior_conversation(tmp_path):
    agent, store, thread = _setup(tmp_path)
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": "PRIVATE_PRIOR_CHAT"}
    )
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "audit-a", "goal": "核对来源"})
    prepared = context_module.prepare_background_context(
        agent=agent, store=store, thread=thread,
        request=_request(thread, task_id="audit-a", reason="audit_finding",
                         wake_signal={"metadata": {"source_id": "source-a"}}),
    )
    rendered = context_module.render_background_context(prepared)
    assert "Audit Task Objective" in rendered
    assert '"task_id": "audit-a"' in rendered
    assert "PRIVATE_PRIOR_CHAT" not in rendered
    assert "## Recent Messages" not in rendered
    assert "## Conversation Thread" not in rendered


def test_history_projection_reuses_frozen_detached_anchor_and_scope(
    tmp_path, monkeypatch
):
    agent, store, thread = _setup(tmp_path)
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": "创建前聊天", "now": 50.0}
    )
    store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": "task-A", "goal": "独立任务 A",
        "status": "active", "work_kind": "goal", "work_name": "saved-A",
        "cancellation_scope": "detached", "now": 100.0,
    })
    store.messages.append({
        "thread_id": thread.thread_id, "role": "assistant", "content": "A 的工作",
        "metadata": {"conversation_task_id": "task-A"}, "now": 150.0,
    })
    store.messages.append({
        "thread_id": thread.thread_id, "role": "user", "content": "未来兄弟 B",
        "metadata": {"conversation_task_id": "task-B"}, "now": 200.0,
    })
    result = history_module.background_conversation_history_seed(
        agent, store, thread, _request(thread, task_id="task-A")
    )
    assert result.status == "ready", result.detail
    assert result.projection is not None
    assert result.projection.scope.detached
    rows = store.messages.recent(thread.thread_id, limit=0)

    def forbidden(*_args, **_kwargs):
        pytest.fail("历史重投影不得重新读取范围或消息")

    monkeypatch.setattr(store, "context_bundle_report", forbidden)
    monkeypatch.setattr(store.messages, "recent_report", forbidden)
    monkeypatch.setattr(store.messages, "after_compact_report", forbidden)
    replayed = history_module.project_background_history_seed(agent, result.projection, rows)
    # 准备时的种子是只读来源，重投影是具体种子；两者按原规则解析后必须逐项相等。
    assert (replayed.compact_summary, replayed.compact_generation) == (
        result.seed.compact_summary, result.seed.compact_generation,
    )
    assert seed_text_messages(replayed) == seed_text_messages(result.seed)
    assert seed_provider_history_messages(replayed) == seed_provider_history_messages(result.seed)
    assert [content for _, content in seed_text_messages(replayed)] == ["创建前聊天", "A 的工作"]


def test_detached_history_rejects_summary_created_after_task(tmp_path):
    agent, store, thread = _setup(tmp_path)
    store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": "task-A", "goal": "独立任务 A",
        "status": "active", "work_kind": "goal", "work_name": "saved-A",
        "cancellation_scope": "detached", "now": 100.0,
    })
    later_thread = store.threads.update_summary(
        thread.thread_id, "未来兄弟任务摘要", now=200.0
    )
    result = history_module.background_conversation_history_seed(
        agent, store, later_thread, _request(thread, task_id="task-A")
    )
    assert result.status == "ready", result.detail
    assert result.projection is not None
    assert result.seed.compact_summary == ""
    assert result.projection.thread["summary"] == ""


def test_unknown_and_unreadable_history_never_fabricate_projection(
    tmp_path, monkeypatch
):
    agent, store, thread = _setup(tmp_path)
    request = _request(thread)
    unknown = history_module.background_conversation_history_seed(agent, None, thread, request)
    assert unknown.status == "unreadable"
    assert unknown.seed is None and unknown.projection is None

    def unreadable(_thread, **_kwargs):
        raise OSError("transcript unavailable")

    monkeypatch.setattr(store.messages, "recent_report", unreadable)
    result = history_module.background_conversation_history_seed(agent, store, thread, request)
    assert result.status == "unreadable"
    assert result.load_errors
    assert result.seed is None and result.projection is None

    disabled = history_module.background_conversation_history_seed(
        agent, store, thread, _request(thread, reason="audit_finding")
    )
    assert disabled.status == "disabled"
    assert disabled.seed is None and disabled.projection is None
