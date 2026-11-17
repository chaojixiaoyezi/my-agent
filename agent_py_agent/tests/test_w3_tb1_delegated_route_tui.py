"""TB-1 (live 2026-11-16, TUI /route true): delegated work keeps the session directory, absorbs a child finish that
lands during its reply round, and a closed delegated task's late lifecycle wake is stale instead of data corruption."""
from __future__ import annotations

from dataclasses import replace

from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    has_pending_turn_input,
)
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import pending_natural_user_reply
from agent_py_agent.agent.conversation.execution_threads import ensure_execution_thread
from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
from agent_py_agent.agent.conversation.runtime import _wake_signal_is_stale
from agent_py_agent.agent.conversation.session_task_report import report_task_result
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture

_BINDING = {"channel": "chat", "conversation_id": "sess-tui", "actor_id": "local-agent"}


def _tui_source(tmp_path, cwd=None):
    agent = SimpleAgent(AgentConfig(prompt_files=[], enable_plugins=False, my_agent_home=str(tmp_path / "home")),
                        tmp_path / "workspace")
    store = agent.conversation_store.threads
    source = store.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "channel_user_id": "local-agent",
        "channel_conversation_id": "sess-tui", "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir),
    })
    if cwd is not None:
        cwd.mkdir(parents=True, exist_ok=True)
        store.update_atomic(source.thread_id, lambda current: replace(
            current, cwd=str(cwd), workspace_root=str(cwd), runtime_workspace_roots=(str(cwd),)))
    return agent, store.require(source.thread_id)


def test_delegated_work_thread_keeps_the_tui_session_directory(tmp_path):
    work = tmp_path / "tui-dir"
    agent, source = _tui_source(tmp_path, work)
    thread = ensure_execution_thread(agent, source.thread_id, "req-1", kind="delegated", source_binding=_BINDING)
    assert thread.cwd == str(work) and thread.runtime_workspace_roots == (str(work),)
    # 幂等重入返回同一线程，不改已有目录。
    assert ensure_execution_thread(agent, source.thread_id, "req-1", kind="delegated", source_binding=_BINDING) == thread


def test_scheduled_and_unset_sources_keep_the_owner_home(tmp_path):
    work = tmp_path / "tui-dir"
    agent, source = _tui_source(tmp_path, work)
    owner_home = str(agent.home_paths.owner_home_dir)
    scheduled = ensure_execution_thread(agent, source.thread_id, "job-1", kind="scheduled", source_binding=_BINDING)
    assert scheduled.cwd == owner_home and scheduled.runtime_workspace_roots == (owner_home,)
    plain_agent, plain_source = _tui_source(tmp_path / "plain")
    plain = ensure_execution_thread(plain_agent, plain_source.thread_id, "req-2", kind="delegated",
                                    source_binding=_BINDING)
    plain_home = str(plain_agent.home_paths.owner_home_dir)
    assert plain.cwd in {plain_home, str(plain_source.cwd)} and plain.runtime_workspace_roots


def _finished_child_wake(agent, task, child):
    return agent.conversation_store.wakes.raise_signal({
        "thread_id": task.target_thread_id, "reason": "subagent_runner_finished", "urgency": "normal",
        "source_agent_id": child.id, "parent_agent_id": task.task_id, "root_task_id": task.task_id,
        "summary": "child finished", "dedupe_key": f"subagent-finished:{child.id}:DONE",
    })


def test_child_finishing_during_the_reply_round_is_absorbed_not_left_pending(tmp_path, monkeypatch):
    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    child.status = "DONE"
    child.result = "created the file"
    agent.subagents.save(child)
    wake = _finished_child_wake(agent, task, child)
    # 表达轮进行中孩子结束：事件进入本任务邮箱，但零工具表达轮不经过提示组装。
    assert has_pending_turn_input(agent, params) is True
    params.live_archive_state.clear()
    prepare_delegated_execution(agent, params)
    assert pending_natural_user_reply(params)["kind"] == "delegated_result"
    # 修复前这里仍为 True：每次回复都被作废，直到连续作废上限判任务失败。
    assert has_pending_turn_input(agent, params) is False
    assert wake.wake_signal_id in params.live_archive_state["_task_event_ack_ids"]
    acknowledge_injected_turn_input(agent, params)
    assert agent.conversation_store.wakes.pending_one(wake.wake_signal_id) is None


def test_closed_delegated_task_makes_a_late_child_wake_stale(tmp_path, monkeypatch):
    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    wake = _finished_child_wake(agent, task, child)
    store = agent.conversation_store
    assert _wake_signal_is_stale(agent, store, wake, "subagent_runner_finished") is False
    failed = store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="failed"))
    # 终态但原 sender 回报尚未发布：保留唤醒，交原无模型补账入口。
    assert _wake_signal_is_stale(agent, store, wake, "subagent_runner_finished") is False
    assert report_task_result(agent, failed) is True
    assert _wake_signal_is_stale(agent, store, wake, "subagent_runner_finished") is True
