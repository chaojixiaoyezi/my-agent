from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.conversation import ConversationStore


class _BrokenBackgroundContextStore(ConversationStore):
    def context_bundle(self, thread_id: str, *, recent_limit: int = 20):
        raise OSError("context bundle unreadable")

    def context_bundle_report(self, thread_id: str, *, recent_limit: int = 20):
        raise OSError("context bundle unreadable")

    def pending_wake_signals(self, *, limit: int = 100, include_normal: bool = True):
        raise OSError("wake queue unreadable")

    def pending_wake_signals_report(self, *, limit: int = 100, include_normal: bool = True):
        raise OSError("wake queue unreadable")

    def load_background_run_claim(self, thread_id: str):
        raise ValueError("claim json corrupt")


def test_background_context_reports_load_errors_without_hiding_state(tmp_path):
    from agent_py_agent.agent.conversation.runtime import context_markdown

    store = _BrokenBackgroundContextStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "canonical_user_id": "user-1",
        }
    )
    agent = SimpleNamespace(config=None, root=tmp_path)
    request = SimpleNamespace(reason="scheduled_progress_report", task_id="task-1", wake_signal=None)

    prompt = context_markdown(agent=agent, store=store, thread=thread, request=request)

    assert "Runtime Load Errors" in prompt
    assert "background_context.context_bundle" in prompt
    assert "background_context.pending_wake_signals" in prompt
    assert "background_context.recovery_snapshot" in prompt
    assert "不要把它解释成任务完成" in prompt or "本地状态或路径读取失败" in prompt
    assert "Conversation Thread" in prompt


def test_background_context_reports_corrupt_jsonl_rows_without_dropping_good_rows(tmp_path):
    from agent_py_agent.agent.conversation.runtime import context_markdown

    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.append_message({"thread_id": thread.thread_id, "role": "user", "content": "这条消息应该保留"})
    store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "progress",
            "summary": "这条观察应该保留",
        }
    )
    store.append_guidance(
        {
            "target_type": "thread",
            "target_id": thread.thread_id,
            "message": "这条运行中提示应该保留",
        }
    )
    _append_bad_jsonl_rows(store._message_path(thread.thread_id))
    _append_bad_jsonl_rows(store._observation_path(thread.thread_id))
    _append_bad_jsonl_rows(store._guidance_path("thread", thread.thread_id))

    prompt = _context_prompt(store, thread, tmp_path)

    assert "这条消息应该保留" in prompt
    assert "这条观察应该保留" in prompt
    assert "这条运行中提示应该保留" in prompt
    assert "conversation.messages.read" in prompt
    assert "conversation.observations.read" in prompt
    assert "conversation.guidance.read" in prompt
    assert "line_number" in prompt


def test_background_context_reports_corrupt_wake_signal_without_dropping_good_signal(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "summary": "这条唤醒应该保留",
            "reason": "agent_event",
        }
    )
    (store.wake_queue_dir / "urgent" / "bad.json").write_text("[]", encoding="utf-8")

    prompt = _context_prompt(store, thread, tmp_path)

    assert "这条唤醒应该保留" in prompt
    assert "conversation.wake_signal.read" in prompt
    assert "bad.json" in prompt


