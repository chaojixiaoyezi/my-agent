from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.process_events import (
    owner_has_pending_process_completions,
    process_completion_delivery_state,
    process_completion_target,
    reconcile_process_completions,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.tooling.process_registry import (
    BackgroundProcess,
    ProcessAccessScope,
    process_registry,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)


# LLM: 测试使用真实持久存储与伪终态进程记录，不启动或杀宿主进程。
# 函数用途: 组装隔离 owner/thread 与已结束进程，验证通知重入和权限边界。
def _fixture(tmp_path, status="exited"):
    owner = tmp_path / "owner"
    store = ConversationStore(owner / "conversations")
    thread = store.get_or_create_thread({"canonical_user_id": "a", "channel": "tui", "channel_conversation_id": "a"})
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_home_dir=owner),
                            config=SimpleNamespace(background_process_notifications=True))
    params = SimpleNamespace(context_scope="conversation", run_id="run-a", task_id="task-a", request_id="req-a",
                             task_attributes={"conversation_thread_id": thread.thread_id, "conversation_task_id": "task-a"})
    target = process_completion_target(agent, params)
    root = process_session_store_root(owner, owner)
    authority = ProcessSessionStore(root)
    record = BackgroundProcess("bg-test-notice", "test", 99999999, 1.0,
        access_scope=ProcessAccessScope("a", thread.thread_id, str(owner)), pid_birth_token="test",
        status=status, exit_code=0, completion_target=target, store_root=str(root))
    authority.write(record.to_record())
    process_registry.clear()
    return agent, params, authority


def test_completion_survives_restart_and_notifies_once(tmp_path):
    agent, params, authority = _fixture(tmp_path)
    assert owner_has_pending_process_completions(agent.home_paths.owner_home_dir)
    assert reconcile_process_completions(agent) == 1
    signals = agent.conversation_store.pending_wake_signals(limit=0)
    assert len(signals) == 1
    assert signals[0].root_task_id == params.task_id
    assert signals[0].metadata["process_completion"]["exit_code"] == 0
    assert authority.load("bg-test-notice").record["completion_notice_id"] == signals[0].wake_signal_id
    process_registry.clear()
    assert reconcile_process_completions(agent) == 0
    assert not owner_has_pending_process_completions(agent.home_paths.owner_home_dir)


def test_publish_crash_retry_dedupes_and_keeps_receipt(tmp_path, monkeypatch):
    agent, _, authority = _fixture(tmp_path)
    original = ProcessSessionStore.write

    def fail_receipt(self, payload):
        if payload.get("completion_notice_id"):
            raise OSError("simulated receipt write failure")
        return original(self, payload)

    monkeypatch.setattr(ProcessSessionStore, "write", fail_receipt)
    reconcile_process_completions(agent)
    assert not authority.load("bg-test-notice").record["completion_notice_id"]
    monkeypatch.setattr(ProcessSessionStore, "write", original)
    assert reconcile_process_completions(agent) == 1
    assert len(agent.conversation_store.pending_wake_signals(limit=0)) == 1


def test_explicit_stop_is_not_auto_restarted(tmp_path):
    agent, _, authority = _fixture(tmp_path, "killed")
    assert reconcile_process_completions(agent) == 0
    assert authority.load("bg-test-notice").record["completion_notice_id"] == "explicit_stop"
    assert not agent.conversation_store.pending_wake_signals(limit=0)


def test_late_completion_is_deliverable_but_stopped_task_is_not(tmp_path):
    from dataclasses import replace

    agent, params, _authority = _fixture(tmp_path)
    store = agent.conversation_store
    store.bind_task({"thread_id": params.task_attributes["conversation_thread_id"],
                     "task_id": params.task_id, "status": "completed", "goal": "test"})
    reconcile_process_completions(agent)
    signal = store.pending_wake_signals(limit=0)[0]
    assert process_completion_delivery_state(agent, signal) == "ready"
    assert not process_completion_delivery_state(agent, replace(signal, source_agent_id="another-run"))
    stopped, stopped_params, _ = _fixture(tmp_path / "stopped")
    stopped.conversation_store.bind_task({"thread_id": stopped_params.task_attributes["conversation_thread_id"],
        "task_id": stopped_params.task_id, "status": "interrupted", "goal": "test"})
    reconcile_process_completions(stopped)
    stopped_signal = stopped.conversation_store.pending_wake_signals(limit=0)[0]
    assert not process_completion_delivery_state(stopped, stopped_signal)


@pytest.mark.parametrize("status,expected", [("completed", 1), ("interrupted", 0), ("cancelled", 0)])
def test_late_completion_passes_scheduler_and_claim_admission(tmp_path, monkeypatch, status, expected):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    prepared, params, authority = _fixture(tmp_path)
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False), prepared.home_paths.owner_home_dir)
    agent.conversation_store = store = prepared.conversation_store
    root = process_session_store_root(agent.effective_workspace_root, agent.home_paths.owner_home_dir)
    record = authority.load("bg-test-notice").record
    record["access_scope"]["owner_home"] = str(agent.home_paths.owner_home_dir)
    ProcessSessionStore(root).write(record)
    calls = []

    class Backend:
        def generate(self, prompt, **kwargs):
            calls.append(prompt)
            return ModelResponse(text="采样进程已退出，退出码 0。", backend="test")

    agent.backend = Backend()
    store.bind_task({"thread_id": params.task_attributes["conversation_thread_id"],
        "task_id": params.task_id, "status": status, "goal": "采样后告知结果"})
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store})
    original_write = ProcessSessionStore.write

    def fail_receipt(self, payload):
        if payload.get("completion_notice_id"):
            raise OSError("receipt temporarily unavailable")
        return original_write(self, payload)

    if expected:
        monkeypatch.setattr(ProcessSessionStore, "write", fail_receipt)
        reconcile_process_completions(agent)
        assert scheduler.tick() == []
        assert len(store.pending_wake_signals(limit=0)) == 1
        assert not calls
        monkeypatch.setattr(ProcessSessionStore, "write", original_write)
    assert reconcile_process_completions(agent) == 1
    reports = scheduler.tick()
    assert len(calls) == expected
    assert len(reports) == expected
    if expected:
        assert "退出码 0" in reports[0].response
        assert any("退出码 0" in row.content for row in store.recent_messages(params.task_attributes["conversation_thread_id"]))
    assert not store.pending_wake_signals(limit=0)
    assert scheduler.tick() == []
    assert len(calls) == expected


def test_disabled_notifications_and_child_scope_do_not_launch_shadow_main(tmp_path):
    agent, params, _ = _fixture(tmp_path)
    params.context_scope = "task_local"
    assert process_completion_target(agent, params) == {}
    agent.config.background_process_notifications = False
    assert reconcile_process_completions(agent) == 0
    assert not agent.conversation_store.pending_wake_signals(limit=0)


def test_completion_target_is_immutable_and_thread_scoped(tmp_path):
    _, _, authority = _fixture(tmp_path)
    record = authority.load("bg-test-notice").record
    record["completion_target"]["thread_id"] = "another-thread"
    with pytest.raises(ValueError):
        authority.write(record)


def test_notified_receipt_survives_stale_registry_write(tmp_path):
    agent, _, authority = _fixture(tmp_path)
    stale = authority.load("bg-test-notice").record
    reconcile_process_completions(agent)
    authority.write(stale)
    assert authority.load("bg-test-notice").record["completion_notice_id"]
    assert reconcile_process_completions(agent) == 0