def test_background_context_reports_corrupt_task_link_without_dropping_good_task(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-good", "goal": "正常任务"})
    store.bind_task({"thread_id": thread.thread_id, "task_id": "task-bad", "goal": "坏链接任务"})
    store._task_path("task-bad").write_text("not-json", encoding="utf-8")

    prompt = _context_prompt(store, thread, tmp_path)

    assert "task-good" in prompt
    assert "正常任务" in prompt
    assert "conversation.task_link.read" in prompt
    assert "task-bad" in prompt


def test_background_context_reports_corrupt_background_claim(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store._background_claim_path(thread.thread_id).write_text("not-json", encoding="utf-8")

    prompt = _context_prompt(store, thread, tmp_path)

    assert "claim_load_error" in prompt
    assert "conversation.background_claim.read" in prompt


def test_background_context_reports_corrupt_current_thread_file(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store)
    store._thread_path(thread.thread_id).write_text("not-json", encoding="utf-8")

    prompt = _context_prompt(store, thread, tmp_path)

    assert "background_context.thread_active_tasks" in prompt
    assert "conversation.thread.read" in prompt
    assert thread.thread_id in prompt


def test_background_context_includes_exact_task_runtime_progress_without_second_compact(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.task_identity import (
        task_path_progress_ledger_id,
    )
    from agent_py_agent.agent.conversation.runtime import context_markdown
    from agent_py_agent.agent.task_progress import write_task_progress

    owner_root = tmp_path / "owner"
    store = ConversationStore(owner_root / "conversations")
    thread = _thread(store)
    task_root = owner_root / "tasks" / "2026-07-16" / "demo"
    state = task_root / "work" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        '{"task_id":"task-demo","status":"RUNNING","progress":0.5}\n',
        encoding="utf-8",
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-demo",
            "goal": "完成演示任务",
            "task_path": str(task_root),
        }
    )
    ledger_id = task_path_progress_ledger_id(str(task_root))
    write_task_progress(
        owner_root,
        ledger_id,
        {
            "summary": "实现完成，正在补真实测试",
            "next_action": "运行真实测试并修复失败",
            "items": [{"id": "tests", "title": "真实测试", "status": "in_progress"}],
        },
    )
    write_task_progress(
        owner_root,
        "task-demo",
        {
            "summary": "这是旧请求编号下的错误账本",
            "next_action": "不应出现在后台上下文",
        },
    )
    agent = SimpleNamespace(
        config=None,
        root=owner_root,
        home_paths=SimpleNamespace(owner_home_dir=str(owner_root)),
        subagents=None,
    )
    request = SimpleNamespace(reason="thread_goal_continue", task_id="task-demo", wake_signal=None)

    prompt = context_markdown(agent=agent, store=store, thread=thread, request=request)

    assert "Task Runtime State" in prompt
    assert "完成演示任务" in prompt
    assert "实现完成，正在补真实测试" in prompt
    assert "运行真实测试并修复失败" in prompt
    assert '"schema_version": "plan-continuation.v1"' in prompt
    assert '"existing_item_ids": [\n      "tests"' in prompt
    assert '"reuse_policy": "reuse_existing_ids"' in prompt
    assert '"field": "items[].covers"' in prompt
    assert '"matching": "exact_id_only"' in prompt
    assert "这是旧请求编号下的错误账本" not in prompt
    assert "不应出现在后台上下文" not in prompt
    assert "task_rollup.json" not in prompt


def test_background_context_hides_unrelated_operational_tasks_but_keeps_thread_history(tmp_path):
    from agent_py_agent.agent.conversation.runtime import context_markdown

    owner_root = tmp_path / "owner"
    store = ConversationStore(owner_root / "conversations")
    thread = _thread(store)
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "历史消息仍属于同一个会话",
            "metadata": {"gateway_request_id": "old-task"},
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "old-task",
            "goal": "不相关的旧项目",
        }
    )
    store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "progress",
            "summary": "旧项目观察不得进入当前后台轮",
            "root_task_id": "old-task",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "current-task",
            "goal": "当前精确目标",
        }
    )
    store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "progress",
            "summary": "当前项目观察应该保留",
            "root_task_id": "current-task",
        }
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "child-task",
            "goal": "当前任务的子代理目标",
        }
    )
    store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "progress",
            "summary": "当前子代理观察应该保留",
            "root_task_id": "child-task",
        }
    )
    agent = SimpleNamespace(
        config=None,
        root=owner_root,
        home_paths=SimpleNamespace(owner_home_dir=str(owner_root)),
        subagents=SimpleNamespace(
            load=lambda _run_id: None,
            list_runs=lambda: [SimpleNamespace(id="child-task", root_id="current-task")],
        ),
    )

    prompt = context_markdown(
        agent=agent,
        store=store,
        thread=thread,
        request=SimpleNamespace(
            reason="scheduled_progress_report",
            task_id="current-task",
            wake_signal=None,
        ),
    )

    assert "历史消息仍属于同一个会话" in prompt
    assert "当前精确目标" in prompt
    assert "当前项目观察应该保留" in prompt
    assert "当前任务的子代理目标" in prompt
    assert "当前子代理观察应该保留" in prompt
    assert "不相关的旧项目" not in prompt
    assert "旧项目观察不得进入当前后台轮" not in prompt


def _thread(store: ConversationStore):
    return store.get_or_create_thread(
        {
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "canonical_user_id": "user-1",
        }
    )


def _context_prompt(store: ConversationStore, thread, root) -> str:
    from agent_py_agent.agent.conversation.runtime import context_markdown

    agent = SimpleNamespace(config=None, root=root)
    request = SimpleNamespace(reason="scheduled_progress_report", task_id="", wake_signal=None)
    return context_markdown(agent=agent, store=store, thread=thread, request=request)


def _append_bad_jsonl_rows(path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\nnot-json\n[]\n")
